"""数据中心读侧（I2 S1）—— `as_of` 冻结视图 + `PITGuard` 第二层防护。

本模块只做一件事：**把「会话只能看到 `as_of_date` 当天及之前已可见的数据」变成会炸的代码。**
数据中心契约里的两处要求直接落在这里：

* §2.4 / §3.10 —— `as_of(T)` 返回的 feed 在任何调用下都不得看到 `available_date > T` 的数据；
  而**显式请求** `as_of` 之后的数据必须抛 `FutureDataAccessError`（DATA_002，CRITICAL）。
* §3.7 —— `PITGuard` 是**第二层**防护。D3 的接口形状能防住「忘了传 `as_of_date`」，
  防不住「传了 `as_of_date` 但实现里没过滤」。后者才是真会发生的 bug，必须运行时炸掉
  而不是静默返回 —— 静默返回一个未来值，回测曲线照样好看，只是假的。

## 约定 1：日线的过滤键就是 `trade_date`

按 D4，行情的 `available_date == trade_date`（收盘后可见），所以日线的 PIT 判据是
`trade_date <= as_of_date`。`db/data_center.sql` 的 `dc_daily_bar` 表**没有** `available_date`
列 —— 那不是省略，是 D4 的结论（写一列恒等于 `trade_date` 的冗余列，只会给人机会把
它填错）。财务数据才真的需要三个日期，那部分不在本切片里。

## 约定 2：越界 = 抛，不是裁剪

`as_of` 之后的数据有两种处理方式：**裁剪**（静默截断到 `as_of`）和**抛**（fail-closed）。
本模块选择：

* 带**显式日期参数**的方法（`get_bar` / `get_bars` / `get_trading_calendar` /
  `is_symbol_available` / `get_market_status` / `get_adjustment_factor` / `get_dividend`）
  —— 参数越过 `as_of_date` 一律抛 `FutureDataAccessError`。
* 不带窗口的**清单**方法（`get_available_dates` / `get_available_symbols`）—— 裁剪到 `as_of`。

理由：一个「显式的越界请求」说明调用方的时钟和会话时钟不一致（off-by-one、忘了把回测区间
裁到 `as_of`、或者干脆在写未来函数）。裁剪会把它变成一个**静默的空结果或短结果**，
而那正是最难查的一类 bug。清单方法没有"请求了哪一天"这回事，裁剪是它的自然语义。
这条界线是**本实现定的**，契约只写了「不得看到」（§3.10）与「请求了之后的数据 ⇒ 抛」（§2.4 第 189 行）；
两种读法都能自圆其说，所以在这里把选哪种、为什么写下来，而不是留给下一个人猜。

## 约定 3：`PITGuard.report()` 只返回一个 `PITReport`

契约 §3.7 的 `report() -> PITReport` 是**单数**，而一次会话有很多次访问、很多个标的 ——
形状上表达不了「全部」。这里的约定：`report()` 返回**最早一次越界**的报告（那是要修的那个
bug），没有越界时返回**最后一次访问**的报告；全部越界点由 `leakage_points()` 给。
因为 `record_access` 一越界就抛，越界列表在实践中最多一条 —— 除非调用方自己 `except`
掉继续跑，那时候 `leakage_points()` 就是唯一的现场。

## 已知缺口（写在这里，而不是散在实现里）

* **复权价已实施**（2026-09-29 晚 **Ⅱ**，即 `docs/迭代计划.md` §四 的「A6 收口 Ⅱ」那一轮；
  **不是**「I2 收口」—— I2 还开着另外几条）：`BarData` 的 OHLC 现在**真的乘了累计因子**
  —— 唯一的复权算术在模块级 `_rescale_price`，唯一的调用点是 `DbDataFeed._row_to_bar`
  ⇒ D6 第二条"复权是**读取时的视图行为**"（按 `as_of_date` 现算）**已兑现**：

  | `AdjustType` | 价格倍数 | 查库？ |
  | --- | --- | --- |
  | `NONE` | `1.0`（原样返回，连乘法都不做） | 否 |
  | `HFQ` | 该交易日**累计因子** | 是，缺行 ⇒ DATA_001 |
  | `QFQ` | 该日累计因子 ÷ **视图末日的累计因子** | 是，两边缺行都 ⇒ DATA_001 |

  `volume` / `amount` **刻意不乘因子**（这不是漏改，理由写在 `_row_to_bar` 的 docstring 里）。
  订正（历史）：本段先后写过"复权因子恒为 1.0"（2026-09-29 之前为真）、"复权价仍未实施 /
  `BarData` 的 OHLC 仍是不复权价"（2026-09-29 晚 **Ⅱ** 之前为真）—— 三句话各自的**有效期**不同，
  读旧记录时按日期取最近的那一句，别把旧的那句当现状引用。
* **因子读取是逐根 K 线一次查询**（`_row_to_bar` 每根都要问一次因子；S1 的存储是内存实现）。
  接 PG 时要改成按 `(symbol, 窗口)` 一次取回再映射；`get_available_symbols` 与
  `get_trading_calendar` 是同一族的 N 次查询，S1 起就记着这个 TODO。
* `get_dividend` 仍恒 0.0（没有分红数据源）—— **与复权是两件事**：分红缺口**没有**被本轮
  顺手"修好"，`tests/test_data_center_pit.py::test_known_gap_dividend_is_still_zero` 仍守着它。
* 因子帧的 schema 校验判据仍未实现（DC 契约附录 B21.6，`quanauto/datasources.py` 里显式报）。
* `live()` / `trading_calendar()` 未实现（`NotImplementedError`）。
* 财务 / 指数成分股 / 数据质量 / 采集幂等不在本切片。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from .datafeed import DataFeed, epoch_seconds
from .enums import AdjustType, FillPolicy, MarketStatus
from .errors import DataNotAvailableError, DataVersionError, FutureDataAccessError
from .models import BarData

# `select_bars(symbol, start, end)` 的下界哨兵：只有 `get_available_dates` 那种
# "没有下界"的清单查询会用到它。刻意不用 `date.min`（公元 1 年）—— 真拿去拼 SQL 时
# 某些库/驱动在公元 1 年上会闹别扭，1900 足够早（A 股最早的日线是 1990 年）。
MIN_DATE = date(1900, 1, 1)

# 日线以**整行**为访问单位（`BarData` 是不可分割的一行），所以 `PITGuard.record_access`
# 的 `field` 参数在这里传 "bar" 而不是某个具体列名。契约 §3.7 只规定 `field` 是
# "字段名"，没有规定日线该填什么 —— 所以这是一个**选择**，写在这里而不是散在调用点。
# 单字段读取（财务的 `revenue` / `roe` 之类）才该填真实列名。
BAR_FIELD = "bar"

# 复权因子是**单字段**读取（`dc_adjust_factor.adjust_factor` 那一列），所以这里填真实列名
# 而不是像日线那样填整行名 —— 契约 §3.7 的 `field` 参数本来就是这个意思。
FACTOR_FIELD = "adjust_factor"


class SessionMode(Enum):
    """会话模式 —— D6 与 D7 的判据来源。

    契约 §3.1 只定义了 `AdjustType` / `FillPolicy` / `SourcePriority` 三个枚举，
    **没有** `SessionMode`；但 §3.3 的 `as_of(as_of_date, adjust_type, fill_policy, data_version)`
    签名里也没有"这是回测还是实盘"的参数，而 D6 又要求「回测会话里请求 `QFQ` 抛异常」。
    说明这个模式只能在 `DataCenter` **实例**上（也就是构造时）确定。所以它属于本模块：
    它不是一条契约取值，是契约签名缺口的一处补丁，登记在
    `tools/contract-signature-manifest.json` 的 `impl_only` 里（这份清单没有 `local_types`
    这个键 —— 2026-09-25 实测更正：`git log -S local_types -- tools/contract-signature-manifest.json`
    全历史 0 命中；见 `docs/开工前缺口清单.md` B9 的 B9.3）。
    """

    BACKTEST = "BACKTEST"
    LIVE = "LIVE"


@dataclass(frozen=True)
class DailyBar:
    """归一化后的日线行 —— 列名与 `db/data_center.sql` 的 `dc_daily_bar` 逐列对应。

    **刻意不是**任何数据源的原生形状。D9 要求"上层不感知数据源差异"，所以源特有的字段名
    （`akshare` 的中文列、`baostock` 的 `code`/`tradeStatus` 等）在适配器里就被换掉了，
    到这一层已经看不见。`tools/verify_data_center_adapter.py` 会反向检查这条：
    **数据类上不得出现任何源特有字段名**。

    去掉 `ingested_at`：那是采集侧的审计信息，读侧不需要，带上只会让同一份数据在
    两次采集后"看起来不同"（而 D8 说数据版本不可变）。
    """

    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    source: str
    data_version: str


class BarStore(ABC):
    """存储这一侧的接口 —— 薄，且**必须**接受窗口。

    `select_bars` 收窗口（而不是"给我某标的的全部"）是刻意的：PGC 落库那层最终会把它编译成
    `WHERE symbol=$1 AND trade_date BETWEEN $2 AND $3`。窗口参数存在，测试才能构造一个
    **"WHERE 写错了"的存储**（忽略窗口、多吐了几行），而 `PITGuard` 的职责就是抓住它。
    如果存储接口不接受窗口，第二层防护就永远没有触发场景 —— 那会是一个测不出东西的空转守卫。
    """

    @abstractmethod
    def select_bars(self, symbol: str, start: date, end: date) -> List[DailyBar]:
        """闭区间 `[start, end]` 的行，按 `trade_date` 升序。"""
        raise NotImplementedError

    @abstractmethod
    def select_symbols(self) -> List[str]:
        """库里出现过的全部标的（升序，去重）。"""
        raise NotImplementedError


class InMemoryBarStore(BarStore):
    """内存实现 —— 给单元测试与门禁用，不依赖数据库（本机没有本地 PostgreSQL）。"""

    def __init__(self, rows: Sequence[DailyBar]):
        self.rows = list(rows)

    def select_bars(self, symbol: str, start: date, end: date) -> List[DailyBar]:
        return sorted(
            (r for r in self.rows if r.symbol == symbol and start <= r.trade_date <= end),
            key=lambda r: r.trade_date,
        )

    def select_symbols(self) -> List[str]:
        return sorted({r.symbol for r in self.rows})


@dataclass(frozen=True)
class AdjustFactorPoint:
    """一条复权因子 —— 列名与 `db/data_center.sql` 的 `dc_adjust_factor` 逐列对应。

    **刻意不是** `DailyBar` 上的一个字段：因子的主键与日线相同（`symbol` / `trade_date` /
    `data_version`），但「这一天没有因子行」是常事（停牌、非交易日、源只发变动日），
    那时正确的行为是抛 DATA_001，而**不是**给日线补一个 1.0 —— 把两者塞进同一行，
    「缺的到底是哪一半」在类型上就消失了，而判据（要不要抛）也跟着消失。

    `adjust_factor` 是**累计因子**（DC 契约 §2.3：无量纲、累乘、`> 0`；附录 B21.5：
    tushare 的 `adj_factor` 本身就是累计值，采集侧归一化但**不换算**）⇒ 后复权价 =
    不复权价 × 该交易日的累计因子，读取时**不需要**再把历史因子累乘一遍。

    注解写 `float` 是契约形状（§2.1.1 的数值字段都是 `float`），但存储层交出来的实际是
    `Decimal`（`pgstore._row_to_factor`，与 `DailyBar` 的价格列同一条缝）⇒ 收口在
    `DbDataFeed.get_adjustment_factor` 里那次 `_as_float`。
    """

    symbol: str
    trade_date: date
    adjust_factor: float
    source: str = ""
    data_version: str = ""


class FactorStore(ABC):
    """复权因子的存储接口 —— 与 `BarStore` **并列**，而不是并进它。

    为什么不把 `select_factors` 加到 `BarStore` 上：那会让每一个「只关心日线」的存储
    实现（以及用例里所有的假存储）都必须为一个它不关心的方法写桩，而**桩是探测器最爱的
    地方**——一个空实现的 `select_factors` 会让「复权因子没接」这件事看起来像
    「接上了但库里没有数据」。两个协议，各自实现，各自测。

    窗口是**闭区间**且必须接受：与 `BarStore` 同一个理由 —— 存储接口不接受窗口，
    第二层防护（`PITGuard`）就永远没有触发场景，那会是一个测不出东西的空转守卫。
    """

    @abstractmethod
    def select_factors(self, symbol: str, start: date, end: date) -> List[AdjustFactorPoint]:
        """闭区间 `[start, end]` 的因子行，按 `trade_date` 升序。"""
        raise NotImplementedError


class InMemoryFactorStore(FactorStore):
    """内存实现 —— 给单元测试与门禁用，不依赖数据库（本机没有本地 PostgreSQL）。"""

    def __init__(self, rows: Sequence[AdjustFactorPoint]):
        self.rows = list(rows)

    def select_factors(self, symbol: str, start: date, end: date) -> List[AdjustFactorPoint]:
        return sorted(
            (r for r in self.rows if r.symbol == symbol and start <= r.trade_date <= end),
            key=lambda r: r.trade_date,
        )


@dataclass
class PITReport:
    """PIT 一致性检查报告（数据中心契约 §3.4，补齐主契约 §2.7.1 里从未定义的返回类型）。"""

    symbol: str
    as_of_date: date
    field: str
    is_consistent: bool
    visible_value: Optional[float]
    latest_visible_date: Optional[date]
    message: str


@dataclass
class LeakagePoint:
    """一个未来函数泄露点（数据中心契约 §3.4，补齐主契约 §2.7.2 的缺失类型）。"""

    symbol: str
    trade_date: date
    as_of_date: date
    field: str
    reason: str
    severity: str


class PITGuard(ABC):
    """PIT 守卫（数据中心契约 §3.7）。两层防护中的第二层。

    第一层是 D3 的接口形状（`DataFeed` 只能由 `DataCenter.as_of()` 产出、9 个方法上都
    不许加 `as_of_date` 可选参数），它保证"忘传 `as_of_date`"这件事**不可能**发生。
    第二层就是这里：在具体实现内部逐行回比，把"传了但没过滤"炸掉。
    """

    @abstractmethod
    def record_access(self, symbol: str, trade_date: date, field: str) -> None:
        """记录一次数据访问，越界时抛 `FutureDataAccessError`。"""
        raise NotImplementedError

    @abstractmethod
    def report(self) -> PITReport:
        """返回本会话的 PIT 一致性报告（`is_consistent=False` 表示检测到越界）。"""
        raise NotImplementedError

    @abstractmethod
    def leakage_points(self) -> List[LeakagePoint]:
        """返回检测到的全部泄露点，供主契约的 `DataIntegrityChecker` 消费。"""
        raise NotImplementedError


class RecordingPITGuard(PITGuard):
    """`PITGuard` 的默认实现：记录 + 回比 + 越界即抛。

    刻意**不**把每次访问都存下来：一次 20 年 × 3000 标的的回测有上千万次访问，
    全存是内存事故。只留 `count` / `last_access` / `leakage`（而 `leakage` 几乎总是空的 ——
    `record_access` 一越界就抛，调用栈已经把人拦住了）。
    """

    def __init__(self, as_of_date: date):
        self.as_of_date = _as_date(as_of_date)
        self.count = 0
        self.last_access: Optional[Tuple[str, date, str]] = None
        self._leakage: List[LeakagePoint] = []

    def record_access(self, symbol: str, trade_date: date, field: str) -> None:
        when = _as_date(trade_date)
        self.count += 1
        self.last_access = (symbol, when, field)
        if when <= self.as_of_date:
            return
        point = LeakagePoint(
            symbol=symbol,
            trade_date=when,
            as_of_date=self.as_of_date,
            field=field,
            severity="CRITICAL",
            reason="访问了 available_date=%s 的 %s，而 as_of_date=%s"
            % (when, field, self.as_of_date),
        )
        self._leakage.append(point)
        # 抛，**不**静默 drop 那一行：drop 会把"上游过滤写错了"变成"今天这根 K 线恰好没有"，
        # 后者看起来完全正常，是最难查的一类 bug。
        raise FutureDataAccessError(
            "PITGuard 拦住一次越界读取：%s（DATA_002，CRITICAL —— 该会话的结果已不可信）"
            % point.reason
        )

    def report(self) -> PITReport:
        if self._leakage:
            point = self._leakage[0]
            return PITReport(
                symbol=point.symbol,
                as_of_date=self.as_of_date,
                field=point.field,
                is_consistent=False,
                visible_value=None,
                latest_visible_date=point.trade_date,
                message="检测到 %d 个泄露点；最早一处：%s" % (len(self._leakage), point.reason),
            )
        symbol, when, field = self.last_access or ("", None, "")
        return PITReport(
            symbol=symbol,
            as_of_date=self.as_of_date,
            field=field,
            is_consistent=True,
            visible_value=None,
            latest_visible_date=when,
            message="本会话 %d 次访问全部落在 as_of_date=%s 之内" % (self.count, self.as_of_date),
        )

    def leakage_points(self) -> List[LeakagePoint]:
        return list(self._leakage)


def _as_datetime(value) -> datetime:
    """`date` → 当日 00:00 的 `datetime`；`datetime` 原样返回。

    为什么要有这个转换：`DataFeed` 的 9 个方法按契约 §2.2.1 收 `datetime`，而库里的
    `dc_daily_bar.trade_date` 是 `date`。I1 的 `CsvDataFeed` 在 CSV 只给 `YYYY-MM-DD` 时
    也是落在 00:00 上，两条取数路径必须同口径 —— 否则同一天在 CSV 里和库里是两个
    不同的时间点，回测结果会随数据源不同而不同，那正是 D9 要消灭的东西。
    """
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _as_date(value) -> date:
    """`datetime` → `date`；`date` 原样返回（`datetime` 是 `date` 的子类，要先判）。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_float(value) -> float:
    """存储形状 → 契约形状：`BarData` 的数值字段是 `float`（§2.1.1）。

    存储层交出来的却是 `Decimal`（`pgstore._row_to_bar`；`tests/test_data_center_store.py`
    **特意**断言了 `isinstance(bar.close, Decimal)` —— 写侧要靠精确比较判「同值不写」）。
    不在这里收口，第一次信号计算就是 `float // Decimal` → `TypeError`，而且是在引擎深处
    被包成 `BacktestExecutionError` 抛出来。`volume` 早就转成 `int` 了，价格那五列漏了
    同一个动作 —— 这就是两个半边各自都绿、接起来跑不通的那个接缝
    （咬住它的用例：`tests/test_backtest_db_feed.py`）。
    """
    return float(value)


