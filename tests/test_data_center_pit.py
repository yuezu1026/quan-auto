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

from datetime import date, datetime
from typing import List

import pytest

from quanauto.datacenter import (
    DailyBar,
    DbDataFeed,
    InMemoryBarStore,
    InMemoryDataCenter,
    LeakagePoint,
    SessionMode,
)
from quanauto.enums import AdjustType, FillPolicy, MarketStatus
from quanauto.errors import DataVersionError, FutureDataAccessError

SYMBOL = "600000.SH"
OTHER = "000001.SZ"
VERSION = "v2026.09.23"

VISIBLE = date(2026, 1, 2)  # 早于 as_of，必须可见
MISSING = date(2026, 1, 3)  # 落在区间里但库里没有 —— 用来证明"空"和"被挡住"不是一回事
ON_AS_OF = date(2026, 1, 5)  # 正好等于 as_of，必须可见
FUTURE = date(2026, 1, 6)  # 晚于 as_of，一次都不许露出来
AS_OF = ON_AS_OF


def _row(symbol: str, trade_date: date, close: float = 10.0, volume: float = 1000.0) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        open=close,
        high=close,
        low=close,
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


def _center(store=None, **kwargs) -> InMemoryDataCenter:
    return InMemoryDataCenter(
        store if store is not None else InMemoryBarStore(_rows()), **kwargs
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


# ── 已知缺口：钉住，不让它悄悄变成"看起来对" ──────────────────────────────


def test_known_gap_hfq_factor_is_identity_in_s1():
    """⚠️ 这条**故意**断言一个错的结果：I2 S1 没接复权数据，HFQ 因子恒 1.0（= 不复权）。

    等 S3 接上 `dc_adjust_factor` 之后它会变红。那一刻要做的**不是**把期望值改成新数，
    而是删掉这条测试并在文档里销掉"已知缺口"那一项。留一条能红的测试，
    好过在 docstring 里写一句"暂未实现" —— 后者没有任何机制会在实现之后提醒你。
    """
    feed = _feed()
    assert feed.adjust_type is AdjustType.HFQ
    assert feed.get_adjustment_factor(SYMBOL, datetime(2026, 1, 5)) == 1.0
    assert feed.get_dividend(SYMBOL, datetime(2026, 1, 5)) == 0.0
