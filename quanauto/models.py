"""数据类 —— 契约里 `@dataclass` 那些块的逐字落地。

这个模块是**签名门禁的主战场**：`tools/verify_contract_signature.py` 会把契约 python 块
里的 `@dataclass` 字段列表（名字、注解文本、有没有默认值）和这里逐个比对。所以：

* 字段**顺序**有意义（dataclass 的位置参数就是按顺序来的），别为了好看重排。
* 注解**文本**有意义（门禁比的是 `ast.unparse` 出来的原文），别把 `List[Trade]` 改成
  `list[Trade]`，哪怕语义一样 —— 那会让门禁红，而你要改的是门禁还是契约得先想清楚。
* 契约只引用、没定义的字段类型（`OrderType` / `EquityPoint` / `PerformanceMetrics` …
  这批是 B7 的 T2 未定义类型）在这里补了最小定义，并登记在
  `tools/contract-signature-manifest.json` 的 `local_types` 里说明理由。门禁会强制
  「代码里每个类要么对得上契约块、要么在 local_types 里登记过」，所以这里不可能偷偷
  多出一个没人管的类型。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, NewType, Optional

from .enums import (
    BacktestStatus,
    CapitalAllocation,
    Direction,
    DirectionEnum,
    OrderStatus,
    OrderType,
    PositionDirection,
    SignalTypeEnum,
    StrategyStatusEnum,
)

# ── 类型别名 ──────────────────────────────────────────────────────────────
# 契约写的是 `submit_order(order) -> OrderId`。用 NewType 而不是 `OrderId = str`：
# 静态检查器会把它当成独立类型，「订单 ID 和股票代码互串」这种错就不再靠人眼。
OrderId = NewType("OrderId", str)


# ── 行情数据 ──────────────────────────────────────────────────────────────
@dataclass
class BarData:
    """单根 K 线。注意契约 §2.1.2 的 `BarData` **要求** `amount` 与 `timestamp`，
    尽管 §2.2.2 自己的 `CsvDataFeed` 示例没写这两个字段 —— 以 §2.1.2 为准。"""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    datetime: datetime
    timestamp: int


@dataclass
class MarketDataBundle:
    """交给 `Strategy.on_data()` 的数据包 —— 比 `BarData` 多了复权与前收。"""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    datetime: datetime
    timestamp: int
    adjust_factor: float
    previous_close: float
    turn_rate: Optional[float] = None
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    is_trading_day: bool = True


# ── 策略侧 ────────────────────────────────────────────────────────────────
@dataclass
class TradingSignal:
    """策略产出的目标仓位意图。`target_quantity` 契约要求是正整数，校验放在
    `StrategyExecutionError` 那条路上（见 `strategies.MA_Cross_Strategy`）。"""

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strategy_id: str = ""
    symbol: str = ""
    direction: DirectionEnum = DirectionEnum.BUY
    signal_type: SignalTypeEnum = SignalTypeEnum.OPEN
    target_quantity: int = 0
    target_price: Optional[float] = None
    priority: int = 5
    timestamp: datetime = field(default_factory=datetime.now)
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionTarget:
    """`Strategy.get_target_positions()` 的值类型 —— 契约只给了它一个名字。"""

    symbol: str
    target_quantity: int = 0
    reason: str = ""


@dataclass
class StrategyConfig:
    """`BacktestConfig.strategy_configs` 的元素类型 —— 契约只给了它一个名字。"""

    strategy_id: str
    strategy_class: str
    params: Dict[str, Any] = field(default_factory=dict)
    capital: float = 0.0


@dataclass
class StrategyInfo:
    """策略元信息。`params_schema` 是给人看的参数说明，I1 不消费它。"""

    strategy_id: str
    strategy_name: str
    version: str
    description: str
    author: str
    created_at: datetime
    updated_at: datetime
    status: StrategyStatusEnum = StrategyStatusEnum.IDLE
    params_schema: Dict[str, Any] = field(default_factory=dict)


# ── 撮合侧 ────────────────────────────────────────────────────────────────
@dataclass
class Order:
    """委托单。字段顺序与契约 §2.4.2 逐字一致。"""

    order_id: str
    strategy_id: str
    symbol: str
    direction: Direction
    order_type: OrderType
    quantity: int
    price: Optional[float]
    stop_price: Optional[float]
    status: OrderStatus
    submit_time: datetime
    last_update_time: datetime
    filled_quantity: int
    filled_price: float
    commission: float
    slippage: float
    error_message: Optional[str]


@dataclass
class Trade:
    """成交记录。`trade_fee` 是各项费用合计，`commission` / `slippage` 是分项。"""

    trade_id: str
    order_id: str
    symbol: str
    direction: Direction
    price: float
    quantity: int
    commission: float
    slippage: float
    timestamp: datetime
    trade_fee: float


@dataclass
class Position:
    """持仓。"""

    symbol: str
    direction: PositionDirection
    quantity: int
    market_value: float
    cost_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float
    pnl_percentage: float


@dataclass
class Fill:
    """成交明细（`SimulatedBroker.get_fills()` 的返回值）。

    与 `Trade` 的区别：`Trade` 是**给策略看的**成交回报，`Fill` 是**撮合器内部**的
    逐笔成交，含滑点分项。契约没定义它，但 `get_fills()` 的返回注解指向它。
    """

    fill_id: str
    order_id: str
    symbol: str
    direction: Direction
    price: float
    quantity: int
    commission: float
    slippage: float
    timestamp: datetime


@dataclass
class Account:
    """账户快照。

    契约 §2.4.5 的那个 python 块是**单行畸形源码**（B7 的 T1 第 23 块，`ast` 解不开），
    字段照那一行文本补全 —— 这是「契约有病，实现照治」而不是「契约有病，实现随便写」。
    """

    account_id: str
    total_capital: float
    available_capital: float
    market_value: float
    frozen_capital: float
    total_pnl: float


@dataclass
class AccountSnapshot:
    """账户快照的历史点（`BacktestResult.account_history` 的元素类型）。"""

    account_id: str
    timestamp: datetime
    total_capital: float
    available_capital: float
    market_value: float
    frozen_capital: float
    total_pnl: float


# ── 费用与配置 ────────────────────────────────────────────────────────────
@dataclass
class CommissionConfig:
    """A 股交易费用配置。默认值抄契约 §2.4.6，是真实费率（万三 / 五元起步 / 千一印花税）。"""

    stock_commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.001
    transfer_fee: float = 0.00001
    handling_fee: float = 0.0000487
    stamp_tax_on_sell_only: bool = True


@dataclass
class SlippageConfig:
    """滑点配置。默认全 0 ⇒ 默认行为是「不模拟滑点」，跑出来的净值是理想值。"""

    fixed_slippage: float = 0.0
    percentage_slippage: float = 0.0
    volume_impact_factor: float = 0.0
    min_slippage: float = 0.0
    max_slippage: float = float('inf')


@dataclass
class TaxConfig:
    """税费配置 —— `BacktestConfig.tax_config` 的类型，契约只引用没定义。"""

    stamp_tax_rate: float = 0.001
    income_tax_rate: float = 0.0
    transfer_fee: float = 0.00001


@dataclass
class PositionLimit:
    """持仓限制 —— `BacktestConfig.position_limit` 的类型，契约只引用没定义。"""

    max_position_per_symbol: float = 1.0
    max_total_position: float = 1.0
    min_order_amount: float = 0.0


@dataclass
class BacktestConfig:
    """回测配置。18 个字段全部**必填**（契约没给默认值），字段顺序照抄 §2.1.2。

    注意 §2.9 的示例写的是 `commission_rate=` / `slippage=` —— 那两个字段不存在，
    裁决见契约附录 E1 与本仓库 `tools/contract-signature-manifest.json` 的
    `non_normative_blocks`。
    """

    strategy_configs: List[StrategyConfig]
    datafeeds: Dict[str, "DataFeed"]
    initial_capital: float
    start_date: datetime
    end_date: datetime
    commission_config: CommissionConfig
    slippage_config: SlippageConfig
    benchmark: str
    benchmark_datafeed: Optional["DataFeed"]
    output_path: str
    enable_checkpoint: bool
    checkpoint_interval: int
    enable_parallel: bool
    max_workers: int
    risk_free_rate: float
    tax_config: TaxConfig
    position_limit: PositionLimit
    capital_allocation: CapitalAllocation


# ── 绩效 ──────────────────────────────────────────────────────────────────
@dataclass
class EquityPoint:
    """权益曲线上的一个点（`BacktestResult.equity_curve` 的元素类型）。"""

    timestamp: datetime
    equity: float
    cash: float = 0.0
    market_value: float = 0.0
    drawdown: float = 0.0


@dataclass
class StrategyHandle:
    """引擎返回给调用方的策略句柄（契约 §2.1.1 只写了「含策略ID和初始资金」）。

    只放契约点名的那两个字段。策略参数不进这里 —— 它属于 `BacktestConfig.strategy_configs`，
    两处都存就会出现「改了这边那边还是旧的」。
    """

    strategy_id: str
    initial_capital: float


@dataclass
class ReturnMetrics:
    """收益指标（契约 §2.5.2）。字段名与顺序逐字照抄。

    契约块在每个字段后面跟了一行裸字符串（`\"\"\"float: 总收益率。\"\"\"`）。那是**表达
    式语句不是 docstring**，`ast` 里是 `Expr`，对 dataclass 没有任何作用，所以这里不抄 ——
    语义注释写在字段行尾。
    """

    total_return: float
    annual_return: float
    daily_return_mean: float
    daily_return_std: float
    cumulative_returns: List[float]
    daily_returns: List[float]


@dataclass
class RiskMetrics:
    """风险指标（契约 §2.5.3）。11 个字段逐字照抄。

    `information_ratio` / `tracking_error` / `beta` / `alpha` 四个**必须有基准序列**
    才算得出来，而 `calculate_risk(returns, risk_free_rate)` 的入参里没有基准。
    契约把没地方算的指标塞进了这个类，I1 的处理是填 `0.0` 并在方法 docstring 里
    点明「0.0 = 没有输入，不是算出来的 0」—— 比编一个基准序列诚实。
    """

    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    information_ratio: float
    tracking_error: float
    beta: float
    alpha: float
    volatility_annual: float
    value_at_risk_95: float
    value_at_risk_99: float
    conditional_var_95: float


@dataclass
class DrawdownMetrics:
    """回撤指标（契约 §2.5.4）。前 4 个必填，后 3 个有时间戳默认值 —— 照抄契约。"""

    max_drawdown: float
    max_drawdown_duration: int
    current_drawdown: float
    drawdown_curve: List[float]
    drawdown_start: Optional[datetime] = None
    drawdown_end: Optional[datetime] = None
    drawdown_recovery: Optional[datetime] = None


@dataclass
class PerformanceMetrics:
    """绩效指标汇总（`BacktestResult.performance` 的类型）。

    契约只引用没定义它，但 §2.9 的示例点名了三个字段（`total_return` /
    `max_drawdown` / `sharpe_ratio`）—— 那三个名字照抄，其余按 `PerformanceAnalyzer`
    实际算得出来的指标补齐。全部是标量；结构化指标拆在
    `ReturnMetrics` / `RiskMetrics` / `DrawdownMetrics`（契约 §2.5.2~§2.5.4 有定义）。
    """

    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    profit_loss_ratio: float = 0.0
    max_consecutive_losses: int = 0
    avg_hold_period: float = 0.0
    total_trades: int = 0
    total_commission: float = 0.0
    final_equity: float = 0.0


@dataclass
class ValidationReport:
    """数据校验报告 —— `BacktestResult.validation_report` 的类型。

    I1 不做数据完整性校验（进「本迭代不做」），但 `BacktestResult` 有这个字段，
    所以类型得存在，默认填 `None`。
    """

    is_valid: bool = True
    warnings: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    """回测结果。15 个字段照抄契约 §2.1.3。

    `performance` 与 `account_history` 由 `PerformanceAnalyzer.analyze()` 填；
    `strategy_id` / `status` / 时间戳 / `duration_ms` / `params_used` / 版本这几个
    **请求域**字段由 `BacktestEngine.run()` 盖上去 —— 分析器拿不到这些上下文。
    """

    strategy_id: str
    status: BacktestStatus
    start_time: datetime
    end_time: datetime
    duration_ms: int
    performance: PerformanceMetrics
    equity_curve: List[EquityPoint]
    trades: List[Trade]
    orders: List[Order]
    account_history: List[AccountSnapshot]
    error_message: Optional[str]
    data_version: str
    strategy_version: str
    params_used: Dict[str, Any]
    validation_report: Optional[ValidationReport]