def _rescale_price(value: float, factor: float) -> float:
    """不复权价 → 复权价：**全仓库唯一一处复权算术**（D6 第二句「读取时现算」）。

    为什么单独一个模块级函数而不是内联的 `value * factor`：`_row_to_bar` 里要乘四列
    （open/high/low/close），内联就是四份同样的乘法；更要紧的是这条口径要**一眼可见地
    只有一份** —— 附录 B21 那句「归一化只发生在 `_row_to_bar` 一处」在这里扩成
    「归一化 + 复权都只在这一处、这一份算式」。

    `factor == 1.0` 时**原样返回**（不做乘法）：

    * 不复权路径（`AdjustType.NONE`）与 I2 A6 之前的实现**逐字节一致**，不会被一次
      `× 1.0` 引入本不存在的浮点尾巴 —— 附录 B18.3 那条 "842270400 vs 842270399.9999999"
      就是同一个家族的老祖宗（能不一样的地方就一定会不一样）；
    * 判据是**值相等**而不是 `is`，因为 `1.0` 与 `1.25 / 1.25` 都是「没有净缩放」，
      而后者在浮点下并不总是逐位等于 `1.0` —— 用 `factor == 1.0` 只挡最干净的那一种，
      不假装能挡住所有。这条**不**claim「不复权价与复权价一定逐位相等」，只 claim
      「`NONE` 走的是恒等路径」。
    """
    if factor == 1.0:
        return value
    return value * factor


