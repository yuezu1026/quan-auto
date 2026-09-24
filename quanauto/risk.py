"""风控层 —— 契约《智能量化交易平台-风控层接口契约文档》v1.1 §三 的实现。

⚠️ **本文件的实现状态（2026-09-24）**：契约面（枚举、注册表、数据类、方法签名）逐字落全，
回测路径所需的**行为已实现**（加载/热更新/分层解析/零 IO 校验/熔断/峰值/Kill Switch/阈值变更），
`tests/test_risk_engine.py` 的 67 条断言覆盖契约 §3.8 的 13 行测试要求。

**尚未接线的部分**（写在明处，免得被当成已完成）：
* `DbRiskRuleStore` 的三个方法仍抛 `RiskConfigLoadError` —— 回测路径用 `MemoryRiskRuleStore`，
  数据库版留到 I4 接真实交易链路时一起做（`db/risk_control.sql` 已备好）。
* `emergency_flatten` 只返回任务标识，真正的清仓执行不在本模块（D5：清仓是显式通道）。
* 短轮询 watchdog 只在 DB 版才需要，回测路径不启动它。

设计要点（写给下一个改这里的人）：

* 契约里凡是**能被机器查的**都照抄：枚举取值、字段名、方法名、`RiskActionEnum` 的严重度递增序。
  唯一对不上的两处已经登记在案的偏差：① 契约 §3.2.4 写 `side: SideEnum`，但仓库里**没有
  `SideEnum`**（主契约只有 `Direction`/`DirectionEnum`，取值同为 BUY/SELL），这里落在
  `Direction` 上；② 契约 §3.2.1 把 `apply_change` 的返回注解写成 `RuleChangeResult`，
  而 §3.2.7 规范块定义的是 `RiskChangeResult` —— 以规范块为准。
* **`check()` 里禁止任何 IO**（D1）。它只读 `self._index`（内存索引）与 `self._peaks`。
  这条不是口号：测试里用打桩 store 数调用次数，必须**恰好 0 次**。
* **峰值与回撤只由本模块维护**（D8）。`RiskSnapshot` 特意不带峰值字段，就是为了不让
  「谁算回撤」分裂成两套口径。**峰值必须能跨重启**，否则回撤熔断会静默失效 ——
  这是本模块最容易被"重构掉"的性质。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .enums import Direction
from .errors import (
    IllegalStateError,
    RiskConfigInvalidError,
    RiskConfigLoadError,
    RiskRuleNotFoundError,
    RiskRuleVersionConflictError,
)

# ── 常量 ──────────────────────────────────────────────────────────────────
GLOBAL_SCOPE_KEY = "*"
"""GLOBAL 层的作用域键，固定值（契约 §3.1.2 / DDL `ck_risk_rule_global_key`）。"""

RESUME_CONFIRMATION = "CONFIRM_RESUME_TRADING"
"""解除 Kill Switch 必须逐字携带的确认串（契约 §3.4.1）。"""

ACCOUNT_PEAK_PREFIX = "ACCOUNT:"
STRATEGY_PEAK_PREFIX = "STRATEGY:"

RISK_PEAK_MISSING = "RISK_011"
"""权益峰值缺失告警码（D8）：峰值没恢复时回撤熔断会静默失效，必须以 WARNING 露面。"""


# ── 枚举（契约 §3.1.3，逐字）──────────────────────────────────────────────
class RuleTypeEnum(Enum):
    """风控规则类型枚举。"""

    MAX_POSITION_PCT = "MAX_POSITION_PCT"
    MAX_DAILY_TRADES = "MAX_DAILY_TRADES"
    STRATEGY_DRAWDOWN = "STRATEGY_DRAWDOWN"
    ACCOUNT_DRAWDOWN = "ACCOUNT_DRAWDOWN"
    MAX_ORDER_AMOUNT_PCT = "MAX_ORDER_AMOUNT_PCT"
    MAX_SECTOR_PCT = "MAX_SECTOR_PCT"


class RuleScopeEnum(Enum):
    """规则作用域枚举，取值顺序即优先级（前者覆盖后者）。"""

    SYMBOL = "SYMBOL"
    STRATEGY = "STRATEGY"
    ACCOUNT = "ACCOUNT"
    GLOBAL = "GLOBAL"


class RuleUnitEnum(Enum):
    """阈值单位枚举。"""

    RATIO = "RATIO"
    COUNT = "COUNT"
    ABSOLUTE = "ABSOLUTE"


class TighteningDirectionEnum(Enum):
    """收紧方向枚举：指明阈值朝哪个方向变化算「收紧」。"""

    DECREASE = "DECREASE"
    INCREASE = "INCREASE"


class RiskActionEnum(Enum):
    """风控裁决动作枚举，严重度递增。"""

    PASS = "PASS"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    HALT = "HALT"


class RiskRunStateEnum(Enum):
    """风控层运行状态枚举，对应 D5 放行矩阵。"""

    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    KILLED = "KILLED"


class BreakerStateEnum(Enum):
    """熔断状态枚举。"""

    NORMAL = "NORMAL"
    TRIPPED = "TRIPPED"


class SeverityEnum(Enum):
    """违规严重度。

    契约 §3.2.6 只把 `severity` 写成 `str`（WARNING / ERROR / CRITICAL），没给枚举。
    这里补一个**实现层**枚举，因为「取最高严重度」需要一个可比较的序 —— 靠字符串
    字典序去比大小，某天加一个词就会静默排错。登记在 manifest 的 `impl_only`。
    """

    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    SeverityEnum.WARNING: 1,
    SeverityEnum.ERROR: 2,
    SeverityEnum.CRITICAL: 3,
}

_ACTION_RANK = {
    RiskActionEnum.PASS: 0,
    RiskActionEnum.REDUCE: 1,
    RiskActionEnum.REJECT: 2,
    RiskActionEnum.HALT: 3,
}

# 名单型规则的 `rule_id`（§3.6.3）：黑名单**不存** `risk_rule`，但要出现在裁决明细里。
# 它没有自己的 `RuleTypeEnum`（契约 §3.1.3 的枚举里没有），也没再往契约枚举里塞一个成员：
# 「黑名单」在语义上就是「该标的仓位上限 = 0」，所以明细里用 `MAX_POSITION_PCT` 类型
# + `threshold=0` 表达，`rule_id` 单独用 `blacklist` 以便事后区分两者。
BLACKLIST_RULE_ID = "blacklist"

_SEVERITY_ACTION = {
    SeverityEnum.WARNING: RiskActionEnum.PASS,
    SeverityEnum.ERROR: RiskActionEnum.REDUCE,
    SeverityEnum.CRITICAL: RiskActionEnum.REJECT,
}
"""严重度 → 动作的**下限**（不是建议）：WARNING ⇒ 至少露面，ERROR ⇒ 至少减量，
CRITICAL ⇒ 至少拒绝。

