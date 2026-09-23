"""枚举定义。

契约里出现的每个枚举都在这里，一个不漏、一个不多 —— 编译器不会替我们记住「契约要求
`OrderStatus` 有 7 个取值」，所以那一层由 `tools/verify_contract_signature.py` 逐个成员
比对（契约块里定义的那些）或由 manifest 的 `local_types` 登记（契约只引用、没定义的那些）。

注意两件事：

1. 契约同时有 `Direction` 与 `DirectionEnum`，取值一样但用途不同：`DirectionEnum` 给
   `TradingSignal`（策略侧），`Direction` 给 `Order` / `Trade`（撮合侧）。这不是笔误，
   是契约 §2.1.2 与 §2.4.2 各写各的。所以两个都得存在，别合并 —— 合并会让字段注解与
   契约文本不一致，签名门禁立刻红。
2. 只有契约给出**规范块**的枚举（`DirectionEnum` / `SignalTypeEnum` / `StrategyStatusEnum`
   / `EventType`）才有成员级校验；`OrderType` 那几个契约只在注释里列了取值
   （`（LIMIT/MARKET/STOP/STOP_LIMIT）`），成员名照注释抄，登记在 manifest 的
   `local_types` 里说明出处。
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


# ── 契约只引用、未定义的枚举（取值抄契约注释，登记在 manifest.local_types）──
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

    **契约全文没有定义这个枚举**，但 §2.2.2 的 `CsvDataFeed.get_market_status`
    直接返回 `MarketStatus.OPEN` / `MarketStatus.CLOSED`。所以「开市」的那个成员
    必须叫 `OPEN`，不能叫 `TRADING` —— 名字对不上，签名门禁就会红，而且是真红
    （照契约写出来的调用方 `MarketStatus.OPEN` 会在运行时 `AttributeError`）。
    `SUSPENDED` / `HOLIDAY` 是契约**没有**用到、但语义上确实需要的成员，
    登记在 manifest 的 `local_types` 里由本地定义管辖。
    """

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    SUSPENDED = "SUSPENDED"
    HOLIDAY = "HOLIDAY"


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