class DbDataFeed(DataFeed):
    """库（PostgreSQL `dc_daily_bar`）支撑的 `DataFeed`，**绑定**一个 `as_of_date`。

    D3：这个类只能由 `DataCenter.as_of()` 产出，9 个方法上都不许出现 `as_of_date`
    可选参数 —— 忘传就变成不可能。构造时的 `as_of_date` 就是整个视图的冻结面。
    """

    def __init__(
        self,
        store: BarStore,
        as_of_date: date,
        data_version: str,
        session_mode: SessionMode = SessionMode.BACKTEST,
        adjust_type: AdjustType = AdjustType.HFQ,
        fill_policy: FillPolicy = FillPolicy.NONE,
        pit_guard: Optional[PITGuard] = None,
        factor_store: Optional[FactorStore] = None,
    ):
        self.store = store
        self.as_of_date = _as_date(as_of_date)
        self.data_version = data_version
        self.session_mode = session_mode
        self.adjust_type = adjust_type
        self.fill_policy = fill_policy
        # `factor_store` 追加在最后且带默认值：位置参数的老用法（`DbDataFeed(store, as_of, ver)`）
        # 一字不改。没有接因子源的 feed 仍然是**合法**的 —— 它只是回答不了"HFQ 的因子是多少"
        # （那时抛 DATA_001，见 `get_adjustment_factor`），而不是悄悄地按不复权跑。
        self.factor_store = factor_store
        self.pit_guard = pit_guard if pit_guard is not None else RecordingPITGuard(self.as_of_date)

    # ── 内部：把存储行变成 `BarData` ──────────────────────────────────────
    def _row_to_bar(self, row: DailyBar) -> BarData:
        """存储行 → `BarData`。**归一化与复权都只发生在这一处**（附录 B21 第 2518 行）。

        D6 第二句「复权在**读取时**现算」就落在这里：四列价格各乘一次 `_price_scale()`
        给出的倍数。这份模块里没有第二个地方做复权算术 —— 判据是"`_rescale_price` 的
        调用点只有本方法"。之所以要这么死：只要有一条读路径复了权、另一条没复，
        同一根 K 线在 `get_bar` 与 `get_available_dates` 里就是两个价，而**报告里看不见**
        （曲线照样画得出来）。

        **`volume` / `amount` 刻意不乘因子**（这是一条决定，不是漏改）：

        * D6 说的是"复权"，说的就是价格；`volume` 是**股数**、`amount` 是**成交额**
          （真金白银），两者都不是"价格"，乘一个无量纲因子没有任何金融含义；
        * 更硬的一条：`volume` 是 `int`，`int(1e6 / 1.25)` 会**截断**，而
          `is_symbol_available` 的判据正是 `bar.volume > 0` ⇒ 一个小成交量的正常交易日
          会被截成 0、**变成"不可交易"**（策略凭空少掉一天数据，且不报任何错）；
        * 附带好处：`tests/test_data_center_store.py` 里那些"库内标度 / 整数性"的断言
          不必因为复权而换口径。
        """
        stamp = _as_datetime(row.trade_date)
        factor = self._price_scale(row.symbol, row.trade_date)
        return BarData(
            symbol=row.symbol,
            open=_rescale_price(_as_float(row.open), factor),
            high=_rescale_price(_as_float(row.high), factor),
            low=_rescale_price(_as_float(row.low), factor),
            close=_rescale_price(_as_float(row.close), factor),
            volume=int(row.volume),
            amount=_as_float(row.amount),
            datetime=stamp,
            timestamp=epoch_seconds(stamp),
        )

    # ── 内部：复权口径（"乘哪个因子"只写在这里） ──────────────────────────
    def _factor_for(self, symbol: str, when: date) -> float:
        """该 (标的, 交易日) 的**累计**复权因子 —— **唯一的实现**。

        `get_adjustment_factor()`（给调用方的那个）与复权价都走这里，所以"因子是多少"
        全仓库只有一份答案。三条分支的顺序：`NONE` ⇒ 1.0 且**不查库**；`HFQ`/`QFQ` ⇒
        严格查、没有行就抛 DATA_001；本 feed 没接因子源 ⇒ 也是 DATA_001，但消息点明是
        **配置**没接上。逐条理由写在 `get_adjustment_factor` 的 docstring 里
        （那是给调用方看的契约口径，这里只管"怎么算"）。

        ⚠️ 把 `NONE` 的早退去掉、换成"查不到就退回 1.0"就是原样把缺口放回去
        （变异 `A5-hfq-factor-silently-defaults-to-one` 钉着这一条）。

        `when` 必须是**已经过 `_require_visible` 的 `date`**：本方法自己不判越界
        （越界的唯一判据是第一层，重复判会让"哪一层拒的"变模糊）。`_row_to_bar` 的调用
        天然满足这条 —— 那些行来自 `_select`，已经在第二层逐行过过守卫。
        """
        if self.adjust_type is AdjustType.NONE:
            return 1.0
        if self.factor_store is None:
            raise DataNotAvailableError(
                "本 feed 没有接复权因子存储（factor_store=None）⇒ 给不出 %s 在 %s 的复权因子。"
                "这是**配置**没接上，不是这一天没有数据（DATA_001）—— "
                "D6 下 HFQ/QFQ 必须有因子，不能退化成 1.0" % (symbol, when)
            )
        rows = self._select_factors(symbol, when, when)
        if not rows:
            raise DataNotAvailableError(
                "%s 在 %s 没有复权因子行（dc_adjust_factor，data_version=%s）⇒ DATA_001。"
                "D6/§2.4：找不到数据必须显式失败 —— 静默返回 1.0 会让这个标的的回测"
                "整体变成不复权，而报告里看不出任何异常" % (symbol, when, self.data_version)
            )
        return _as_float(rows[0].adjust_factor)

    def _qfq_base_factor(self, symbol: str) -> float:
        """前复权的**基准因子** = 本视图内该标的最后一个可见交易日的累计因子。

        前复权的定义就是"把历史价折算到最新那一天"，所以它**必然**依赖视图的右端：
        同一根历史 K 线在两个 `as_of_date` 下会得到两个不同的 QFQ 价。这不是实现缺陷，
        正是 D6 禁止回测用 `QFQ` 的理由（回测要可复现，而"最新那一天"每天都在动）。

        "最新那一天" = 因子表里该标的 `trade_date <= as_of_date` 的**最后一行**
        （`FactorStore.select_factors` 的契约是升序）。一行都没有 ⇒ DATA_001：
        基准不可知就不猜 —— 猜一个 1.0 会让整段前复权价**静默等于后复权价**。
        """
        rows = self._select_factors(symbol, MIN_DATE, self.as_of_date)
        if not rows:
            raise DataNotAvailableError(
                "%s 在 as_of_date=%s 之前没有任何复权因子行 ⇒ 前复权的基准不可知（DATA_001）。"
                "D6 下 QFQ 是视图行为，基准 = 视图末日的累计因子；拿 1.0 当基准会让"
                "前复权价静默等于不复权价" % (symbol, self.as_of_date)
            )
        return _as_float(rows[-1].adjust_factor)

    def _price_scale(self, symbol: str, when: date) -> float:
        """K 线价要乘的**总倍数**：`NONE` ⇒ `1.0`；`HFQ` ⇒ 该日累计因子；
        `QFQ` ⇒ 该日累计因子 ÷ `_qfq_base_factor()`。

        刻意把"乘什么"（本方法）与"怎么乘"（`_rescale_price`）分开：加第三种复权口径时
        只改这里，价格路径一个字都不用碰。
        """
        factor = self._factor_for(symbol, when)
        if self.adjust_type is AdjustType.QFQ:
            return factor / self._qfq_base_factor(symbol)
        return factor

    # ── 两道防线 ─────────────────────────────────────────────────────────
    def _require_visible(self, when, where: str) -> date:
        """**第一层**：显式日期参数越过 `as_of_date` ⇒ 立刻抛（约定 2：不裁剪）。

        `where` 只进错误信息，但它是必要的 —— 报错要让人一眼看出是**哪个方法**
        被越权调用了，否则拿到"请求了 2026-01-06"根本定位不到调用点。
        """
        target = _as_date(when)
        if target > self.as_of_date:
            raise FutureDataAccessError(
                "%s 请求了 %s，晚于 as_of_date=%s ⇒ 未来函数（DATA_002）"
                % (where, target, self.as_of_date)
            )
        return target

    def _select(self, symbol: str, start: date, end: date) -> List[BarData]:
        """**第二层**：不管第一层怎么放行，**每一行**都要过 `PITGuard`。

        这一层存在的唯一理由就是"存储层可能多吐行"：窗口合法但 `WHERE` 写错、
        仓储缓存按 symbol 命中忘了带日期、或者换了个数据源之后过滤语义不一样。
        这些情况第一层全都看不见。

        越界即抛，**不**静默 drop —— drop 会把"上游过滤写错了"变成"今天这根 K 线
        恰好没有"，而后者看起来完全正常。这是整个模块最重要的一行注释。
        """
        bars = []
        for row in self.store.select_bars(symbol, start, end):
            self.pit_guard.record_access(row.symbol, row.trade_date, BAR_FIELD)
            bars.append(self._row_to_bar(row))
        return bars

    def _select_factors(self, symbol: str, start: date, end: date) -> List[AdjustFactorPoint]:
        """因子行走**同一套**第二层防护：每一行都要过 `PITGuard`（理由见 `_select`）。

        单独一个方法而不是往 `_select` 的循环里塞：两者读的是两张表、返回两种行。
        一条被库多吐出来的因子行（窗口写错、缓存按 symbol 命中忘了带日期）与一根多余的
        K 线是**同等严重**的未来函数 —— 它会把后复权价乘上一个未来才生效的因子。

        没有接因子源时返回空列表：这是存储层的事实（"没有行"），**不是**本方法替调用方
        做决定 —— 要抛还是退回 1.0，是 `get_adjustment_factor` 的判据，只写在那一处。
        """
        if self.factor_store is None:
            return []
        factors = []
        for row in self.factor_store.select_factors(symbol, start, end):
            self.pit_guard.record_access(row.symbol, row.trade_date, FACTOR_FIELD)
            factors.append(row)
        return factors

    # ── DataFeed 的 9 个方法 ──────────────────────────────────────────────
    def get_bar(self, symbol: str, datetime: datetime) -> Optional[BarData]:
        when = self._require_visible(datetime, "DbDataFeed.get_bar")
        bars = self._select(symbol, when, when)
        return bars[0] if bars else None

    def get_bars(self, symbol: str, start: datetime, end: datetime) -> List[BarData]:
        first = self._require_visible(start, "DbDataFeed.get_bars(start)")
        last = self._require_visible(end, "DbDataFeed.get_bars(end)")
        if first > last:
            return []
        return self._select(symbol, first, last)

    def get_available_symbols(self) -> List[str]:
        """清单方法 ⇒ 裁剪（约定 2）：只在 `as_of` 之后才有数据的标的当下**不算可用**。

        代价是每个标的都要问一次存储（N 次查询）。S1 的存储是内存实现，先这样；
        接 PG 时这条要改成一条 `SELECT DISTINCT symbol ... WHERE trade_date <= $1`。
        """
        return [
            symbol
            for symbol in self.store.select_symbols()
            if self._select(symbol, MIN_DATE, self.as_of_date)
        ]

    def get_available_dates(self, symbol: str) -> List[datetime]:
        return [bar.datetime for bar in self._select(symbol, MIN_DATE, self.as_of_date)]

    def is_symbol_available(self, symbol: str, datetime: datetime) -> bool:
        # 越界由 get_bar 里的 `_require_visible` 挡住 —— 这一条不重复判，
        # 重复判会让"哪一层拒的"变得模糊。
        bar = self.get_bar(symbol, datetime)
        return bar is not None and bar.volume > 0

    def get_adjustment_factor(self, symbol: str, datetime: datetime) -> float:
        """该 (标的, 交易日) 的**累计**复权因子（D6 / §2.3）。

        三条分支，按"先问要不要因子、再问库里有没有"的顺序（2026-09-29 裁决）：

        * `AdjustType.NONE`（不复权）⇒ 返回 `1.0`，**不查库**。不复权就是"不乘任何东西"，
          去查一张可能没有这一行的表，只会把"今天停牌"变成 DATA_001 —— 那不是调用方
          想知道的事。这条分支是**纯计算**，所以它连 `factor_store` 都不看：一个没接
          因子源的 feed 在 `NONE` 下依然可用（否则 `NONE` 会因为配置而变红）。
        * `HFQ` / `QFQ` ⇒ **严格查** `dc_adjust_factor`，取 `(symbol, trade_date,
          data_version)` 那一行；**没有行就抛 `DataNotAvailableError`（DATA_001）**。
          不退化、不补 1.0：§2.4「所有"找不到数据"的分支都必须显式失败，不允许返回空集
          或默认值」。静默补 1.0 的后果不是一个错数，而是**整段回测悄悄变成不复权**，
          曲线照样好看。
        * 本 feed 没接因子源（`factor_store=None`）⇒ 同样是 DATA_001，但消息里点明是
          **配置**没接上、不是这一天没数据 —— 两种情形一个错误码，靠消息区分：
          真实原因不同，**修法**也不同（一个去接线，一个去重采）。

        `_require_visible` 必须在最前面：**问**"1 月 6 日的复权因子是多少"这件事本身
        就是一次对未来数据的访问。它也是 `tools/verify_data_center_pit.py` 的 P4
        钉住的不变量（带日期参数的公开方法必须过第一层）。

        **本方法只是 `_factor_for` 的公开薄壳**（2026-09-29 晚抽出来的）：复权价走同一份
        实现（`_row_to_bar` → `_price_scale` → `_factor_for`）⇒ "策略问到的因子"与
        "价格实际乘的因子"**不可能分歧**。这比"两份实现各自全绿"强：各自绿只说明各自
        自洽，说明不了两个答案相等。
        """
        when = self._require_visible(datetime, "DbDataFeed.get_adjustment_factor")
        return self._factor_for(symbol, when)

    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        """**仍然开着的**已知缺口：本切片没有分红数据，恒返回 0.0。

        与复权**刻意分开**：复权已经在 `_row_to_bar` 里实施了（模块文档的"已知缺口"节），
        而分红这一半**一个字都没动** —— 复权价是从 `dc_adjust_factor` 现算的，不依赖也不
        消费分红。守住它的用例是 `tests/test_data_center_pit.py` 里那条
        `test_known_gap_dividend_is_still_zero`（它的删改条件是**缺口真的关闭**，不是
        "某个迭代交付了"—— 2026-09-29 收口时正是按这个条件把它**留**下来的）。
        """
        self._require_visible(datetime, "DbDataFeed.get_dividend")
        return 0.0

    def get_trading_calendar(self, start: datetime, end: datetime) -> List[datetime]:
        first = self._require_visible(start, "DbDataFeed.get_trading_calendar(start)")
        last = self._require_visible(end, "DbDataFeed.get_trading_calendar(end)")
        if first > last:
            return []
        days = set()
        for symbol in self.store.select_symbols():
            days.update(bar.datetime for bar in self._select(symbol, first, last))
        return sorted(days)

    def get_market_status(self, datetime: datetime) -> MarketStatus:
        when = self._require_visible(datetime, "DbDataFeed.get_market_status")
        for symbol in self.store.select_symbols():
            if self._select(symbol, when, when):
                return MarketStatus.OPEN
        return MarketStatus.CLOSED