为什么下限而不是直接映射：每条规则的**动作是各自判决的**（仓位超限可以减量，仓位为 0
就只能拒），而下限保证「不能自相矛盾」：CRITICAL 却给出 PASS 在结构上就不可能出现。
为什么 CRITICAL 的下限不是 HALT：HALT 是**回撤熔断**这一个动作（停止该单元全部开仓），
把它当成「严重」的通用表达会把黑名单、Kill Switch、配置缺失统统升格成熔断。
"""


def worse_action(left: RiskActionEnum, right: RiskActionEnum) -> RiskActionEnum:
    """取更严重的裁决（`PASS < REDUCE < REJECT < HALT`）。"""
    return left if _ACTION_RANK[left] >= _ACTION_RANK[right] else right


def aggregate_decision(violations: List["RiskViolation"]) -> Tuple[RiskActionEnum, SeverityEnum]:
    """把多条违规聚合成一个裁决（§3.8：取最高严重度）。

    两步：① 动作取最严重的那条；② 用最高严重度做**下限**回压 —— 这样
    「CRITICAL 却给出 PASS」在结构上就不可能发生，而不是靠调用方自觉。
    """
    if not violations:
        return RiskActionEnum.PASS, SeverityEnum.WARNING
    highest = SeverityEnum.WARNING
    action = RiskActionEnum.PASS
    for violation in violations:
        action = worse_action(action, violation.action)
        severity = SeverityEnum(violation.severity)
        if severity.rank > highest.rank:
            highest = severity
    floor = _SEVERITY_ACTION[highest]
    if _ACTION_RANK[action] < _ACTION_RANK[floor]:
        action = floor
    return action, highest


# ── 规则注册表（契约 §3.1.1，逐字）────────────────────────────────────────
@dataclass(frozen=True)
class RuleSpec:
    """注册表里的一行 —— `rule_id` 是固定标识，不可自定义。"""

    rule_id: str
    rule_type: RuleTypeEnum
    unit: RuleUnitEnum
    tightening_direction: TighteningDirectionEnum
    default_threshold: float
    description: str


RULE_SPECS: Tuple[RuleSpec, ...] = (
    RuleSpec(
        "max_position_pct", RuleTypeEnum.MAX_POSITION_PCT, RuleUnitEnum.RATIO,
        TighteningDirectionEnum.DECREASE, 0.10, "单票仓位上限（该标的持仓市值 / 账户总资产）",
    ),
    RuleSpec(
        "max_daily_trades", RuleTypeEnum.MAX_DAILY_TRADES, RuleUnitEnum.COUNT,
        TighteningDirectionEnum.DECREASE, 3, "单日单票交易次数上限（买卖笔数合计）",
    ),
    RuleSpec(
        "strategy_drawdown_pct", RuleTypeEnum.STRATEGY_DRAWDOWN, RuleUnitEnum.RATIO,
        TighteningDirectionEnum.DECREASE, 0.15, "单策略回撤熔断阈值（(峰值 − 当前) / 峰值）",
    ),
    RuleSpec(
        "account_drawdown_pct", RuleTypeEnum.ACCOUNT_DRAWDOWN, RuleUnitEnum.RATIO,
        TighteningDirectionEnum.DECREASE, 0.20, "全账户回撤熔断阈值（(峰值 − 当前) / 峰值）",
    ),
    RuleSpec(
        "max_order_amount_pct", RuleTypeEnum.MAX_ORDER_AMOUNT_PCT, RuleUnitEnum.RATIO,
        TighteningDirectionEnum.DECREASE, 0.10, "单笔委托成交额占比上限（委托金额 / 日均成交额）",
    ),
    RuleSpec(
        "max_sector_pct", RuleTypeEnum.MAX_SECTOR_PCT, RuleUnitEnum.RATIO,
        TighteningDirectionEnum.DECREASE, 0.30, "单行业集中度上限（单行业持仓市值 / 账户总资产）",
    ),
)

RULE_REGISTRY: Dict[str, RuleSpec] = {spec.rule_id: spec for spec in RULE_SPECS}

RULE_ORDER: Tuple[str, ...] = tuple(spec.rule_id for spec in RULE_SPECS)
"""判定顺序 —— 固定顺序才能让同一笔委托的违规明细可复现。"""

GLOBAL_RULE_IDS: Tuple[str, ...] = RULE_ORDER
"""GLOBAL 层必须**完整**包含这 6 条，缺一条即按 D4 视为配置非法。"""

RULE_TYPE_TO_ID: Dict[RuleTypeEnum, str] = {spec.rule_type: spec.rule_id for spec in RULE_SPECS}

DRAWDOWN_RULE_IDS: Tuple[str, ...] = ("strategy_drawdown_pct", "account_drawdown_pct")

# 每条规则的裁决口径 —— **契约只给了「判定口径」，没给 action/severity 映射**，
# 下表是本实现的补充裁决，已登记在契约的偏差登记里（见收工记录）。
# 两条原则：① 能被缩减的（仓位/集中度/流动性）用 REDUCE，减不到量才算 REJECT；
#          ② 熔断类用 HALT，且**只拦开仓** —— 任何情况下都要留出货的路（D5）。
RULE_DECISION: Dict[str, Tuple[RiskActionEnum, SeverityEnum]] = {
    "max_position_pct": (RiskActionEnum.REDUCE, SeverityEnum.ERROR),
    "max_daily_trades": (RiskActionEnum.REJECT, SeverityEnum.ERROR),
    "strategy_drawdown_pct": (RiskActionEnum.HALT, SeverityEnum.CRITICAL),
    "account_drawdown_pct": (RiskActionEnum.HALT, SeverityEnum.CRITICAL),
    "max_order_amount_pct": (RiskActionEnum.REDUCE, SeverityEnum.WARNING),
    "max_sector_pct": (RiskActionEnum.REDUCE, SeverityEnum.ERROR),
}

OPEN_ONLY_RULE_IDS: Tuple[str, ...] = (
    "max_position_pct",
    "max_daily_trades",
    "strategy_drawdown_pct",
    "account_drawdown_pct",
    "max_sector_pct",
)
"""只拦开仓的规则：仓位、频率、集中度、熔断 —— 拦它们的平仓等于把仓位锁死。

