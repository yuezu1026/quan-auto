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

## 已知缺口（I2 S1 不修，但必须写在这里）

* **复权因子恒为 1.0**：本切片没有接 `dc_adjust_factor`，`get_adjustment_factor` 返回 1.0，
  即**等价于不复权**。所以 `as_of(..., adjust_type=AdjustType.HFQ)` 与 `NONE` 结果相同 ——
  D6 要求的"读取时按 `as_of_date` 现算后复权"还没实现。
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
from .errors import DataVersionError, FutureDataAccessError
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


class SessionMode(Enum):
    """会话模式 —— D6 与 D7 的判据来源。

    契约 §3.1 只定义了 `AdjustType` / `FillPolicy` / `SourcePriority` 三个枚举，
    **没有** `SessionMode`；但 §3.3 的 `as_of(as_of_date, adjust_type, fill_policy, data_version)`
    签名里也没有"这是回测还是实盘"的参数，而 D6 又要求「回测会话里请求 `QFQ` 抛异常」。
    说明这个模式只能在 `DataCenter` **实例**上（也就是构造时）确定。所以它属于本模块：
    它不是一条契约取值，是契约签名缺口的一处补丁，记在 manifest 的 `local_types` 里。
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
    ):
        self.store = store
        self.as_of_date = _as_date(as_of_date)
        self.data_version = data_version
        self.session_mode = session_mode
        self.adjust_type = adjust_type
        self.fill_policy = fill_policy
        self.pit_guard = pit_guard if pit_guard is not None else RecordingPITGuard(self.as_of_date)

    # ── 内部：把存储行变成 `BarData` ──────────────────────────────────────
    def _row_to_bar(self, row: DailyBar) -> BarData:
        stamp = _as_datetime(row.trade_date)
        return BarData(
            symbol=row.symbol,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=int(row.volume),
            amount=row.amount,
            datetime=stamp,
            timestamp=epoch_seconds(stamp),
        )

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
        """已知缺口：本切片没有复权数据，恒返回 1.0（= 不复权）。见模块 docstring。

        仍然要过 `_require_visible`：即使返回值是个常数，**问**"1 月 6 日的复权因子是多少"
        这件事本身就是一次对未来数据的访问 —— 今天返回 1.0，明天实现之后返回真因子，
        同一个调用在两种实现下语义不同，所以现在就得拒。
        """
        self._require_visible(datetime, "DbDataFeed.get_adjustment_factor")
        return 1.0

    def get_dividend(self, symbol: str, datetime: datetime) -> float:
        """已知缺口：本切片没有分红数据，恒返回 0.0（理由同 `get_adjustment_factor`）。"""
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
    ):
        self.store = store
        self.versions = tuple(versions)
        self._active_version = active_version
        self.session_mode = session_mode

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
        )

    def live(self, account_mode: str) -> DataFeed:
        raise NotImplementedError("I2 S1 未实现 live()；实盘视图要等账户侧接入")

    def trading_calendar(self, as_of_date: date) -> List[date]:
        raise NotImplementedError("I2 S1 未实现 trading_calendar()；见数据中心契约 §3.5")

    def data_version(self) -> str:
        return self._active_version