class DataCenter(ABC):
    """数据中心（数据中心契约 §3.3）。I2 S1 只实现 `as_of` 与 `data_version`。"""

    @abstractmethod
    def as_of(
        self,
        as_of_date: date,
        adjust_type: AdjustType = AdjustType.HFQ,
        fill_policy: FillPolicy = FillPolicy.NONE,
        data_version: str = "",
    ) -> DataFeed:
        """取 `as_of_date` 冻结视图。**这是产出 `DataFeed` 的唯一合法入口**（D3）。"""
        raise NotImplementedError

    @abstractmethod
    def live(self, account_mode: str) -> DataFeed:
        """实盘视图。本切片未实现。"""
        raise NotImplementedError

    @abstractmethod
    def trading_calendar(self, as_of_date: date) -> List[date]:
        """交易日历。本切片未实现。"""
        raise NotImplementedError

    @abstractmethod
    def data_version(self) -> str:
        """当前生效的数据版本号。"""
        raise NotImplementedError


class InMemoryDataCenter(DataCenter):
    """内存实现 —— 供单元测试与门禁使用（本机无本地 PostgreSQL 实例）。

    版本校验放在这里而不是 `DbDataFeed`：D8 说"数据版本缺失 ⇒ 抛 `DataVersionError`，
    回测结果不得标记为可复现"，那是**取视图**这一刻的判断，不是逐行读数的判断。
    """

    def __init__(
        self,
        store: BarStore,
        versions: Sequence[str] = ("v2026.09.23",),
        active_version: str = "v2026.09.23",
        session_mode: SessionMode = SessionMode.BACKTEST,
        factor_store: Optional[FactorStore] = None,
    ):
        self.store = store
        self.versions = tuple(versions)
        self._active_version = active_version
        self.session_mode = session_mode
        # 因子源与日线源是两个存储（两张表、两种行），所以在**数据中心**这一层也得各接一根
        # 线；默认 `None` 的老用法向后兼容（位置参数一个没动）。
        self.factor_store = factor_store

    def as_of(
        self,
        as_of_date: date,
        adjust_type: AdjustType = AdjustType.HFQ,
        fill_policy: FillPolicy = FillPolicy.NONE,
        data_version: str = "",
    ) -> DataFeed:
        version = data_version or self._active_version
        if version not in self.versions:
            raise DataVersionError(
                "数据版本不存在或未激活: %r（可用：%s）" % (version, ", ".join(self.versions))
            )
        # D6 / D7：这两个参数本身就能把未来信息带进回测，所以判在**取视图**这一刻，
        # 而不是等到逐行读的时候 —— 早早炸掉，调用方拿到的堆栈就在自己那行代码上。
        if self.session_mode is SessionMode.BACKTEST:
            if adjust_type is AdjustType.QFQ:
                raise FutureDataAccessError(
                    "回测会话请求 AdjustType.QFQ ⇒ 未来函数"
                    "（D6：前复权要用到今天的最新股本，实盘展示才允许）"
                )
            if fill_policy is FillPolicy.BFILL:
                raise FutureDataAccessError(
                    "回测会话请求 FillPolicy.BFILL ⇒ 未来函数"
                    "（D7：后向填充用未来值回填过去，仅离线清洗脚本允许）"
                )
        return DbDataFeed(
            self.store,
            as_of_date,
            version,
            session_mode=self.session_mode,
            adjust_type=adjust_type,
            fill_policy=fill_policy,
            # 每次取视图配一个新的守卫：守卫的状态（count / last_access / leakage）
            # 是**会话级**的，跨会话复用会让上一个视图的泄露点污染下一个视图的报告。
            pit_guard=RecordingPITGuard(as_of_date),
            factor_store=self.factor_store,
        )

    def live(self, account_mode: str) -> DataFeed:
        raise NotImplementedError("I2 S1 未实现 live()；实盘视图要等账户侧接入")

    def trading_calendar(self, as_of_date: date) -> List[date]:
        raise NotImplementedError("I2 S1 未实现 trading_calendar()；见数据中心契约 §3.5")

    def data_version(self) -> str:
        return self._active_version