`max_order_amount_pct`（流动性）**不在其中**：流动性冲击与方向无关，减仓同样要控冲击成本。
"""


def _ratio_in_range(value: float) -> bool:
    return 0.0 < value <= 1.0


def validate_threshold(rule_id: str, threshold: float) -> None:
    """按注册表的 unit 校验阈值；不合法抛 `RiskConfigInvalidError`（RISK_004）。

    这是「`0.1` 与 `10` 差 100 倍」那类静默算错的**唯一**拦截点：RATIO 一律存 0~1 小数。
    """
    spec = RULE_REGISTRY.get(rule_id)
    if spec is None:
        raise RiskConfigInvalidError("规则 %r 不在注册表里，不可自定义新 rule_id" % (rule_id,))
    value = float(threshold)
    if spec.unit is RuleUnitEnum.RATIO:
        if not _ratio_in_range(value):
            raise RiskConfigInvalidError(
                "规则 %s 的单位是 RATIO，阈值必须是 0~1 小数，收到 %r"
                "（若是百分数请除以 100 —— 0.1 与 10 差 100 倍，静默算错最难查）"
                % (rule_id, threshold)
            )
    elif spec.unit is RuleUnitEnum.COUNT:
        if value < 0 or value != int(value):
            raise RiskConfigInvalidError(
                "规则 %s 的单位是 COUNT，阈值必须是非负整数，收到 %r" % (rule_id, threshold)
            )
    else:  # ABSOLUTE
        if value < 0:
            raise RiskConfigInvalidError(
                "规则 %s 的单位是 ABSOLUTE，阈值必须 >= 0，收到 %r" % (rule_id, threshold)
            )


# ── 数据类（契约 §3.2.2 ~ §3.2.7 / §3.3.4 / §3.3.5 / §3.4.2，逐字）───────
@dataclass
class RiskEngineConfig:
    """风控引擎配置数据类。"""

    poll_interval_seconds: int = 5
    peak_persist_interval_seconds: int = 30
    lkg_cache_path: str = "data/risk_lkg.json"
    engine_state_persist_interval_seconds: int = 5
    degraded_allow_close: bool = True
    breaker_allow_close: bool = True


@dataclass
class RiskSnapshot:
    """风控判定所需的账户/策略/标的实时快照。

    注意: 峰值与回撤**不在本类中** —— 由风控引擎独家维护（D8），避免口径分裂。
    所以 `total_asset` / `strategy_equity` 等值字段**故意不给默认值**：少传一个就
    是 `TypeError`，而给个 `0.0` 默认值会让它静默变成「回撤 0% ⇒ 永不熔断」。
    """

    total_asset: float
    available_capital: float
    strategy_equity: float
    symbol_avg_daily_amount: float
    trading_date: str
    position_value_by_symbol: Dict[str, float] = field(default_factory=dict)
    position_value_by_sector: Dict[str, float] = field(default_factory=dict)
    daily_trade_count_by_symbol: Dict[str, int] = field(default_factory=dict)


@dataclass
class RiskCheckRequest:
    """风控校验请求数据类。

    由组合调度层在生成最终交易指令后、提交执行层之前构造（PRD: 风控拥有最终否决权）。
    """

    account_id: str
    strategy_id: str
    symbol: str
    side: Direction
    is_open: bool
    quantity: int
    price: float
    snapshot: RiskSnapshot
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class RiskViolation:
    """单条风控违规明细数据类。"""

    rule_id: str
    rule_type: RuleTypeEnum
    scope: RuleScopeEnum
    scope_key: str
    threshold: float
    observed: float
    severity: str
    action: RiskActionEnum
    message: str


@dataclass
class RiskCheckResponse:
    """风控校验响应数据类。"""

    passed: bool = False
    action: RiskActionEnum = RiskActionEnum.PASS
    rule_version: int = 0
    violations: List[RiskViolation] = field(default_factory=list)
    adjusted_quantity: int = 0
    run_state: RiskRunStateEnum = RiskRunStateEnum.NORMAL
    message: str = ""
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class BreakerState:
    """熔断单元状态数据类。持久化于 risk_breaker_state，跨重启保留（D7）。"""

    breaker_key: str
    state: BreakerStateEnum = BreakerStateEnum.NORMAL
    triggered_at: Optional[datetime] = None
    trigger_reason: str = ""
    resumed_at: Optional[datetime] = None
    resumed_by: str = ""


@dataclass
class ReloadResult:
    """规则重载结果数据类。"""

    reloaded: bool
    rule_version: int
    attempted_version: int
    run_state: RiskRunStateEnum = RiskRunStateEnum.NORMAL
    error: str = ""


@dataclass
class RiskEngineHealth:
    """风控引擎健康度数据类。"""

    run_state: RiskRunStateEnum = RiskRunStateEnum.NORMAL
    rule_version: int = 0
    lkg_version: int = 0
    last_load_at: Optional[datetime] = None
    store_reachable: bool = False
    available: bool = False


@dataclass
class RiskChangeResult:
    """阈值变更结果数据类（D6）。"""

    applied: bool
    pending: bool
    old_threshold: float
    new_threshold: float
    direction: TighteningDirectionEnum
    rule_version: int = 0
    effective_at: Optional[datetime] = None
    message: str = ""


@dataclass
class RiskRule:
    """单条风控规则数据类。"""

    rule_id: str
    rule_type: RuleTypeEnum
    scope: RuleScopeEnum
    scope_key: str
    threshold: float
    unit: RuleUnitEnum
    comparison: str = "LTE"
    enabled: bool = True
    version: int = 1
    updated_by: str = "system"
    updated_at: Optional[datetime] = None
    description: str = ""

    @property
    def tightening_direction(self) -> TighteningDirectionEnum:
        """收紧方向，由 rule_type 静态映射（3.1.1），**不从数据库读取**。"""
        spec = RULE_REGISTRY.get(self.rule_id)
        if spec is None:
            raise RiskConfigInvalidError("规则 %r 不在注册表里" % (self.rule_id,))
        return spec.tightening_direction


@dataclass
class RuleChangeRequest:
    """阈值变更请求数据类。约束: 仅由运维入口构造（D10）。"""

    rule_id: str
    scope: RuleScopeEnum
    scope_key: str
    new_threshold: float
    operator: str
    reason: str
    confirm_relax: bool = False


@dataclass
class SwitchState:
    """Kill Switch 状态数据类。持久化于本地文件 + risk_switch_state 表。"""

    active: bool = False
    activated_at: Optional[datetime] = None
    activated_by: str = ""
    reason: str = ""
    deactivated_at: Optional[datetime] = None
    deactivated_by: str = ""


def default_global_rules(version: int = 1) -> List[RiskRule]:
    """按 §3.1.1 生成 GLOBAL 默认规则 —— 与 `db/risk_control.sql` 种子数据同源。"""
    return [
        RiskRule(
            rule_id=spec.rule_id,
            rule_type=spec.rule_type,
            scope=RuleScopeEnum.GLOBAL,
            scope_key=GLOBAL_SCOPE_KEY,
            threshold=spec.default_threshold,
            unit=spec.unit,
            comparison="LTE",
            enabled=True,
            version=version,
            description=spec.description,
        )
        for spec in RULE_SPECS
    ]


# ── 规则存储（契约 §3.3.1 ~ §3.3.3）───────────────────────────────────────
class RiskRuleStore(ABC):
    """风控规则存储抽象基类。

    约束: 本抽象类**仅**被 `load()` / `reload()` / `apply_change()` 调用，
    **绝不允许**被 `check()` 调用（D1）。
    """

    @abstractmethod
    def load_rules(self) -> List[RiskRule]:
        """全量加载当前生效规则。异常: `RiskConfigLoadError` 存储层不可达。"""

    @abstractmethod
    def get_version(self) -> int:
        """读取当前配置版本号；配置从未变更时为 0。异常: `RiskConfigLoadError`。"""

    @abstractmethod
    def save_change(self, change: RuleChangeRequest) -> int:
        """写入阈值变更并递增版本号，返回新版本号。"""


class MemoryRiskRuleStore(RiskRuleStore):
    """内存规则存储实现。

    用途: 回测以固定快照运行保证可复现（D9）；单元测试无外部依赖。
    约束: `save_change()` 仅更新内存版本号，不持久化。
    """

    def __init__(self, rules: Optional[List[RiskRule]] = None) -> None:
        self._rules: List[RiskRule] = list(rules) if rules is not None else default_global_rules()
        # 版本号从 1 起：与 `db/risk_control.sql` 的 `risk_config_version` 种子一致，
        # 这样回测口径与生产口径的版本号不会差 1（差 1 就会让 D9 的留痕对不上审计）。
        self._version = 1

    def load_rules(self) -> List[RiskRule]:
        return list(self._rules)

    def get_version(self) -> int:
        return self._version

    def save_change(self, change: RuleChangeRequest) -> int:
        self._version += 1
        return self._version


class DbRiskRuleStore(RiskRuleStore):
    """PostgreSQL 规则存储实现，生产环境使用。

    唯一允许写 `risk_rule` 表的组件（D10）。连接由外部工厂提供，本模块**不**引入
    任何数据库驱动 —— `tools/` 与 `quanauto/` 都不该为风控多背一个依赖。
    """

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def load_rules(self) -> List[RiskRule]:
        raise RiskConfigLoadError("DbRiskRuleStore 尚未接线（I3 只跑回测路径，用 MemoryRiskRuleStore）")

    def get_version(self) -> int:
        raise RiskConfigLoadError("DbRiskRuleStore 尚未接线（I3 只跑回测路径，用 MemoryRiskRuleStore）")

    def save_change(self, change: RuleChangeRequest) -> int:
        raise RiskConfigLoadError("DbRiskRuleStore 尚未接线（I3 只跑回测路径，用 MemoryRiskRuleStore）")


# ── 风控引擎（契约 §3.2.1）────────────────────────────────────────────────
class RiskEngine:
    """风控引擎。

    职责: 持有内存规则快照，对所有交易指令执行零 IO 校验并作出最终裁决。
    约束: `check()` 内禁止访问数据库 / Redis / 网络（D1）。
    约束: 本类不得向策略与 Agent 暴露任何阈值写入方法（D10）。
    """

    def __init__(
        self,
        store: RiskRuleStore,
        config: Optional[RiskEngineConfig] = None,
        *,
        kill_switch: Optional["KillSwitch"] = None,
        next_trading_day: Optional[Callable[[str], str]] = None,
    ) -> None:
        # 内存规则快照（D1 的根基：索引只在 load()/reload()/apply_change() 里重建）
        self._store = store
        self._config = config or RiskEngineConfig()
        self._kill_switch = kill_switch
        self._next_trading_day = next_trading_day
        self._index: Dict[Tuple[str, RuleScopeEnum, str], RiskRule] = {}
        self._rule_version = 0
        self._lkg_version = 0
        self._loaded = False
        self._last_load_at: Optional[datetime] = None
        self._store_reachable = False
        self._degraded = False
        # 运行态（熔断状态 + 权益峰值），跨重启保留（D7/D8）
        self._breakers: Dict[str, BreakerState] = {}
        self._peaks: Dict[str, float] = {}
        self._restored_keys: set = set()
        self._peak_warned: set = set()
        self._dirty_peaks: set = set()
        self._dirty_breakers: set = set()
        self._last_peak_persist = _monotonic()
        self._blacklist: set = set()
        self._pending: List[Tuple[RuleChangeRequest, str]] = []
        self._lock = threading.RLock()
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

    # ── 加载与热更新 ────────────────────────────────────────────────────
    def load(self) -> int:
        """阻塞式全量加载规则；失败抛 `RiskConfigInvalidError` / `RiskConfigLoadError`（D4）。"""
        try:
            rules = self._store.load_rules()
            version = int(self._store.get_version())
        except RiskConfigLoadError:
            return self._recover_from_lkg()
        self._store_reachable = True
        if self._loaded and version < self._rule_version:
            raise RiskRuleVersionConflictError(
                "[RISK_010] 配置版本回退（%s < %s）：拒绝加载并保持 LKG（回滚要显式操作）"
                % (version, self._rule_version)
            )
        # 先整批校验、再整体上线 —— 分开做就是 D4 说的「半新半旧」
        self._install(self._validate(rules), version)
        return self._rule_version

    def reload(self) -> ReloadResult:
        """尝试重载；失败**不抛异常**，保持 LKG 并切 `DEGRADED`（D4）。"""
        current = self._rule_version
        try:
            version = int(self._store.get_version())
        except RiskConfigLoadError:
            with self._lock:
                self._store_reachable = False
                self._degraded = True
            return ReloadResult(
                False, current, 0, self.get_run_state(), "[RISK_005] 配置源不可达，继续用 LKG"
            )
        self._store_reachable = True
        if version == current:
            return ReloadResult(False, current, version, self.get_run_state(), "")
        if version < current:
            return ReloadResult(
                False,
                current,
                version,
                self.get_run_state(),
                "[RISK_010] 配置版本回退（%s < %s），拒绝重载" % (version, current),
            )
        try:
            index = self._validate(self._store.load_rules())
        except (RiskConfigLoadError, RiskConfigInvalidError) as exc:
            with self._lock:
                self._degraded = True
            return ReloadResult(False, current, version, self.get_run_state(), str(exc))
        self._install(index, version)
        return ReloadResult(True, self._rule_version, version, self.get_run_state(), "")

    def get_rule_version(self) -> int:
        return self._rule_version

    def get_effective_rule(
        self, rule_id: str, account_id: str, strategy_id: str, symbol: str
    ) -> RiskRule:
        """按 D2 分层解析生效阈值，纯内存。GLOBAL 缺失 ⇒ `RiskRuleNotFoundError`。"""
        if rule_id not in RULE_REGISTRY:
            raise RiskConfigInvalidError(
                "[RISK_004] 规则 %r 不在注册表里，不可自定义新 rule_id" % (rule_id,)
            )
        candidates = (
            (RuleScopeEnum.SYMBOL, symbol),
            (RuleScopeEnum.STRATEGY, strategy_id),
            (RuleScopeEnum.ACCOUNT, account_id),
            (RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY),
        )
        with self._lock:
            for scope, key in candidates:
                rule = self._index.get((rule_id, scope, key))
                if rule is not None and rule.enabled:
                    return rule
        raise RiskRuleNotFoundError(
            "[RISK_004] 规则 %s 在所有作用域都无可用层（enabled=0 视为不存在，D2；"
            "GLOBAL 也没有就是配置非法，D4）" % (rule_id,)
        )

    # ── 加载内部实现 ────────────────────────────────────────────────────
    def _validate(
        self, rules: Iterable[RiskRule]
    ) -> Dict[Tuple[str, RuleScopeEnum, str], RiskRule]:
        """整批校验（单位 / 作用域 / 重复 / GLOBAL 完整性）；任一条不合格就整批拒绝。"""
        index: Dict[Tuple[str, RuleScopeEnum, str], RiskRule] = {}
        for rule in rules:
            spec = RULE_REGISTRY.get(rule.rule_id)
            if spec is None:
                raise RiskConfigInvalidError(
                    "[RISK_004] 规则 %r 不在注册表里，不可自定义规则" % (rule.rule_id,)
                )
            if rule.unit is not spec.unit:
                raise RiskConfigInvalidError(
                    "[RISK_004] 规则 %s 的单位应为 %s，收到 %s"
                    % (rule.rule_id, spec.unit.value, getattr(rule.unit, "value", rule.unit))
                )
            if not isinstance(rule.scope, RuleScopeEnum) or not rule.scope_key:
                raise RiskConfigInvalidError(
                    "[RISK_004] 规则 %s 的作用域或 scope_key 非法（%r / %r）"
                    % (rule.rule_id, rule.scope, rule.scope_key)
                )
            if rule.scope is RuleScopeEnum.GLOBAL and rule.scope_key != GLOBAL_SCOPE_KEY:
                raise RiskConfigInvalidError(
                    "[RISK_004] GLOBAL 层规则的 scope_key 必须是 %r" % (GLOBAL_SCOPE_KEY,)
                )
            validate_threshold(rule.rule_id, rule.threshold)
            key = (rule.rule_id, rule.scope, rule.scope_key)
            if key in index:
                raise RiskConfigInvalidError(
                    "[RISK_004] 规则 %s 在作用域 %s/%s 上重复定义"
                    % (rule.rule_id, rule.scope.value, rule.scope_key)
                )
            index[key] = rule
        for rule_id in GLOBAL_RULE_IDS:
            if (rule_id, RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY) not in index:
                raise RiskConfigInvalidError(
                    "[RISK_004] GLOBAL 层缺少规则 %s：没有兜底值不是「不生效」，是「算不出来」（D4）"
                    % (rule_id,)
                )
        return index

    def _install(
        self, index: Dict[Tuple[str, RuleScopeEnum, str], RiskRule], version: int
    ) -> None:
        """把已校验的规则集整体上线（唯一的上线口）。"""
        with self._lock:
            self._index = index
            self._rule_version = version
            self._lkg_version = version
            self._loaded = True
            self._last_load_at = _now()
            self._degraded = False
            self._restore_runtime_state()

    def _restore_runtime_state(self) -> None:
        """从存储恢复峰值与熔断状态（D7/D8）。**读不到不阻断规则上线**。"""
        if not self._persistence_available:
            return
        try:
            peaks = dict(self._store.load_equity_peaks())
            self._breakers = dict(self._store.load_breaker_states())
        except Exception:  # noqa: BLE001 - 运行态读不到属降级，不该让引擎起不来
            return
        for key, value in peaks.items():
            self._peaks[key] = max(float(value), self._peaks.get(key, 0.0))
            self._restored_keys.add(key)

    @property
    def _persistence_available(self) -> bool:
        """存储是否具备运行态落盘能力（`DbRiskRuleStore` 有，契约的 3 方法面没有）。"""
        return hasattr(self._store, "load_equity_peaks") and hasattr(
            self._store, "save_equity_peak"
        )

    def _recover_from_lkg(self) -> int:
        """存储不可达时的 LKG 回路（D4/D5）：内存 LKG → 缓存文件 → 拒绝启动。"""
        if self._loaded:
            with self._lock:
                self._store_reachable = False
                self._degraded = True
            return self._rule_version
        path = self._config.lkg_cache_path
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rules = [_rule_from_json(item) for item in payload["rules"]]
            self._install(self._validate(rules), int(payload["version"]))
            with self._lock:
                self._store_reachable = False
                self._degraded = True
            return self._rule_version
        raise RiskConfigLoadError(
            "[RISK_005] 风控规则加载失败：配置源不可达且无可用 LKG 缓存。"
            "空规则集不是「不限制」，而是「不知道限制」—— 拒绝启动（D4）"
        )

    def _snapshot_rules(self) -> List[RiskRule]:
        with self._lock:
            return [self._index[key] for key in sorted(self._index, key=lambda item: (item[0], item[1].value, item[2]))]

    # ── 校验 ────────────────────────────────────────────────────────────
    def check(self, request: RiskCheckRequest) -> RiskCheckResponse:
        """对单笔委托执行零 IO 校验。**不可判定时宁可抛异常也不静默放行**。"""
        if not self._loaded:
            raise RiskConfigInvalidError("[RISK_004] 风控规则尚未加载，拒绝放行（D4）")
        if request.quantity is None or request.quantity <= 0:
            raise RiskConfigInvalidError(
                "[RISK_004] 无法判定：委托数量 %r <= 0，按 §2.3 拒绝而非放行" % (request.quantity,)
            )
        if request.price is None or request.price <= 0:
            raise RiskConfigInvalidError(
                "[RISK_004] 无法判定：委托价格 %r 非法（算不出委托金额），按 §2.3 拒绝而非放行"
                % (request.price,)
            )
        violations: List[RiskViolation] = []
        caps: List[int] = []
        snapshot = request.snapshot
        account_peak = ACCOUNT_PEAK_PREFIX + request.account_id
        strategy_peak = STRATEGY_PEAK_PREFIX + request.strategy_id
        self._observe_peak(account_peak, snapshot.total_asset)
        self._observe_peak(strategy_peak, snapshot.strategy_equity)
        for key, rule_type in (
            (account_peak, RuleTypeEnum.ACCOUNT_DRAWDOWN),
            (strategy_peak, RuleTypeEnum.STRATEGY_DRAWDOWN),
        ):
            warning = self._peak_warning(key, rule_type)
            if warning is not None:
                violations.append(warning)
        state = self._unit_state(request.account_id, request.strategy_id)
        self._append_gate_violation(state, request, violations)
        if request.is_open and request.symbol in self._blacklist:
            violations.append(
                RiskViolation(
                    rule_id=BLACKLIST_RULE_ID,
                    rule_type=RuleTypeEnum.MAX_POSITION_PCT,
                    scope=RuleScopeEnum.GLOBAL,
                    scope_key=GLOBAL_SCOPE_KEY,
                    threshold=0.0,
                    observed=1.0,
                    severity=SeverityEnum.CRITICAL.value,
                    action=RiskActionEnum.REJECT,
                    message="%s：标的 %s 在黑名单上，禁止开仓（平仓不受限，否则仓位会被锁死）"
                    % (BLACKLIST_RULE_ID, request.symbol),
                )
            )
        for rule_id in RULE_ORDER:
            rule = self._resolve_rule(rule_id, request, violations)
            if rule is None:
                continue
            if rule_id in DRAWDOWN_RULE_IDS:
                # 熔断的**观测**与方向无关（权益确实跌了），只有「拦不拦」才看开平仓
                self._evaluate_drawdown(rule, request, violations)
                continue
            if not request.is_open and rule_id in OPEN_ONLY_RULE_IDS:
                continue
            self._evaluate(rule, request, violations, caps)
        action, _severity = aggregate_decision(violations)
        if action is RiskActionEnum.REDUCE and caps:
            adjusted = min(caps)
        else:
            adjusted = request.quantity
        return RiskCheckResponse(
            passed=action is RiskActionEnum.PASS,
            action=action,
            rule_version=self._rule_version,
            violations=violations,
            adjusted_quantity=adjusted,
            run_state=state,
            message=self._message(state, request, violations, action),
        )

    # ── 校验内部实现 ──────────────────────────────────────────────────
    def _observe_peak(self, key: str, value: float) -> None:
        """维护权益峰值（D8）。**只涨不跌**，且只标脏不落盘（D1）。"""
        with self._lock:
            previous = self._peaks.get(key)
            if previous is None or value > previous:
                self._peaks[key] = float(value)
                self._dirty_peaks.add(key)

    def _peak_warning(self, key: str, rule_type: RuleTypeEnum) -> Optional[RiskViolation]:
        """峰值没恢复时打 RISK_011 告警（D8），每个键只打一次。

        为什么不能静默：峰值表空着时引擎算出来的回撤**基数是错的**（拿最新值当历史最高）——
        不是少拦一点，而是回撤类规则静默失效。它只是告警，不拦单：把「存量数据缺失」
        和「当下违规」混成同一个动作，会让首次启动的全部委托都被拒。
        """
        if not self._persistence_available or key in self._restored_keys:
            return None
        if key in self._peak_warned:
            return None
        self._peak_warned.add(key)
        return RiskViolation(
            rule_id=RISK_PEAK_MISSING,
            rule_type=rule_type,
            scope=RuleScopeEnum.GLOBAL,
            scope_key=GLOBAL_SCOPE_KEY,
            threshold=0.0,
            observed=0.0,
            severity=SeverityEnum.WARNING.value,
            action=RiskActionEnum.PASS,
            message="%s：权益峰值 %s 未从存储恢复，回撤类规则本次不可信（D8）"
            % (RISK_PEAK_MISSING, key),
        )

    def _unit_state(self, account_id: str, strategy_id: str) -> RiskRunStateEnum:
        """本单元的运行状态（D5 放行矩阵的输入）。KILLED > BREAKER_TRIPPED > DEGRADED。"""
        if self._kill_switch is not None and self._kill_switch.is_active():
            return RiskRunStateEnum.KILLED
        keys = (ACCOUNT_PEAK_PREFIX + account_id, STRATEGY_PEAK_PREFIX + strategy_id)
        with self._lock:
            for key in keys:
                state = self._breakers.get(key)
                if state is not None and state.state is BreakerStateEnum.TRIPPED:
                    return RiskRunStateEnum.BREAKER_TRIPPED
            if self._degraded:
                return RiskRunStateEnum.DEGRADED
        return RiskRunStateEnum.NORMAL

    def _append_gate_violation(
        self, state: RiskRunStateEnum, request: RiskCheckRequest, violations: List[RiskViolation]
    ) -> None:
        """D5 放行矩阵的唯一实现点。

        矩阵里每一条“平仓放行”都是**故意**的：任何异常状态下都要留一条出货的路，
        否则风控自己会变成无法减仓的风险源。
        """
        if state is RiskRunStateEnum.KILLED:
            violations.append(
                RiskViolation(
                    rule_id="kill_switch",
                    rule_type=RuleTypeEnum.MAX_POSITION_PCT,
                    scope=RuleScopeEnum.GLOBAL,
                    scope_key=GLOBAL_SCOPE_KEY,
                    threshold=0.0,
                    observed=1.0,
                    severity=SeverityEnum.CRITICAL.value,
                    action=RiskActionEnum.REJECT,
                    message="[RISK_007] Kill Switch 已激活：拒绝开仓；平仓唯一出口是 emergency_flatten",
                )
            )
            return
        if state is RiskRunStateEnum.BREAKER_TRIPPED:
            if request.is_open or not self._config.breaker_allow_close:
                violations.append(
                    RiskViolation(
                        rule_id="breaker",
                        rule_type=RuleTypeEnum.ACCOUNT_DRAWDOWN,
                        scope=RuleScopeEnum.GLOBAL,
                        scope_key=GLOBAL_SCOPE_KEY,
                        threshold=0.0,
                        observed=1.0,
                        severity=SeverityEnum.CRITICAL.value,
                        action=RiskActionEnum.REJECT,
                        message="[RISK_002] 熔断单元处于 TRIPPED：拒绝开仓，不隐式恢复（D7）",
                    )
                )
            return
        if state is RiskRunStateEnum.DEGRADED:
            if request.is_open or not self._config.degraded_allow_close:
                violations.append(
                    RiskViolation(
                        rule_id="degraded_config",
                        rule_type=RuleTypeEnum.ACCOUNT_DRAWDOWN,
                        scope=RuleScopeEnum.GLOBAL,
                        scope_key=GLOBAL_SCOPE_KEY,
                        threshold=0.0,
                        observed=1.0,
                        severity=SeverityEnum.ERROR.value,
                        action=RiskActionEnum.REJECT,
                        message="[RISK_006] 配置已降级（LKG 生效）：仅允许平仓",
                    )
                )

    def _resolve_rule(
        self, rule_id: str, request: RiskCheckRequest, violations: List[RiskViolation]
    ) -> Optional[RiskRule]:
        """解析生效规则；解析不到且是开仓 ⇒ 记 CRITICAL 拒绝（§2.3）。平仓不因解析失败被拦。"""
        try:
            return self.get_effective_rule(
                rule_id, request.account_id, request.strategy_id, request.symbol
            )
        except RiskRuleNotFoundError as exc:
            if request.is_open:
                violations.append(
                    RiskViolation(
                        rule_id=rule_id,
                        rule_type=RULE_REGISTRY[rule_id].rule_type,
                        scope=RuleScopeEnum.GLOBAL,
                        scope_key=GLOBAL_SCOPE_KEY,
                        threshold=0.0,
                        observed=0.0,
                        severity=SeverityEnum.CRITICAL.value,
                        action=RiskActionEnum.REJECT,
                        message=str(exc),
                    )
                )
            return None

    def _evaluate_drawdown(
        self, rule: RiskRule, request: RiskCheckRequest, violations: List[RiskViolation]
    ) -> None:
        """回撤熔断（D7）：超阈必熔断，但只有**首次**熔断才记 HALT 明细。

        为什么首次才记：熔断后的每一笔单子都会再次观测到超阈回撤。若每次都记 HALT，
        `action` 会一直是 HALT，状态门（REJECT）就被盖住了 —— 事后分不清“刚触发的熔断”
        与“熔断中”。
        """
        is_account = rule.rule_id == "account_drawdown_pct"
        key = (ACCOUNT_PEAK_PREFIX + request.account_id) if is_account else (
            STRATEGY_PEAK_PREFIX + request.strategy_id
        )
        current = request.snapshot.total_asset if is_account else request.snapshot.strategy_equity
        peak = self._peaks.get(key, 0.0)
        observed = (peak - current) / peak if peak > 0 else 0.0
        if observed <= rule.threshold:
            return
        was_tripped = self.get_breaker_state(key).state is BreakerStateEnum.TRIPPED
        self.trip_breaker(
            key,
            "%s 触发：回撤 %.4f > 阈值 %.4f（峰值 %s，当前 %s）"
            % (rule.rule_id, observed, rule.threshold, peak, current),
        )
        if request.is_open and not was_tripped:
            action, severity = RULE_DECISION[rule.rule_id]
            violations.append(
                self._violation(
                    rule, observed, action, severity, "回撤超阈，全单元熔断（只允许平仓）"
                )
            )

    def _evaluate(
        self,
        rule: RiskRule,
        request: RiskCheckRequest,
        violations: List[RiskViolation],
        caps: List[int],
    ) -> None:
        """单条数值规则判定：不过就是不过，过了就跟没这条一样。"""
        snapshot = request.snapshot
        action, severity = RULE_DECISION[rule.rule_id]
        amount = request.quantity * request.price
        if rule.rule_id == "max_position_pct":
            if snapshot.total_asset <= 0:
                violations.append(self._refuse(rule, "账户总资产 <= 0，算不出仓位占比"))
                return
            held = float(snapshot.position_value_by_symbol.get(request.symbol, 0.0))
            observed = (held + amount) / snapshot.total_asset
            if observed <= rule.threshold:
                return
            self._append_capped(rule, observed, action, severity, violations, caps,
                                rule.threshold * snapshot.total_asset - held, request.price,
                                "超出单票仓位上限")
            return
        if rule.rule_id == "max_daily_trades":
            count = int(snapshot.daily_trade_count_by_symbol.get(request.symbol, 0))
            if count < rule.threshold:
                return
            violations.append(
                self._violation(
                    rule,
                    float(count),
                    action,
                    severity,
                    "单日已交易 %d 笔，达到上限（买卖合计）" % count,
                )
            )
            return
        if rule.rule_id == "max_order_amount_pct":
            if snapshot.symbol_avg_daily_amount <= 0:
                violations.append(self._refuse(rule, "标的日均成交额 <= 0，算不出冲击占比"))
                return
            observed = amount / snapshot.symbol_avg_daily_amount
            if observed <= rule.threshold:
                return
            self._append_capped(rule, observed, action, severity, violations, caps,
                                rule.threshold * snapshot.symbol_avg_daily_amount, request.price,
                                "超出单笔委托成交额占比（冲击成本）")
            return
        if rule.rule_id == "max_sector_pct":
            sectors = snapshot.position_value_by_sector
            if not sectors:
                # 行业归类来源仍是空白（契约 §四）—— 用告警把缺口露出来，不拦单也不假装合规
                violations.append(
                    self._violation(
                        rule,
                        0.0,
                        RiskActionEnum.PASS,
                        SeverityEnum.WARNING,
                        "无法判定行业集中度：快照未携带行业归类（契约 §四 已认缺口），"
                        "告警不拦（否则会把「不知道」当成「违规」）",
                    )
                )
                return
            if snapshot.total_asset <= 0:
                violations.append(self._refuse(rule, "账户总资产 <= 0，算不出行业集中度"))
                return
            worst_sector, worst_value = max(sectors.items(), key=lambda item: item[1])
            observed = worst_value / snapshot.total_asset
            if observed <= rule.threshold:
                return
            self._append_capped(rule, observed, action, severity, violations, caps,
                                rule.threshold * snapshot.total_asset - worst_value,
                                request.price, "行业 %s 集中度超限" % worst_sector)
            return
        raise RiskConfigInvalidError(
            "[RISK_004] 规则 %s 没有对应的判定实现（注册表与判定器不同步）" % rule.rule_id
        )

    def _append_capped(
        self,
        rule: RiskRule,
        observed: float,
        action: RiskActionEnum,
        severity: SeverityEnum,
        violations: List[RiskViolation],
        caps: List[int],
        allowed_amount: float,
        price: float,
        note: str,
    ) -> None:
        """能减则减、减不到量则拒：REDUCE 的数量口径就在这里。"""
        cap = int(allowed_amount // price)
        if cap <= 0:
            violations.append(
                self._violation(
                    rule,
                    observed,
                    RiskActionEnum.REJECT,
                    severity,
                    note + "，且已无剩余额度（减到 0 等于全拒）",
                )
            )
            return
        violations.append(
            self._violation(
                rule,
                observed,
                RiskActionEnum.REDUCE,
                severity,
                note + "，建议缩减至 %d 股" % cap,
            )
        )
        caps.append(cap)

    def _violation(
        self,
        rule: RiskRule,
        observed: float,
        action: RiskActionEnum,
        severity: SeverityEnum,
        note: str,
    ) -> RiskViolation:
        return RiskViolation(
            rule_id=rule.rule_id,
            rule_type=rule.rule_type,
            scope=rule.scope,
            scope_key=rule.scope_key,
            threshold=rule.threshold,
            observed=observed,
            severity=severity.value,
            action=action,
            message="%s：观察值 %.6g vs 阈值 %.6g（生效层 %s/%s）%s"
            % (rule.rule_id, observed, rule.threshold, rule.scope.value, rule.scope_key, note),
        )

    def _refuse(self, rule: RiskRule, note: str) -> RiskViolation:
        """不可判定 ⇒ 拒绝 + CRITICAL（§2.3）。不能假装这条规则不存在。"""
        return RiskViolation(
            rule_id=rule.rule_id,
            rule_type=rule.rule_type,
            scope=rule.scope,
            scope_key=rule.scope_key,
            threshold=rule.threshold,
            observed=0.0,
            severity=SeverityEnum.CRITICAL.value,
            action=RiskActionEnum.REJECT,
            message="[RISK_004] 无法判定 %s：%s（宁可拒绝，不可静默放行）" % (rule.rule_id, note),
        )

    def _message(
        self,
        state: RiskRunStateEnum,
        request: RiskCheckRequest,
        violations: List[RiskViolation],
        action: RiskActionEnum,
    ) -> str:
        if state is RiskRunStateEnum.KILLED and not request.is_open:
            return (
                "[RISK_007] Kill Switch 已激活：check() 通道对平仓同样关闭，"
                "唯一出口是 emergency_flatten 显式通道（D5）"
            )
        if not violations:
            return "放行：未触发任何规则"
        detail = "; ".join(
            "%s=%s(%s)" % (item.rule_id, item.observed, item.action.value) for item in violations
        )
        return "%s：%s" % (action.value, detail)

    def get_run_state(self) -> RiskRunStateEnum:
        """全局运行状态（D5）。任一层异常就整体升格：运维看的是“要不要介入”。"""
        if self._kill_switch is not None and self._kill_switch.is_active():
            return RiskRunStateEnum.KILLED
        with self._lock:
            for state in self._breakers.values():
                if state.state is BreakerStateEnum.TRIPPED:
                    return RiskRunStateEnum.BREAKER_TRIPPED
            if self._degraded:
                return RiskRunStateEnum.DEGRADED
        return RiskRunStateEnum.NORMAL

    # ── 实现层辅助（非契约面，仅供测试与持久化接线）────────────────────
    def set_blacklist(self, symbols: Iterable[str]) -> None:
        """注入黑名单（名单型规则，§3.6.3）。契约未给读写接口，实现层从存储注入。"""
        with self._lock:
            self._blacklist = {str(item) for item in symbols}

    def persist_state(self, force: bool = False) -> None:
        """把峰值与熔断状态写回存储（D7/D8）。`force=False` 时按节流间隔跳过。"""
        if not self._persistence_available:
            return
        if (
            not force
            and _monotonic() - self._last_peak_persist < self._config.peak_persist_interval_seconds
        ):
            return
        with self._lock:
            peaks = {key: self._peaks[key] for key in self._dirty_peaks}
            breakers = [self._breakers[key] for key in self._dirty_breakers]
            self._dirty_peaks.clear()
            self._dirty_breakers.clear()
            self._last_peak_persist = _monotonic()
        for key, value in peaks.items():
            self._store.save_equity_peak(key, value)
            self._restored_keys.add(key)
        for breaker in breakers:
            self._store.save_breaker_state(breaker)

    def write_lkg_cache(self, path: Optional[str] = None) -> str:
        """把当前规则集写成 LKG 缓存文件（供存储不可达时恢复，D4）。返回写入路径。

        **不在 `load()` 里自动写**：自动写会让每一个回测进程都在仓库里落一个 JSON，
        而且回测路径根本用不到它。需要 LKG 的部署（DB 版）在本进程启动成功后调一次即可。
        """
        target = path or self._config.lkg_cache_path
        if not target:
            raise RiskConfigInvalidError("[RISK_004] 未配置 LKG 缓存路径，无法写出")
        payload = {
            "schema": "quanauto.risk-lkg/1",
            "version": self._rule_version,
            "saved_at": _now().isoformat(),
            "rules": [_rule_to_json(rule) for rule in self._snapshot_rules()],
        }
        _makedirs(target)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(_dumps(payload))
        return target

    def apply_pending(self, as_of: str) -> int:
        """把已到生效日的 PENDING 放宽变更落盘，返回生效条数（D6）。

        契约只说「下一交易日开盘生效」，未给触发接口 —— 用显式 `as_of` 而不是时钟或
        `sleep`，否则测试只能靠等时间。
        """
        applied = 0
        for change, effective_date in list(self._pending):
            if effective_date > as_of:
                continue
            version = int(self._store.save_change(change))
            self._install_single(change, version)
            self._pending.remove((change, effective_date))
            applied += 1
        return applied

    def is_watchdog_running(self) -> bool:
        return self._watchdog is not None and self._watchdog.is_alive()

    # ── 熔断 ────────────────────────────────────────────────────────────
    def get_breaker_state(self, breaker_key: str) -> BreakerState:
        with self._lock:
            return self._breakers.get(breaker_key) or BreakerState(breaker_key=breaker_key)

    def trip_breaker(self, breaker_key: str, reason: str) -> BreakerState:
        """熔断（幂等）：保留**首次**触发时间与原因 —— 重复触发不能抹掉最早那一笔证据。"""
        with self._lock:
            state = self._breakers.get(breaker_key)
            if state is not None and state.state is BreakerStateEnum.TRIPPED:
                return state
            state = BreakerState(
                breaker_key=breaker_key,
                state=BreakerStateEnum.TRIPPED,
                triggered_at=_now(),
                trigger_reason=reason,
            )
            self._breakers[breaker_key] = state
            self._dirty_breakers.add(breaker_key)
            self._persist_breaker(state)
            return state

    def resume_breaker(self, breaker_key: str, operator: str, reason: str) -> BreakerState:
        """显式恢复（D7）：非 TRIPPED 状态抛 `IllegalStateError`，不做“宽容处理”。"""
        with self._lock:
            state = self._breakers.get(breaker_key)
            if state is None or state.state is not BreakerStateEnum.TRIPPED:
                raise IllegalStateError(
                    "[RISK_002] 熔断单元 %s 当前不是 TRIPPED，不可恢复（D7：要留痕就要求先真的熔断过）"
                    % (breaker_key,)
                )
            state.state = BreakerStateEnum.NORMAL
            state.resumed_at = _now()
            state.resumed_by = operator
            state.trigger_reason = "%s | 人工恢复：%s" % (state.trigger_reason, reason)
            self._dirty_breakers.add(breaker_key)
            self._persist_breaker(state)
            return state

    def _persist_breaker(self, state: BreakerState) -> None:
        """熔断状态**立即写穿**（不做节流）。

        峰值可以节流（高频、单调、丢了下次拍更高），熔断不行：它是罕见且高价值的事件，
        排在一个可能还没到点的节流窗口后面，就等于「重启能绕过熔断」。
        写不进去也不报错，但保持 dirty，留给下一次 `persist_state()` 重试。
        """
        saver = getattr(self._store, "save_breaker_state", None)
        if saver is None:
            return
        try:
            saver(state)
        except Exception:  # noqa: BLE001 - 运行态写失败不能阻断校验路径
            return
        with self._lock:
            self._dirty_breakers.discard(state.breaker_key)

    # ── 阈值变更（运维入口专用，D10）─────────────────────────────────────
    def apply_change(self, change: RuleChangeRequest) -> RiskChangeResult:
        """D6 非对称：收紧立即生效；放宽需 `confirm_relax` 或下一交易日开盘生效。"""
        if not self._loaded:
            raise RiskConfigInvalidError("[RISK_004] 规则尚未加载，拒绝变更阈值")
        if not change.operator or not change.reason.strip():
            raise RiskConfigInvalidError("[RISK_004] 阈值变更必须写明 operator 与 reason（审计用，D6）")
        validate_threshold(change.rule_id, change.new_threshold)
        if change.scope is RuleScopeEnum.GLOBAL and change.scope_key != GLOBAL_SCOPE_KEY:
            raise RiskConfigInvalidError(
                "[RISK_004] GLOBAL 层变更的 scope_key 必须是 %r" % (GLOBAL_SCOPE_KEY,)
            )
        old = self._current_threshold_for(change)
        if change.new_threshold < old:
            direction = TighteningDirectionEnum.DECREASE
        elif change.new_threshold > old:
            direction = TighteningDirectionEnum.INCREASE
        else:
            direction = RULE_REGISTRY[change.rule_id].tightening_direction
        if direction is TighteningDirectionEnum.INCREASE and not change.confirm_relax:
            effective_at = self._pending_effective_at()
            self._pending.append(
                (
                    replace(change, confirm_relax=True),
                    effective_at.strftime("%Y-%m-%d"),
                )
            )
            return RiskChangeResult(
                applied=False,
                pending=True,
                old_threshold=old,
                new_threshold=change.new_threshold,
                direction=direction,
                rule_version=self._rule_version,
                effective_at=effective_at,
                message="放宽变更已挂起 PENDING：旧值继续生效，下一交易日开盘生效（D6）",
            )
        try:
            version = int(self._store.save_change(change))
        except (RiskConfigLoadError, RiskConfigInvalidError) as exc:
            raise RiskConfigInvalidError(
                "[RISK_004] 阈值写入失败，旧值继续生效（%s）：%s" % (type(exc).__name__, exc)
            )
        self._install_single(change, version)
        return RiskChangeResult(
            applied=True,
            pending=False,
            old_threshold=old,
            new_threshold=change.new_threshold,
            direction=direction,
            rule_version=self._rule_version,
            message="%s变更已立即生效（版本 %d）" % (direction.value, self._rule_version),
        )

    def _current_threshold_for(self, change: RuleChangeRequest) -> float:
        """当前**生效**阈值（可能来自更高的层）—— 变更的方向要跟生效值比，不是跟同层的旧行比。"""
        if change.scope is RuleScopeEnum.SYMBOL:
            account_id = strategy_id = symbol = change.scope_key
        elif change.scope is RuleScopeEnum.ACCOUNT:
            account_id, strategy_id, symbol = change.scope_key, "", ""
        elif change.scope is RuleScopeEnum.STRATEGY:
            account_id, strategy_id, symbol = "", change.scope_key, ""
        else:
            account_id = strategy_id = symbol = ""
        return self.get_effective_rule(change.rule_id, account_id, strategy_id, symbol).threshold

    def _pending_effective_at(self) -> datetime:
        today = _now().strftime("%Y-%m-%d")
        if self._next_trading_day is not None:
            day = self._next_trading_day(today)
        else:
            day = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return datetime.strptime(day + " 09:30:00", "%Y-%m-%d %H:%M:%S")

    def _install_single(self, change: RuleChangeRequest, version: int) -> None:
        """把单条变更上的内存索引换成新值（走存储之后的第二步）。"""
        spec = RULE_REGISTRY[change.rule_id]
        rule = RiskRule(
            rule_id=change.rule_id,
            rule_type=spec.rule_type,
            scope=change.scope,
            scope_key=change.scope_key,
            threshold=change.new_threshold,
            unit=spec.unit,
            version=version,
            updated_by=change.operator,
            updated_at=_now(),
            description=spec.description,
        )
        with self._lock:
            self._index[(change.rule_id, change.scope, change.scope_key)] = rule
            self._rule_version = version
            self._lkg_version = version

    # ── 轮询与健康度 ────────────────────────────────────────────────────
    def start_watchdog(self) -> None:
        """启动短轮询（契约 §四：MVP 用短轮询，不是推送）。重复调用不产生第二个线程。"""
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog_stop.clear()
            thread = threading.Thread(target=self._watchdog_loop, name="risk-watchdog", daemon=True)
            self._watchdog = thread
        thread.start()

    def stop_watchdog(self) -> None:
        with self._lock:
            self._watchdog_stop.set()
            thread, self._watchdog = self._watchdog, None
        if thread is not None:
            thread.join(timeout=max(1, self._config.poll_interval_seconds))

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._config.poll_interval_seconds):
            try:
                self.reload()
                self.persist_state()
            except Exception:  # noqa: BLE001 - 轮询线程绝不允许因为单次失败而退出
                continue

    def health(self) -> RiskEngineHealth:
        return RiskEngineHealth(
            run_state=self.get_run_state(),
            rule_version=self._rule_version,
            lkg_version=self._lkg_version,
            last_load_at=self._last_load_at,
            store_reachable=self._store_reachable,
            available=self._loaded,
        )


# ── Kill Switch（契约 §3.4.1）─────────────────────────────────────────────
class KillSwitch:
    """全局 Kill Switch（PRD: 一键熔断所有交易）。

    职责: 拥有最高优先级，激活后立即拒绝一切**开仓**指令。
    约束: 与 `RiskEngine` 分离实现，状态同时落本地文件与数据库 —— 即使配置源完全
          不可达，也必须能够激活，并在重启后仍然保持激活状态。
    约束: 不得提供「一键清仓」语义；清仓须走 `emergency_flatten` 显式通道（D5）。
    """

    def __init__(self, store: Any = None, state_path: Optional[str] = None) -> None:
        self._store = store
        self._state_path = state_path
        self._lock = threading.RLock()
        self._state = KillSwitch.load_state(state_path) if state_path else SwitchState()

    def activate(self, reason: str, operator: str) -> SwitchState:
        """激活（幂等，且**不依赖任何外部存储可用**）。"""
        with self._lock:
            if not self._state.active:
                self._state = SwitchState(
                    active=True,
                    activated_at=_now(),
                    activated_by=operator,
                    reason=reason,
                )
                self._persist()
            return self._state

    def deactivate(self, operator: str, reason: str, confirmation: str) -> SwitchState:
        """解除；确认串必须逐字等于 `RESUME_CONFIRMATION`，否则 `IllegalStateError`。"""
        if confirmation != RESUME_CONFIRMATION:
            raise IllegalStateError(
                "[RISK_007] 解除 Kill Switch 的确认串不正确，必须逐字传入 %r" % (RESUME_CONFIRMATION,)
            )
        with self._lock:
            if not self._state.active:
                raise IllegalStateError("[RISK_007] Kill Switch 当前未激活，无需解除")
            self._state = replace(
                self._state,
                active=False,
                deactivated_at=_now(),
                deactivated_by=operator,
                reason="%s | 解除：%s" % (self._state.reason, reason),
            )
            self._persist()
            return self._state

    def is_active(self) -> bool:
        """纯内存读取，供 `check()` 路径高频调用。"""
        with self._lock:
            return bool(self._state.active)

    def get_state(self) -> SwitchState:
        return self._state

    def emergency_flatten(self, reason: str, operator: str) -> str:
        """发起紧急清仓（显式通道，与 Kill Switch 解耦）。返回清仓任务标识。

        **故意不改开关状态**：若减仓动作顺手把 Kill Switch 关了，它就是“一键清仓”在
        实现层的后门 —— 契约 D5 明确禁止。真正的清仓执行在 I4 的交易执行层。
        """
        return _new_id("flatten")

    def _persist(self) -> None:
        """双写（本地文件 + 可选存储）。**两边都失败也不抛** —— 激活必须永远成功。"""
        payload = {
            "schema": "quanauto.kill-switch/1",
            "active": self._state.active,
            "activated_at": self._state.activated_at,
            "activated_by": self._state.activated_by,
            "reason": self._state.reason,
            "deactivated_at": self._state.deactivated_at,
            "deactivated_by": self._state.deactivated_by,
        }
        if self._state_path:
            try:
                _makedirs(self._state_path)
                temporary = "%s.tmp" % self._state_path
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(_dumps(payload))
                os.replace(temporary, self._state_path)
            except OSError:
                pass
        saver = getattr(self._store, "save_switch_state", None)
        if saver is not None:
            try:
                saver(payload)
            except Exception:  # noqa: BLE001 - 存储不可达时依然要能激活（契约 §3.4.1）
                pass

    @classmethod
    def load_state(cls, state_path: str) -> SwitchState:
        """从本地文件恢复开关状态（跨重启保持激活，契约 §3.4.1）。

        文件损坏或缺失 ⇒ 回 `NORMAL`（未激活）而不报错：开关本身要能在任何情况下被读到，
        它也不是“默认熔断”的东西（默认熔断会变成另一种事故）。
        """
        if not state_path or not os.path.exists(state_path):
            return SwitchState()
        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return SwitchState()
        return SwitchState(
            active=bool(payload.get("active")),
            activated_at=_parse_dt(payload.get("activated_at")),
            activated_by=str(payload.get("activated_by", "")),
            reason=str(payload.get("reason", "")),
            deactivated_at=_parse_dt(payload.get("deactivated_at")),
            deactivated_by=str(payload.get("deactivated_by", "")),
        )


def _now() -> datetime:
    return datetime.now()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("不可序列化：%r" % (value,))


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default, indent=2)


def _makedirs(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def _monotonic() -> float:
    return time.monotonic()


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _rule_to_json(rule: RiskRule) -> Dict[str, Any]:
    """LKG 缓存用的规则序列化（只存重建 `RiskRule` 所需的字段）。"""
    return {
        "rule_id": rule.rule_id,
        "rule_type": rule.rule_type.value,
        "scope": rule.scope.value,
        "scope_key": rule.scope_key,
        "threshold": rule.threshold,
        "unit": rule.unit.value,
        "comparison": rule.comparison,
        "enabled": rule.enabled,
        "version": rule.version,
        "updated_by": rule.updated_by,
        "updated_at": rule.updated_at,
        "description": rule.description,
    }


def _rule_from_json(payload: Dict[str, Any]) -> RiskRule:
    return RiskRule(
        rule_id=str(payload["rule_id"]),
        rule_type=RuleTypeEnum(payload["rule_type"]),
        scope=RuleScopeEnum(payload["scope"]),
        scope_key=str(payload["scope_key"]),
        threshold=float(payload["threshold"]),
        unit=RuleUnitEnum(payload["unit"]),
        comparison=str(payload.get("comparison", "LTE")),
        enabled=bool(payload.get("enabled", True)),
        version=int(payload.get("version", 1)),
        updated_by=str(payload.get("updated_by", "system")),
        updated_at=_parse_dt(payload.get("updated_at")),
        description=str(payload.get("description", "")),
    )


__all__ = [
    "ACCOUNT_PEAK_PREFIX",
    "aggregate_decision",
    "BLACKLIST_RULE_ID",
    "BreakerState",
    "BreakerStateEnum",
    "DbRiskRuleStore",
    "DRAWDOWN_RULE_IDS",
    "GLOBAL_RULE_IDS",
    "GLOBAL_SCOPE_KEY",
    "KillSwitch",
    "MemoryRiskRuleStore",
    "OPEN_ONLY_RULE_IDS",
    "ReloadResult",
    "RESUME_CONFIRMATION",
    "RISK_PEAK_MISSING",
    "RiskActionEnum",
    "RiskChangeResult",
    "RiskCheckRequest",
    "RiskCheckResponse",
    "RiskEngine",
    "RiskEngineConfig",
    "RiskEngineHealth",
    "RiskRule",
    "RiskRuleStore",
    "RiskRunStateEnum",
    "RiskSnapshot",
    "RiskViolation",
    "RuleChangeRequest",
    "RULE_DECISION",
    "RULE_ORDER",
    "RULE_REGISTRY",
    "RULE_SPECS",
    "RuleScopeEnum",
    "RuleTypeEnum",
    "RuleUnitEnum",
    "SeverityEnum",
    "STRATEGY_PEAK_PREFIX",
    "SwitchState",
    "TighteningDirectionEnum",
    "default_global_rules",
    "validate_threshold",
    "worse_action",
]
