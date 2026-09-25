"""枚举定义。

契约里出现的枚举，落地处不止本文件（还有 `datacenter.py`、`risk.py`）。编译器不会替我们
记住「契约要求 `OrderStatus` 有 7 个取值」，所以那一层要门禁来管 —— 而**门禁实际管到哪一层
必须写清楚，否则这段注释自己就是假话**（2026-09-25 实测更正，分歧的完整登记见
`docs/开工前缺口清单.md` B9）：

* **成员级比对原先只覆盖主契约的附录 G 节**：`tools/verify_contract_appendix.py` 扫 G 节的每个
  `class` 声明逐项比成员名与取值（`AX-IMPL-MISMATCH`）。2026-09-25 之前，G 节里没有声明的枚举
  —— 契约正文的 `DirectionEnum` / `SignalTypeEnum` / `StrategyStatusEnum` / `EventType`，以及
  数据中心契约 §3.1.3 的 `MarketStatus` —— **一个都没有**成员级判据（`MarketStatus` 当时确实与
  契约不同，就是这条假绿的实证，见 B9.1）。现在三份契约**正文**里的枚举也逐成员对拍了：
  `tools/verify_enum_members.py`（tier-A 门禁 `enum-members`，2026-09-25 起注册）。
* `tools/verify_contract_signature.py` 的输入是 `tools/contract-signature-manifest.json` 的
  `classes`（9 个契约类）与 `impl_only`（22 个实现侧登记），**两者都不含枚举**。
* 本文件此前写「登记在 manifest 的 `local_types`」是错的：那份清单**从来没有**这个键
  （顶层键只有 `schema` / `contract` / `classes` / `impl_only` / `non_normative_blocks` /
  `note`）。精确判据：`git log -S local_types -- tools/contract-signature-manifest.json`
  全历史 0 命中（该文件的 5 个历史版本逐个查过，都没有这个键）；**不加路径**的
  `git log -S local_types` 会命中 2 个提交（`7c9f0a5` / `14dd83e`），但那两次改的是
  `quanauto/*.py` 里**说**它登记在那儿的句子 —— 是描述这个键的文字，不是这个键。
  实现侧本地类型的真实登记处是 `impl_only`（例：`SessionMode`）。
* 实测分母（2026-09-25）：三份契约共声明 23 个枚举，实现侧 23 个（比对 21 个）；
  `DataQualityFlag` / `ReportType` 只有契约没有实现（登记在 `PENDING`），
  `SessionMode`（本仓 `datacenter.py`）与 `SeverityEnum`（`risk.py`）只有实现没有契约
  （登记在 `IMPL_LOCAL`；`SeverityEnum` 的取值 `WARNING`/`ERROR`/`CRITICAL` 与
  `db/data_center.sql` 的 `ck_dc_quality_severity`（`INFO` 而非 `ERROR`）不同域同形，
  所以它**不能**被当成那份 DDL 约束的实现映射）。

注意两件事：

1. 契约同时有 `Direction` 与 `DirectionEnum`，取值一样但用途不同：`DirectionEnum` 给
   `TradingSignal`（策略侧），`Direction` 给 `Order` / `Trade`（撮合侧）。这不是笔误，
   是契约 §2.1.2 与 §2.4.2 各写各的。所以两个都得存在，别合并 —— 合并会让字段注解与
   契约文本不一致，签名门禁立刻红。
2. `Direction` / `OrderType` / `OrderStatus` / `PositionDirection` / `BacktestStatus` 五个，
   契约正文只在注释里列了取值（`（LIMIT/MARKET/STOP/STOP_LIMIT）` 这种），但 2026-09-25
   起主契约**附录 G3** 已给出规范块，成员名以那份规范块为准、由附录门禁逐项比对。
   （此前这里写「登记在 manifest 的 `local_types` 里说明出处」—— 那个键不存在，见上文。）
"""

from __future__ import annotations

from enum import Enum


# ── 契约给出规范块的枚举（成员由门禁逐条比对）────────────────────────────
class DirectionEnum(Enum):
    """策略侧方向 —— `TradingSignal.direction`。"""

    BUY = "BUY"
    SELL = "SELL"


class SignalTypeEnum(Enum):
    """信号类型 —— 开仓 / 平仓 / 调仓。"""

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"


class StrategyStatusEnum(Enum):
    """策略生命周期状态。"""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"


class EventType(Enum):
    """事件引擎的事件类型。

    值是**短字符串**而不是枚举名（契约 §2.3.1 写的就是 `BAR_EVENT = "bar"`），
    因为事件要能序列化进日志/检查点，`"bar"` 比 `"EventType.BAR_EVENT"` 稳。
    """

    BAR_EVENT = "bar"
    TICK_EVENT = "tick"
    ORDER_EVENT = "order"
    TRADE_EVENT = "trade"
    ACCOUNT_EVENT = "account"
    POSITION_EVENT = "position"
    STRATEGY_EVENT = "strategy"
    RISK_EVENT = "risk"
    SYSTEM_EVENT = "system"


