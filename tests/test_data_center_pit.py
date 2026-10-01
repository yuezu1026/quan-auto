"""数据中心 `as_of` 冻结视图与 `PITGuard` —— I2 的**触发测试**。

这一份不是"把覆盖率刷上去"的测试，是**判据**。数据中心契约 §3.10「测试要求」表里
标了「必须」的四条 PIT 行为，逐条对应到下面：

| 数据中心契约 §3.10 条目 | 本文件的测试 |
| --- | --- |
| 视图冻结：`as_of(T)` 的 feed 在任何调用下都不得看到 `available_date > T` 的数据 | `test_get_bars_*` / `test_get_bar_*` / `test_available_*` / `test_is_symbol_available_*` / `test_trading_calendar_*` / `test_market_status_*` |
| PITGuard：构造一次越界读取，必须抛 `FutureDataAccessError` 且 `PITReport.is_consistent=False` | `test_pit_guard_*` |
| 传 `AdjustType.QFQ` 且会话为回测 ⇒ 必须抛 `FutureDataAccessError` | `test_as_of_rejects_qfq_in_backtest_session` |
| 缺失值：`BFILL` 在回测上下文抛异常 | `test_as_of_rejects_bfill_in_backtest_session` |

I2 的 DoD 那条「构造一条 `available_date > as_of_date` 的数据，确认取数必须报错而不是
静默返回」就落在 `test_pit_guard_catches_leak_from_store_that_ignores_window`：
把存储层换成一个**忽略窗口**的实现（等价于 SQL 里 `WHERE` 写错、或者仓储缓存脏了），
`PITGuard` 必须把它抓住并抛 **CRITICAL**。没有这一条，"第二层防护生效"永远只是个说法 ——
写起来像门禁、实际上是空转。

另外每一组判据都配了**控制样本**（`test_*_control` / 同函数里的正向分支）：
只测"该炸的一定炸"会把 `as_of` 设成 `date(1900,1,1)` 这种把一切都挡住的实现也判成 PASS。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import List

import pytest

from quanauto.datacenter import (
    AdjustFactorPoint,
    DailyBar,
    DbDataFeed,
    DividendPoint,
    InMemoryBarStore,
    InMemoryDataCenter,
    InMemoryDividendStore,
    InMemoryFactorStore,
    LeakagePoint,
    SessionMode,
)
from quanauto.datafeed import CsvDataFeed
from quanauto.enums import AdjustType, FillPolicy, MarketStatus
from quanauto.errors import DataNotAvailableError, DataVersionError, FutureDataAccessError

SYMBOL = "600000.SH"
OTHER = "000001.SZ"
VERSION = "v2026.09.23"

#: 本文件里唯一一处「另一条实现体」：用来把 D9（同一协议两条实现体同口径）变成断言。
CSV_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_prices.csv")

VISIBLE = date(2026, 1, 2)  # 早于 as_of，必须可见
MISSING = date(2026, 1, 3)  # 落在区间里但库里没有 —— 用来证明"空"和"被挡住"不是一回事
ON_AS_OF = date(2026, 1, 5)  # 正好等于 as_of，必须可见
FUTURE = date(2026, 1, 6)  # 晚于 as_of，一次都不许露出来
AS_OF = ON_AS_OF


def _row(
    symbol: str,
    trade_date: date,
    close: float = 10.0,
    volume: float = 1000.0,
    open_: float = None,
    high: float = None,
    low: float = None,
) -> DailyBar:
    """一行存储形状的日线。`open_` / `high` / `low` 不给就取 `close`。

    另有三个参数是为了让**四列价格**能被分开观察（`open_` 叫这个名字是因为 `open`
    是内建函数）—— 夹具里四列全相等时，"四列都乘了"与"只乘了 close"这两种实现
    给出的是同一组数，那类变异就抓不到了。
    """
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=close if open_ is None else open_,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
        amount=close * volume,
        source="fixture",
        data_version=VERSION,
    )


def _rows() -> List[DailyBar]:
    """三个可见点 + 两个只会出现在未来的点。

    `OTHER` 的全部数据都在 `as_of` 之后 —— 它是 `get_available_symbols` 的判据：
    一个"未来才有数据"的标的，在当下这个视角里**不算可用标的**。
    """
    return [
        _row(SYMBOL, VISIBLE, close=9.0),
        _row(SYMBOL, ON_AS_OF, close=10.0),
        _row(SYMBOL, FUTURE, close=11.0),
        _row(OTHER, FUTURE, close=12.0),
    ]


def _unit_factor_rows() -> List[AdjustFactorPoint]:
    """全 1.0 的因子行 —— 默认夹具的那一份（理由见 `_center` 的 docstring）。"""
    return [
        AdjustFactorPoint(SYMBOL, VISIBLE, 1.0, source="fixture", data_version=VERSION),
        AdjustFactorPoint(SYMBOL, ON_AS_OF, 1.0, source="fixture", data_version=VERSION),
    ]


_NOT_GIVEN = object()  # 区分"没传 factor_store"与"显式传 None"（后者是**一个真实形态**）


def _center(store=None, factor_store=_NOT_GIVEN, **kwargs) -> InMemoryDataCenter:
    """默认夹具是**接线完整**的 feed：`factor_store` 给一份全 1.0 的因子行。

    为什么默认要给（2026-09-29 晚 · 复权价实施之后）：D6 的默认口径是 `HFQ`，而
    **读取时现算复权价**意味着每一次 `get_bar`/`get_bars` 都要问得出这一天的因子。
    一份 `factor_store=None` 的 HFQ feed 是**接线不完整**的 feed —— 拿它当"PIT 行为"
    的样本，失败信息会指向复权（DATA_001），而用例想说的是越界（DATA_002）。

    因子取 **1.0**：本文件这一组的判据是"看得见 / 看不见"，不是"乘了多少"。全 1.0 ⇒
    复权对价格是**恒等**变换，`close == 9.0 / 10.0` 这些断言仍然在说它们本来要说的事。
    "乘了多少"的判据在下面 `_feed_with_factors()` 那一组里，那里用的是非 1.0 的因子。
    """
    if factor_store is _NOT_GIVEN:
        factor_store = InMemoryFactorStore(_unit_factor_rows())
    return InMemoryDataCenter(
        store if store is not None else InMemoryBarStore(_rows()),
        factor_store=factor_store,
        **kwargs
    )


def _feed(store=None, **kwargs) -> DbDataFeed:
    feed = _center(store, **kwargs).as_of(AS_OF)
    assert isinstance(feed, DbDataFeed), "as_of() 是产出 DataFeed 的唯一入口（D3）"
    return feed


class LyingBarStore(InMemoryBarStore):
    """一个 **WHERE 写错了**的存储：忽略窗口，把该标的的全部行都吐出来。

    这不是为了凑一个测试而编的坏对象 —— 它对应两个真实故障：SQL 里 `BETWEEN` 的边界
    写反/漏写，以及仓储层带缓存时"按 (symbol) 缓存了整个标的、之后只按 symbol 命中"。
    两种情况的表现完全一样：窗口过滤在**上面**那一层被迫承担全部责任。
    """

    def select_bars(self, symbol: str, start: date, end: date) -> List[DailyBar]:
        return sorted((r for r in self.rows if r.symbol == symbol), key=lambda r: r.trade_date)


# ── 视图冻结：显式越界请求必须抛（约定 2）────────────────────────────────


def test_get_bars_rejects_window_that_extends_past_as_of():
    """窗口 `end` 越过 `as_of` ⇒ 抛，不裁剪成一个短结果。"""
    feed = _feed()
    with pytest.raises(FutureDataAccessError) as excinfo:
        feed.get_bars(SYMBOL, datetime(2026, 1, 1), datetime(2026, 1, 31))
    assert excinfo.value.code == "DATA_002"


def test_get_bars_returns_only_rows_at_or_before_as_of():
    """控制样本：窗口正好收到 `as_of` ⇒ 正常返回，`as_of` 当天算可见。"""
    feed = _feed()
    bars = feed.get_bars(SYMBOL, datetime(2026, 1, 1), datetime(2026, 1, 5))
    assert [b.datetime.date() for b in bars] == [VISIBLE, ON_AS_OF]
    assert [b.close for b in bars] == [9.0, 10.0]


def test_get_bar_rejects_point_after_as_of():
    feed = _feed()
    with pytest.raises(FutureDataAccessError):
        feed.get_bar(SYMBOL, datetime(2026, 1, 6))


def test_get_bar_control_missing_day_is_none_not_error():
    """"库里没有" 和 "被 PIT 挡住" 必须是两种结果：前者 `None`，后者抛。"""
    feed = _feed()
    assert feed.get_bar(SYMBOL, datetime(2026, 1, 3)) is None
    assert feed.get_bar(SYMBOL, datetime(2026, 1, 5)).close == 10.0


def test_available_dates_are_clipped_to_as_of():
    feed = _feed()
    assert feed.get_available_dates(SYMBOL) == [
        datetime(2026, 1, 2),
        datetime(2026, 1, 5),
    ]


def test_available_symbols_excludes_future_only_symbol():
    """只在 `as_of` 之后才有数据的标的，当下不可用。"""
    feed = _feed()
    assert feed.get_available_symbols() == [SYMBOL]


def test_is_symbol_available_rejects_point_after_as_of():
    feed = _feed()
    with pytest.raises(FutureDataAccessError):
        feed.is_symbol_available(SYMBOL, datetime(2026, 1, 6))
    assert feed.is_symbol_available(SYMBOL, datetime(2026, 1, 5)) is True


def test_trading_calendar_rejects_range_past_as_of():
    feed = _feed()
    with pytest.raises(FutureDataAccessError):
        feed.get_trading_calendar(datetime(2026, 1, 1), datetime(2026, 2, 1))
    assert feed.get_trading_calendar(datetime(2026, 1, 1), datetime(2026, 1, 5)) == [
        datetime(2026, 1, 2),
        datetime(2026, 1, 5),
    ]


def test_market_status_rejects_point_after_as_of():
    feed = _feed()
    with pytest.raises(FutureDataAccessError):
        feed.get_market_status(datetime(2026, 1, 6))
    assert feed.get_market_status(datetime(2026, 1, 5)) is MarketStatus.OPEN
    assert feed.get_market_status(datetime(2026, 1, 3)) is MarketStatus.CLOSED


# ── PITGuard：第二层防护 ────────────────────────────────────────────────


def test_pit_guard_reports_consistent_after_clean_read():
    """控制样本：干净读取之后报告必须是 `is_consistent=True` 且没有泄露点。

    有这一条才能区分"守卫真的在看"和"守卫永远说不一致"（后者把所有回测都炸掉，
    看起来更安全，实际是不可用）。
    """
    feed = _feed()
    feed.get_bars(SYMBOL, datetime(2026, 1, 1), datetime(2026, 1, 5))
    report = feed.pit_guard.report()
    assert report.is_consistent is True
    assert report.as_of_date == AS_OF
    assert report.latest_visible_date == ON_AS_OF
    assert feed.pit_guard.leakage_points() == []
    assert feed.pit_guard.count > 0, "守卫一次访问都没记到 ⇒ 它根本没被接上"


def test_pit_guard_catches_leak_from_store_that_ignores_window():
    """🔴 I2 的核心触发测试：窗口是合法的，越界行由**存储**吐出来。

    `get_bars(1/1, 1/5)` 本身没有越界，所以第一层（参数校验）不响；越界的那一行
    (`trade_date=1/6`) 是从 `select_bars` 里多出来的。这一层如果没人查，
    它就会变成一根 `as_of` 之后的 K 线，被策略读到，然后回测曲线变好看。
    """
    feed = _feed(store=LyingBarStore(_rows()))
    with pytest.raises(FutureDataAccessError) as excinfo:
        feed.get_bars(SYMBOL, datetime(2026, 1, 1), datetime(2026, 1, 5))
    assert excinfo.value.code == "DATA_002"

    report = feed.pit_guard.report()
    assert report.is_consistent is False
    assert report.symbol == SYMBOL
    assert report.latest_visible_date == FUTURE
    assert "1/6" in report.message or "2026-01-06" in report.message

    points = feed.pit_guard.leakage_points()
    assert isinstance(points[0], LeakagePoint)
    assert [p.trade_date for p in points] == [FUTURE]
    assert points[0].severity == "CRITICAL"
    assert points[0].as_of_date == AS_OF
    # `field="bar"`：日线以**整行**为访问单位（`BarData` 不可分割），契约 §3.7 只规定
    # `field` 是"字段名"、没规定日线填什么 ⇒ 这是实现侧的一个选择（常量 `BAR_FIELD`），
    # 单字段读取（财务的 `revenue` 之类）才填真实列名。这里钉住它，是为了让改动可见。
    assert points[0].field == "bar"


# ── D6 / D7：复权口径与填充策略在回测会话里不许乱来 ──────────────────────


def test_as_of_rejects_qfq_in_backtest_session():
    """D6：`QFQ` 要用到今天的最新股本 ⇒ 含未来信息 ⇒ 回测里请求就是未来函数。"""
    dc = _center(session_mode=SessionMode.BACKTEST)
    with pytest.raises(FutureDataAccessError):
        dc.as_of(AS_OF, adjust_type=AdjustType.QFQ)


def test_as_of_allows_qfq_in_live_session():
    """控制样本：实盘展示允许 `QFQ`（契约 §D6 的原文就是"允许用于实盘展示"）。"""
    dc = _center(session_mode=SessionMode.LIVE)
    feed = dc.as_of(AS_OF, adjust_type=AdjustType.QFQ)
    assert isinstance(feed, DbDataFeed)
    assert feed.adjust_type is AdjustType.QFQ


def test_as_of_rejects_bfill_in_backtest_session():
    """D7：后向填充用未来值回填过去，是同类里最直白的未来函数。"""
    dc = _center(session_mode=SessionMode.BACKTEST)
    with pytest.raises(FutureDataAccessError):
        dc.as_of(AS_OF, fill_policy=FillPolicy.BFILL)


def test_as_of_allows_ffill_when_explicitly_asked():
    """控制样本：`FFILL` 是显式开关（默认 `NONE`），回测里允许 —— 只是必须由调用方开。"""
    dc = _center(session_mode=SessionMode.BACKTEST)
    assert dc.as_of(AS_OF).fill_policy is FillPolicy.NONE
    assert dc.as_of(AS_OF, fill_policy=FillPolicy.FFILL).fill_policy is FillPolicy.FFILL


# ── D8：数据版本 ────────────────────────────────────────────────────────


def test_unknown_data_version_raises():
    """D8：版本不存在/未激活 ⇒ 抛 `DataVersionError`，不许悄悄退回当前版本。"""
    dc = _center()
    with pytest.raises(DataVersionError) as excinfo:
        dc.as_of(AS_OF, data_version="v1999.01.01")
    assert excinfo.value.code == "DATA_004"


def test_default_data_version_is_the_active_one():
    dc = InMemoryDataCenter(
        InMemoryBarStore(_rows()),
        versions=("v2026.09.23", "v2026.10.01"),
        active_version="v2026.10.01",
    )
    assert dc.data_version() == "v2026.10.01"
    assert dc.as_of(AS_OF).data_version == "v2026.10.01"
    assert dc.as_of(AS_OF, data_version="v2026.09.23").data_version == "v2026.09.23"


# ── D6 读侧：复权因子真的从存储里读出来（I2 A6 收口）──────────────────────
#
# 这一组替换掉了原先那条 `test_known_gap_hfq_factor_is_identity_in_s1`
# （它**故意**断言 `get_adjustment_factor(...) == 1.0`）。删它的理由就写在它自己的
# docstring 里：触发条件只认**产物**，而产物变了 —— 读侧不再恒返回 1.0。
# 那条测试的收尾命令是「删掉这条测试」，所以这里是**删除**，不是改期望值。
#
# **订正（2026-09-29 晚 Ⅱ，复权价实施）**：上面这句「缺口只关了一半：复权价仍未实施」**已作废** ——
# 复权已在**读取时现算**（`DbDataFeed._row_to_bar` 调 `_rescale_price`，见 `quanauto/datacenter.py`
# 模块头与 `tests/test_backtest_db_feed.py` 里那一组读侧用例）。「缺口没关」本身仍然成立，
# 只是开着的换成了另外三半：**分红**（就是下面 `get_dividend` 那条）、两个 ingestor 在 `quanauto/`
# 内**零产品调用点**、`validate_frame` 对**复权帧**零判据。
#
# **订正（2026-09-29 晩 Ⅳ）**：上面那三半里的第三半（`validate_frame` 对复权帧零判据）
# **已收口** —— `validate_frame` 认 schema（2026-09-29 晩 Ⅳ 时是四张含复权因子；
# 2026-10-01 分红落地后是五张），真的判值域 / 帧级自然键重复 /
# 认 schema（DC 契约附录 B21.5 的 Ⅳ 块与 B21.6 表里那一行）⇒ 开着的**只剩两半**：
# 分红与两个 ingestor 零产品调用点。**「缺口没关」这句话本身继续成立**，那条用例也继续不许删。
#
# **订正（2026-09-30，I2 收口 ①ⓑ「采集编排层」）**：上面那两半里的第二半（两个 ingestor 在
# `quanauto/` 内零产品调用点）**已收口** —— `quanauto/ingest.py` 就是那条缺的编排层
# （取数 → `validate_frame` → 盖 `source`/`data_version` → 落库 → 写 `dc_ingest_run` 批次留痕），
# 两个 ingestor 各有一个调用点，`tests/test_ingest.py` 45 条压着它（DC 契约附录 H）。
# ⇒ 开着的**只剩一半**：分红（就是下面 `get_dividend` 那条）。**「缺口没关」这句话本身继续成立。**
# ⚠️ 它只主张「调用点接上了」：`quanauto/cli.py` 没开采集子命令、这条路径在任何真实 PostgreSQL 上没跑过。
#
# **订正（2026-10-01，I2 收口 ①ⓐ「分红读取」）**：上面那「只剩一半」里的**分红那一半也关了** ——
# `DbDataFeed.get_dividend()` 从 `dc_dividend` 真的读库（缺行 / 缺源都返回 `0.0`，与主契约
# §2.2.1 的 `CsvDataFeed` 同口径，理由见附录 B22.3）⇒ **I2 收口条件① 的三条到这里全关**
# （复权价 2026-09-29 晚 Ⅱ、写入路径 2026-09-30、分红今天）。
# ⇒ 照原先写好的收尾命令，那一条「另一半」的用例 `test_known_gap_dividend_is_still_zero`
# 在这里**删除**（不是把期望值改成新数），新行为的判据在文件末尾那一组。
# ⚠️ 「①全关」**不是**「I2 收工」：②（各通道「哪些位置已实测、哪些仍是推断」的未确认
# 清单）照旧开着；而且分红**读得出来** ≠ 引擎在绩效里**计**分红（`quanauto/engine.py`
# 一行没动，附录 B22.2）。


def _factor_rows():
    """因子夹具：`SYMBOL` 两个可见日，外加一个未来日（守卫那条用例要用）。"""
    return [
        AdjustFactorPoint(SYMBOL, VISIBLE, 1.10, source="fixture", data_version=VERSION),
        AdjustFactorPoint(SYMBOL, ON_AS_OF, 1.25, source="fixture", data_version=VERSION),
        AdjustFactorPoint(SYMBOL, FUTURE, 9.99, source="fixture", data_version=VERSION),
    ]


def _feed_with_factors(rows=None, **kwargs) -> DbDataFeed:
    return _feed(
        factor_store=InMemoryFactorStore(_factor_rows() if rows is None else rows), **kwargs
    )


class ExplodingFactorStore(InMemoryFactorStore):
    """探针：**一旦被查询就炸**，用来把「没查库」变成可断言的事实。

    「没查库」本身没有返回值可供断言 —— 一个偷偷查了库的实现与一个真的没查的实现，
    对这条路径给出的是同一个 1.0。所以证人必须是「被查就抛」的对象。
    """

    def __init__(self):
        super().__init__([])
        self.queries = 0

    def select_factors(self, symbol, start, end):
        self.queries += 1
        raise AssertionError("NONE 口径下不该去问复权因子，却问了 %s" % (symbol,))


class LyingFactorStore(InMemoryFactorStore):
    """`select_factors` 忽略窗口的因子存储 —— 与 `LyingBarStore` 同一个故障模型。"""

    def select_factors(self, symbol, start, end):
        return sorted((r for r in self.rows if r.symbol == symbol), key=lambda r: r.trade_date)


def test_hfq_without_a_factor_store_is_refused_not_defaulted():
    """🔴 默认口径（`HFQ`）下没有因子存储 ⇒ 抛 DATA_001，**不许**静默给 1.0。

    这条是「复权因子缺口」真正关闭的判据：在那之前这里返回 1.0，而报告里一个字都不提
    ⇒ 整段回测静默变成不复权。DC 契约 §2.4：「所有『找不到数据』的分支都必须
    **显式失败**，不允许返回空集或默认值」。
    **显式**传 `factor_store=None`（2026-09-29 晚起，本文件的默认夹具是接线完整的）：
    这条用例要的恰恰是"接线不完整"那个形态，所以它必须自己指名，不能再靠默认值 ——
    否则 `_center()` 哪天换成别的默认，这条会**静默地**变成在测别的东西。
    """
    feed = _feed(factor_store=None)
    assert feed.adjust_type is AdjustType.HFQ, "默认口径是 HFQ（DC 契约 §2.3 行 265）"
    with pytest.raises(DataNotAvailableError) as excinfo:
        feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5))
    assert excinfo.value.code == "DATA_001"
    assert "factor_store" in str(excinfo.value), (
        "这一支要说清是**配置**没接上，而不是「这一天没有数据」：%s" % excinfo.value
    )


def test_hfq_with_a_store_that_has_no_row_for_that_day_is_refused():
    """另一支：存储接上了、但这一天**没有因子行** ⇒ 同样抛 DATA_001，不补 1.0。"""
    feed = _feed_with_factors(rows=[])  # 接上了，只是空的
    with pytest.raises(DataNotAvailableError) as excinfo:
        feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5))
    assert excinfo.value.code == "DATA_001"
    assert "factor_store" not in str(excinfo.value), (
        "这一支是「库里没有这一行」，不能再拿配置说事 —— 两句话必须分得开：%s"
        % excinfo.value
    )


def test_hfq_reads_the_real_factor_instead_of_returning_one():
    """控制样本 + 防空转：真读到那一天的值（1.25 / 1.10），而不是恒 1.0。

    任何「恒返回 1.0」的实现都能通过上面两条（它两条都抛）—— 只有真给一个非 1.0 的值，
    才能证明这个数确实来自存储；而 1/3 没有因子行这一条，同时证明它是**逐日**去查的。
    """
    feed = _feed_with_factors()
    assert feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5)) == 1.25
    assert feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 2)) == 1.10
    with pytest.raises(DataNotAvailableError):
        feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 3))  # MISSING：库里没这一天


def test_none_returns_identity_without_touching_the_store():
    """`NONE` ⇒ 1.0 是**算出来的**结果，而且**一次都不问库**。

    这两件事必须分开断言：只测 1.0 的话，「1.0 是算出来的」与「1.0 是查不到时的退路」
    给出的是同一个值，而后者正是这个缺口原本的形态。
    """
    probe = ExplodingFactorStore()
    feed = _center(factor_store=probe).as_of(AS_OF, adjust_type=AdjustType.NONE)
    assert feed.adjust_type is AdjustType.NONE
    assert feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5)) == 1.0
    assert probe.queries == 0, "NONE 口径下还去查库 ⇒ 这条分支的存在意义就没了"


def test_factor_read_is_guarded_by_pit_too():
    """因子读取也在 PIT 面上：存储忽略窗口 ⇒ 守卫必须炸（DATA_002）。

    「因子只是乘一下」是最容易漏掉的未来函数：累计因子本身就把未来信息（到今天为止的
    全部送转/分红）折了进去，所以它必须和 K 线走同一条守卫，而不是走一条后门。
    这条对应 `tools/verify_data_center_pit.py` 里那条**静态**判据（因子读取也要被守卫
    点名）；静态判据管「有没有写」，这条管「写了到底拦不拦得住」。
    """
    feed = _feed(factor_store=LyingFactorStore(_factor_rows()))
    with pytest.raises(FutureDataAccessError) as excinfo:
        feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5))
    assert excinfo.value.code == "DATA_002"
    points = feed.pit_guard.leakage_points()
    assert [p.trade_date for p in points] == [FUTURE], "越界的必须是因子那一行的日期"
    assert points[0].field == "adjust_factor", (
        "因子是**单列**读取 ⇒ field 记真实列名（`FACTOR_FIELD`），不是日线那个整行的 'bar'"
    )


# ── （原「已知缺口：分红恒 0.0」那一条已于 2026-10-01 删除）─────────────────
#
# `test_known_gap_dividend_is_still_zero` 就在这里。它**故意**断言 `get_dividend` 恒 0.0，
# 并写明自己的收尾命令是「直接删掉这条测试，不要先把期望值改成新数」。缺口已关 ⇒ 照命删掉。
# 分红现在的判据在文件末尾那一组 `test_dividend_*`：钉的是**新**行为 —— `0.0` 只在
# 「库里没有那一天的行」与「本 feed 没接分红源」两种情形下出现。


# ── 复权价读数：D6 第二句「读取时按 as_of_date 现算」（2026-09-29 晚 · I2 收口）──
#
# 上面那组钉的是"因子能被问出来"，这一组钉的是"这个因子真的乘到了价格上"。
# 两件事必须分开测：`get_adjustment_factor()` 答对、而 `get_bar()` 不乘，就是"策略问到的
# 因子"与"价格实际乘的因子"分歧 —— 报告里一个字都看不出来（曲线照样画得出来）。
# 所以下面一律读 `get_bar` / `get_bars`，不去读因子接口。


def test_hfq_bars_are_the_store_price_times_that_days_factor():
    """`HFQ` ⇒ 每根 K 线的价格乘**那一天**的累计因子。

    两个可见日的因子不同（1.10 / 1.25）⇒ 顺带证明它是**逐日**查的，不是拿第一个值
    套满全程（那种实现只会错在第二根上）。
    """
    feed = _feed_with_factors()
    first = feed.get_bar(SYMBOL, datetime(2026, 1, 2)).close
    # 9.0 × 1.10：1.10 在二进制里不精确，所以只声称"到 1e-12 相对误差"；
    # 下面那个 1.25 是 5/4（二进制精确）⇒ 可以直接判相等，不需要 approx。
    assert first == pytest.approx(9.9, rel=1e-12)
    assert feed.get_bar(SYMBOL, datetime(2026, 1, 5)).close == 12.5
    assert first != 9.0, "乘了个 1.0 ⇒ 因子一行都没生效"


def test_get_bars_returns_adjusted_prices_too():
    """`get_bars` 走的是同一个 `_select` ⇒ 窗口读出来的**每一根**都复权。

    只改 `get_bar` 的实现会让这条红：那时同一根 K 线在两个入口上是两个价。
    """
    feed = _feed_with_factors()
    bars = feed.get_bars(SYMBOL, datetime(2026, 1, 1), datetime(2026, 1, 5))
    assert [b.datetime.date() for b in bars] == [VISIBLE, ON_AS_OF]
    assert bars[0].close == pytest.approx(9.9, rel=1e-12)
    assert bars[1].close == 12.5


def test_all_four_price_columns_are_scaled_not_just_close():
    """开 / 高 / 低 / 收**四列**都乘 —— 夹具里四列相等时，"只乘了 close"抓不出来。"""
    rows = [_row(SYMBOL, ON_AS_OF, close=10.0, open_=11.0, high=12.0, low=9.0)]
    feed = _feed(
        store=InMemoryBarStore(rows),
        factor_store=InMemoryFactorStore(
            [AdjustFactorPoint(SYMBOL, ON_AS_OF, 1.25, source="fixture", data_version=VERSION)]
        ),
    )
    bar = feed.get_bar(SYMBOL, datetime(2026, 1, 5))
    assert (bar.open, bar.high, bar.low, bar.close) == (13.75, 15.0, 11.25, 12.5)


def test_volume_and_amount_are_not_scaled():
    """`volume` / `amount` **刻意不乘因子** —— 这是一条决定，不是漏改（下面钉住它）。

    乘上去有两个具体后果：① `volume` 是 `int`，`int(1000 / 1.25)` 虽然还是 800，
    但任意小成交量（如 1 手）会被截成 0 ⇒ `is_symbol_available` 判它不可交易，
    策略凭空少掉一天数据且不报错；② `amount` 是成交额（真金白银），乘一个无量纲因子
    没有金融含义。
    """
    bar = _feed_with_factors().get_bar(SYMBOL, datetime(2026, 1, 5))
    assert bar.volume == 1000
    assert bar.amount == 10.0 * 1000, "`_row` 的 amount = close × volume，不该被 1.25 碰过"


def test_none_bars_are_identity_and_never_touch_the_factor_store():
    """`NONE` ⇒ 价格**原样**（逐位相同）、且一次都不问因子库。

    "一次都不问"只能靠探针断言（`ExplodingFactorStore` 被调用就抛）—— "没查"没有返回
    值可断言。而且这条必须在**价格路径**上测：因子接口不查、`_row_to_bar` 偷偷查，
    是两种实现，报告里长得一模一样。
    """
    probe = ExplodingFactorStore()
    feed = _center(factor_store=probe).as_of(AS_OF, adjust_type=AdjustType.NONE)
    bar = feed.get_bar(SYMBOL, datetime(2026, 1, 5))
    assert bar.close == 10.0, "不复权价必须逐位等于库里的值（不做一次 × 1.0，不留浮点尾巴）"
    assert probe.queries == 0, "NONE 口径下价格路径还去查库 ⇒ 这条分支的存在意义就没了"


def test_missing_factor_row_fails_the_read_path_instead_of_returning_the_raw_price():
    """**有行情、没因子**的那一天 ⇒ 读它就抛 DATA_001，不是静默按不复权给价。

    静默退化是最坏的形态：整段回测变成一个不复权的策略，而报告里没有任何异常 ——
    所以这里刻意让 1/2 那根 K 线成为"行情在、因子不在"的一天（因子只给 1/5）。

    同一条里带控制样本：有因子的那一天必须照常给价。只测"该炸的炸"会把一个
    "任何一天都炸"的实现判成 PASS。
    """
    only_on_as_of = [
        AdjustFactorPoint(SYMBOL, ON_AS_OF, 1.25, source="fixture", data_version=VERSION)
    ]
    feed = _feed(factor_store=InMemoryFactorStore(only_on_as_of))
    with pytest.raises(DataNotAvailableError) as excinfo:
        feed.get_bar(SYMBOL, datetime(2026, 1, 2))
    assert excinfo.value.code == "DATA_001"
    assert "factor_store" not in str(excinfo.value), (
        "这是「库里没有这一行」，不能拿配置没接上那句话说事：%s" % excinfo.value
    )
    assert feed.get_bar(SYMBOL, datetime(2026, 1, 5)).close == 12.5


def test_qfq_over_the_same_view_is_relative_to_the_last_visible_factor():
    """`QFQ` ⇒ 该日因子 ÷ **视图末日**的因子（前复权的定义）。

    钉四件事：① 基准日（= 视图末日）上 QFQ **逐位等于不复权价** —— "折算到最新那一天"
    意味着最新那一天不动；② 更早的那天被折到不复权价**以下**（该日因子 < 基准因子）；
    ③ 两个口径的数**不相等** —— `_qfq_base_factor` 返回 1.0 的实现等于把 QFQ 静默变成
    HFQ，在这里必红；④ QFQ / HFQ 在任意两天的比值**相同** ⇒ 前复权是整条曲线的一次
    整体缩放，不是逐点各自为政。
    """
    center = _center(
        factor_store=InMemoryFactorStore(_factor_rows()), session_mode=SessionMode.LIVE
    )
    qfq = center.as_of(AS_OF, adjust_type=AdjustType.QFQ)
    hfq = _feed_with_factors()
    assert qfq.adjust_type is AdjustType.QFQ

    qfq_last = qfq.get_bar(SYMBOL, datetime(2026, 1, 5)).close
    assert qfq_last == 10.0, "基准日：库里的 close 就是 10.0，前复权不该动它"
    hfq_last = hfq.get_bar(SYMBOL, datetime(2026, 1, 5)).close
    assert hfq_last == 12.5, "同一天的后复权价"

    qfq_first = qfq.get_bar(SYMBOL, datetime(2026, 1, 2)).close
    hfq_first = hfq.get_bar(SYMBOL, datetime(2026, 1, 2)).close
    assert qfq_first < 9.0, "更早的那天要折到不复权价以下（该日因子 < 基准因子）"
    assert qfq_first == pytest.approx(9.9 / 1.25, rel=1e-12)
    assert qfq_first != hfq_first, "QFQ 与 HFQ 在这个视图里不是同一个数"
    assert qfq_first / qfq_last == pytest.approx(hfq_first / hfq_last, rel=1e-12)


# ── D4 读侧：分红真的从存储里读出来（I2 收口 ①ⓐ，2026-10-01）────────────────
#
# 与上面两组同一条思路：`get_dividend` 的返回值只在**真的查了库**时才有意义。
# 这一组的探针各钉一件事：
#   * `_feed_with_dividends()`（接线完整）—— 有那一天的行 ⇒ 返回那一行的值（逐日不同）；
#   * `InMemoryDividendStore([一行别的日子])` —— 那一天没行 ⇒ `0.0`；
#   * `dividend_store=None` —— 压根没接 ⇒ 同样 `0.0`，且与 `CsvDataFeed` **同口径**（D9）；
#   * `LyingDividendStore`（窗口过滤坏了）—— 守卫必须炸，且按 `announce_date` 判可见性。
# ⚠️ 「`0.0`」在这一组里出现三次而含义不同：**那一天没有事件** / **本 feed 没接源**。
# 两者返回值相同是**有意的决定**（附录 B22.3 那张表：分红是稀疏事件流，"没有"是常态），
# 与 `get_adjustment_factor` 缺行抛 DATA_001 **刻意不对称**。

#: 除权除息日 = `as_of` 当天 ⇒ 这一次事件必须可见。
DIVIDEND_EX = ON_AS_OF
#: 公告日早于除权日（DDL 的 `ck_dc_dividend_announce_not_after_ex` 就是这个方向）。
DIVIDEND_ANNOUNCE = VISIBLE
#: 一次**未来**的除权：除权日与公告日都在 `as_of` 之后。两列**故意不相等** ——
#: 相等的话，「守卫记的是公告日还是除权日」就断言不出来了。
FUTURE_EVENT = date(2026, 1, 9)
FUTURE_ANNOUNCE = date(2026, 1, 6)


def _dividend_rows() -> List[DividendPoint]:
    """两次可见事件（0.12 / 0.35，值不同 ⇒ 能抓「恒返回第一个值」）+ 一次未来事件。"""
    return [
        DividendPoint(SYMBOL, VISIBLE, DIVIDEND_ANNOUNCE, 0.12,
                      source="fixture", data_version=VERSION),
        DividendPoint(SYMBOL, DIVIDEND_EX, DIVIDEND_ANNOUNCE, 0.35,
                      source="fixture", data_version=VERSION),
        DividendPoint(SYMBOL, FUTURE_EVENT, FUTURE_ANNOUNCE, 9.99,
                      source="fixture", data_version=VERSION),
    ]


def _feed_with_dividends(rows=None, **kwargs) -> DbDataFeed:
    return _feed(
        dividend_store=InMemoryDividendStore(_dividend_rows() if rows is None else rows),
        **kwargs
    )


class LyingDividendStore(InMemoryDividendStore):
    """`select_dividends` 忽略窗口的分红存储 —— 与 `LyingBarStore` 同一个故障模型。"""

    def select_dividends(self, symbol: str, start: date, end: date) -> List[DividendPoint]:
        return sorted((r for r in self.rows if r.symbol == symbol), key=lambda r: r.ex_date)


def test_dividend_without_a_dividend_store_returns_zero_like_the_csv_feed():
    """🔴 没接分红源 ⇒ `0.0`，**不抛** —— 这条把「同一协议两条实现体同口径」钉住（D9）。

    主契约 §2.2.1 的参考实现 `CsvDataFeed` 在「没有 symbol / 没有这一天 / 没有 `dividend`
    列」三种情形下一律 `return 0.0`，**从不抛**（本仓库的 `CsvDataFeed.get_dividend` 逐字同形）。
    库侧若在"没接源"时抛 DATA_001，同一个 `DataFeed` 协议就会在"分红数据不存在"这件事上
    分裂成两种行为，调用方只能按实现类分别写代码 —— 那正是 D9 要消掉的东西。

    所以这条**两边都断言**：库侧没接源时给 `0.0`，CSV 侧没有 `dividend` 列时也 `0.0`。
    只断言一边时，"两边同口径"只是一句话。
    """
    feed = _feed(dividend_store=None)   # 显式指名"接线不完整"那个形态，不靠默认值撞上
    assert feed.dividend_store is None, "这条要的恰恰是没接源那一支，夹具换了它就该红"
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 5)) == 0.0

    csv_feed = CsvDataFeed(CSV_FIXTURE)  # 夹具里**没有** `dividend` 列
    assert csv_feed.get_dividend(OTHER, datetime(2024, 1, 2)) == 0.0, (
        "两条实现体必须同口径：CSV 侧不抛，库侧也不许抛"
    )


def test_dividend_with_a_store_that_has_no_row_for_that_day_returns_zero():
    """存储接上了、那一天**没有分红行** ⇒ `0.0`（不是报错，也不是拿别的日子顶）。

    带控制样本（同一条 feed 上有行的那一天必须给真值）：只测"该给 0 的给了 0"会把一个
    「整条通道没接上」的实现也判成 PASS —— 而本条要说的恰恰是"接上了、这一天没有"。
    """
    rows = [DividendPoint(SYMBOL, DIVIDEND_EX, DIVIDEND_ANNOUNCE, 0.35,
                          source="fixture", data_version=VERSION)]
    feed = _feed_with_dividends(rows=rows)
    assert feed.dividend_store is not None, "这一支必须是「接上了」那个形态"
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 3)) == 0.0, (
        "MISSING 那天库里没有分红行 ⇒ `0.0`，而不是 DATA_001"
    )
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 5)) == 0.35, (
        "同一条 feed 在有行的那天必须给真值：否则上面那个 0.0 也可以解释成「整条没接」"
    )


def test_dividend_reads_the_real_cash_per_share_per_event_day():
    """控制样本 + 防空转：真读到那一天的值（0.12 / 0.35），且是**逐日**查的。

    "恒返回第一个值"与"按窗口取"在只有一次事件的夹具上给出同一个数；两个不同日子各给
    一个不同的非零值 + 一个没有事件的日子给 `0.0`，三件事合起来才能排除它们。
    """
    feed = _feed_with_dividends()
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 2)) == 0.12
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 5)) == 0.35
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 3)) == 0.0  # 中间那天没有事件


def test_dividend_read_is_guarded_by_pit_too():
    """分红读取也在 PIT 面上：存储忽略窗口 ⇒ 守卫必须炸，且**按公告日**判可见性。

    "拿除权日还是拿公告日当可见性依据"不是学术问题：拿除权日判，会在公告日**晚于**
    `as_of` 时放过一次真实的未来函数（那条分红当时还没公告）。所以夹具里那一天的两列
    **故意不相等**（除权 1/9 / 公告 1/6），断言泄露点的日期**正是公告日** —— 实现里两个
    都写对了这一条才绿。

    这条对应 `tools/verify_data_center_pit.py` 里那条**静态**判据（分红读取也要被守卫
    点名）；静态判据管「有没有写」，这条管「写了到底拦不拦得住」。
    """
    feed = _feed(dividend_store=LyingDividendStore(_dividend_rows()))
    with pytest.raises(FutureDataAccessError) as excinfo:
        feed.get_dividend(SYMBOL, datetime(2026, 1, 5))
    assert excinfo.value.code == "DATA_002"
    points = feed.pit_guard.leakage_points()
    assert [p.trade_date for p in points] == [FUTURE_ANNOUNCE], (
        "记的必须是**公告日**（D4 的可见性依据），不是除权日 %s" % (FUTURE_EVENT,)
    )
    assert points[0].field == "cash_per_share", (
        "分红是**单列**读取 ⇒ field 记真实列名（`DIVIDEND_FIELD`），不是日线那个整行的 'bar'"
    )


def test_dividend_rejects_a_point_after_as_of():
    """`_require_visible` 在**最前面**：问一句"那一天的分红是多少"本身就是一次未来访问。

    夹具里 1/9 那一天**真的有行** ⇒ 这一条同时证明它在问库**之前**就拒了（拿"库里没这一行"
    当解释的实现会在这里返回 0.0）。后半句是控制样本：同一条 feed 在可见日照常给价。
    """
    feed = _feed_with_dividends()
    with pytest.raises(FutureDataAccessError) as excinfo:
        feed.get_dividend(SYMBOL, datetime(2026, 1, 9))
    assert excinfo.value.code == "DATA_002"
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 5)) == 0.35