# ── 契约正文只给注释、由主契约附录 G3 补规范块的枚举（成员由附录门禁比对）──────
class Direction(Enum):
    """撮合侧方向 —— `Order.direction` / `Trade.direction`（契约注：BUY/SELL）。"""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """订单类型（契约注：LIMIT/MARKET/STOP/STOP_LIMIT）。"""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(Enum):
    """订单状态（契约注：PENDING/SUBMITTED/PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED）。"""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionDirection(Enum):
    """持仓方向（契约注：LONG/SHORT）。"""

    LONG = "LONG"
    SHORT = "SHORT"


class BacktestStatus(Enum):
    """回测状态（契约注：SUCCESS/FAILED/PARTIAL/STOPPED）。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    STOPPED = "STOPPED"


class MarketStatus(Enum):
    """某时刻的市场状态 —— `DataFeed.get_market_status()` 的返回值。

    成员表**逐条对齐**数据中心契约 §3.1.3 的规范块（顺序也是契约顺序）：
    `PRE_OPEN` / `OPEN` / `LUNCH_BREAK` / `CLOSED` / `HALTED`。

    「开市」的那个成员必须叫 `OPEN`：主契约 §2.2.2 的 `CsvDataFeed.get_market_status`
    直接返回 `MarketStatus.OPEN` / `MarketStatus.CLOSED`，实现侧的调用点
    （`datafeed.py`、`datacenter.py`）与 `engine.py` 的 `is_trading_day` 都按这两个名字读。

    2026-09-25 更正：此前这里是 `OPEN` / `CLOSED` / `SUSPENDED` / `HOLIDAY`，与契约
    三个成员互缺 —— 当时的 13 个门禁对此**全绿**（往本文件插一个成员也全绿），因为
    唯一比对成员名的 `verify_contract_appendix.py` 只覆盖附录 G，而
    `contract-signature-manifest.json` 的 `classes` / `impl_only` 里一个枚举都没有。
    现已补齐 `tools/verify_enum_members.py`（tier-A，逐成员比对契约与实现），
    本类就是它的第一个 FINDING 对象，登记在 `docs/开工前缺口清单.md` B9 的 B9.1。
    被删掉的 `SUSPENDED` / `HOLIDAY` 全仓零引用（`git grep` 实测），删它们不动任何调用点；
    「停牌」「假日」这两层语义属于 `dc_quality_issue.flag` 与交易日历，不是市场状态。
    """

    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    LUNCH_BREAK = "LUNCH_BREAK"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


class CapitalAllocation(Enum):
    """资金分配模式 —— `BacktestConfig.capital_allocation`。

    I1 只实现 `EQUAL`（等权）。`FIXED` / `WEIGHTED` 先列上是因为字段得有类型，
    真正实现进了「本迭代不做」清单。
    """

    EQUAL = "EQUAL"
    FIXED = "FIXED"
    WEIGHTED = "WEIGHTED"


# ── I2 加入：数据中心契约（`智能量化交易平台-数据中心接口契约文档.md` §3.1）────
# 这三个是**数据中心契约给出规范块**的枚举，成员由 `tools/verify_data_center_pit.py`
# 逐条比对。取值的**顺序**照契约原样（NONE / QFQ / HFQ），不按字典序重排 ——
# 契约里那一行注释（"前复权含未来信息，禁止用于回测"）就是 `adjust_type` 会被
# 当作 PIT 判据的原因，重排会让 diff 读起来像改了语义。
class AdjustType(Enum):
    """复权类型 —— 数据中心契约 §3.1、D6。

    `QFQ` 含未来信息（前复权要用到今天的最新股本），所以**回测会话里请求 QFQ
    必须抛 `FutureDataAccessError`**；实盘展示才允许用。
    """

    NONE = "NONE"
    QFQ = "QFQ"
    HFQ = "HFQ"


class FillPolicy(Enum):
    """缺失值填充策略 —— 数据中心契约 §3.1、D7。

    `BFILL`（后向填充）用未来值回填过去，是最直白的未来函数，**生产与回测一律禁止**，
    只在离线清洗脚本里允许（且要写 `dc_quality_issue` 留痕）。所以它必须是一个
    存在的取值 —— 存在才能被显式拒绝；删掉它只会让「有人传 BFILL」变成静默的
    参数错误而不是一条能写测试的判据。
    """

    NONE = "NONE"
    FFILL = "FFILL"
    BFILL = "BFILL"


class SourcePriority(Enum):
    """数据源优先级 —— 数据中心契约 §3.1、D9（主源失败时降级到备源）。"""

    PRIMARY = "PRIMARY"
    FALLBACK = "FALLBACK"
