"""I2 S3 后半：**回测真的去读日线** —— 引擎接 `DataCenter.as_of()` 产出的 feed。

## 为什么这一份必须是端到端的

I2 的两个半边各自都有测试：S1 的 `tests/test_data_center_pit.py` 证明 `DbDataFeed` 的两层
防护会拦越界，S3 前半的 `tests/test_data_center_store.py` 证明 `PgBarStore` 发出去的语句
与参数是对的。**但「半边各自都对」不等于「接起来能跑」** —— 两个套件都在自己那一层停下：
一个拿 `InMemoryBarStore` 当存储（从不经过 `DailyBar → BarData` 的数值转换），
另一个从不把 bar 交给引擎。

本文件让他们真的碰一次：
`PgBarStore / InMemoryBarStore → InMemoryDataCenter.as_of → DbDataFeed → BacktestEngine.run → 报告`。
口径与那两份一致：**一律不连库**，假连接只负责回答 `PgBarStore` 的两条读语句。

## 它咬出来的两个真实缺陷（先红后绿的那一次）

1. `PgBarStore` 交出来的是 `Decimal` —— S3 前半的用例**故意**断言了这一点
   （`assert isinstance(bar.close, Decimal)`：存储层要靠精确比较判「同值不写」），
   而契约 §2.1.1 的 `BarData` 数值字段是 `float`。第一次成交时 `float * Decimal`
   直接 `TypeError`：也就是说在 S3 后半之前，**「回测读库」这条路径一次都没跑通过**，
   只是从没人把两个半边接起来过。
2. 引擎的数据版本一直从 `csv_path` 拼（`getattr(feed, "csv_path", "")`），`DbDataFeed`
   没有这个属性 ⇒ 报告里写的是 `600000.SH::35` —— 标的 + 空路径 + **根数**。
   根数不是版本：换一份 `data_version` 重采，只要天数一样，报告一模一样，D8 要的
   可复现性标记在这里是瞎的。

## 断言口径

除了数据版本本身（那就是被测的东西），其余一律用**不变量**：`account_history` 的长度必须
等于**可见**交易日数而不是库里总行数；`validate_no_leakage().row_count` 必须 > 0
（拿 0 笔成交去证明「没有未来函数」是空判据）。数值地板会被下一次调参整片带走，不变量不会。
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from quanauto.cli import (
    build_config,
    build_parser,
    pick_symbol,
    window_from_feed,
)
from quanauto.datafeed import CsvDataFeed
from quanauto.datacenter import (
    MIN_DATE,
    AdjustFactorPoint,
    DailyBar,
    DbDataFeed,
    InMemoryBarStore,
    InMemoryDataCenter,
    InMemoryFactorStore,
)
from quanauto.engine import BacktestEngine, dump_report, report_payload
from quanauto.enums import BacktestStatus
from quanauto.errors import DataVersionError, FutureDataAccessError
from quanauto.pgstore import SQL_SELECT_BARS, SQL_SELECT_SYMBOLS, PgBarStore
from quanauto.broker import SimulatedBroker
from quanauto.strategies import MA_Cross_Strategy

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "sample_prices.csv")
CSV_NAME = "sample_prices.csv"

SYMBOL = "600000.SH"
VERSION = "v2026.01.05"
OTHER_VERSION = "v2026.01.06"
STRATEGY_ID = "ma-cross"

FIRST_DAY = date(2026, 1, 5)
VISIBLE = 35      # 落在 as_of 当天及之前：回测能看见的
FUTURE = 10       # 只存在于库里：一次都不许进回测
FLAT = 25         # 前 25 根横盘，之后直线拉起 ⇒ MA5 必然上穿 MA20，必然有成交
FACTOR = 2.0      # 端到端控制组用的累计因子：故意非 1.0，否则"乘过没有"看不出来


def _days():
    return [FIRST_DAY + timedelta(days=i) for i in range(VISIBLE + FUTURE)]


AS_OF = _days()[VISIBLE - 1]      # 2026-02-08


def _close(index: int) -> float:
    """横盘 25 根再拉起：保证均线真的交叉，而不是靠「反正没信号也算跑通」蒙过。"""
    return 10.0 if index < FLAT else 10.0 + (index - FLAT + 1)


def _bars(version: str = VERSION, symbol: str = SYMBOL):
    """`DailyBar` 是**存储形状**：数值用 `Decimal`（`PgBarStore` 就是这么交出来的）。"""
    out = []
    for index, day in enumerate(_days()):
        price = Decimal(str(_close(index)))
        out.append(
            DailyBar(
                symbol=symbol,
                trade_date=day,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal("1000000"),
                amount=price * Decimal("1000000"),
                source="akshare",
                data_version=version,
            )
        )
    return out


def _bar_rows(version: str = VERSION):
    """假库里的行：数值列故意给**字符串**，走 `Decimal(str(...))` 那条真实通道。"""
    return [
        {
            "symbol": bar.symbol,
            "trade_date": bar.trade_date,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "amount": str(bar.amount),
            "source": bar.source,
            "data_version": bar.data_version,
        }
        for bar in _bars(version)
    ]


def _factors(version: str = VERSION, symbol: str = SYMBOL, factor: float = 1.0):
    """`AdjustFactorPoint` 是**存储形状**，与 `DailyBar` 一样由存储层交出来。

    默认给 **1.0**（2026-09-29 晚起口径变了）：`DbDataFeed._row_to_bar` 现在**真的**会用
    这个数去乘四列价格（D6 第二句「读取时现算」），所以 1.0 意味着"这次没有复权事件"，
    读出来的 bar 与库里的值**逐位相同** —— 本文件那些"看根数、看版本、看有没有越界"的
    断言就仍然在说它们本来要说的事。

    要验"因子真的乘上去了"的用例把 `factor` 传成非 1.0（见
    `test_adjusted_prices_reach_the_strategy_through_the_engine`）。
    """
    return [
        AdjustFactorPoint(
            symbol=symbol,
            trade_date=day,
            adjust_factor=factor,
            source="fixture",
            data_version=version,
        )
        for day in _days()
    ]


class FakeConn:
    """只回答 `PgBarStore` 的两条读语句，并记下每一次调用。

    它证明的是**我们发了什么语句、绑了什么参数**，不是 PostgreSQL 会接受它 ——
    后者只有容器里的真库能证（`tools/run_sql_smoke.py`，本轮不在门禁内）。
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if sql == SQL_SELECT_SYMBOLS:
            (version,) = params
            seen = sorted({row["symbol"] for row in self.rows if row["data_version"] == version})
            return [{"symbol": symbol} for symbol in seen]
        if sql == SQL_SELECT_BARS:
            symbol, version, start, end = params
            picked = [
                row
                for row in self.rows
                if row["symbol"] == symbol
                and row["data_version"] == version
                and start <= row["trade_date"] <= end
            ]
            return sorted(picked, key=lambda row: row["trade_date"])
        raise AssertionError("落库侧发了一条我们不认识的语句：%r" % (sql,))

    def transaction(self):
        raise AssertionError("读侧不该开事务")


class LyingBarStore(InMemoryBarStore):
    """`WHERE trade_date <= as_of` 写错的存储：忽略窗口，把该标的的全部行都吐出来。"""

    def select_bars(self, symbol, start, end):
        return sorted((row for row in self.rows if row.symbol == symbol), key=lambda row: row.trade_date)


class RecordingMA_Cross(MA_Cross_Strategy):
    """双均线 + 把**真正送到策略手上的** bundle 记下来。

    观察点选在策略这一侧（不是 feed、不是引擎的内部字段）：从存储到策略之间的每一步
    ——`Decimal → float`、乘因子、`build_bundle` 组装、事件派发——任何一步失效，都会在
    这里表现为一个**数值不对**，而不是一句"内部字段看着还行"。

    行为与父类完全一致（`on_data` 记一笔就交回去），所以它跑出来的成交与信号跟真跑
    双均线一样。
    """

    def __init__(self, strategy_id, config):
        super().__init__(strategy_id, config)
        self.bundles = []

    def on_data(self, data):
        self.bundles.append(data)
        return super().on_data(data)


# ── 组装 ──────────────────────────────────────────────────────────────────
def _center(store=None, version: str = VERSION, factor: float = 1.0) -> InMemoryDataCenter:
    if store is None:
        store = InMemoryBarStore(_bars(version))
    # `as_of()` 默认口径是 `HFQ`（DC 契约 §2.3 行 265：后复权 = 回测默认）⇒ 因子存储
    # 是**必需的前置条件**，不是可选装饰：不给它，读**任何一根** K 线都会按
    # §2.4「找不到数据必须显式失败」抛 DATA_001（复权价在读取时现算，见下）。
    # 一次性接在这里，`as_of()` 那条链上的每个调用点就都满足了。
    return InMemoryDataCenter(
        store,
        versions=(version,),
        active_version=version,
        factor_store=InMemoryFactorStore(_factors(version, factor=factor)),
    )


def _feed(center: InMemoryDataCenter) -> DbDataFeed:
    feed = center.as_of(AS_OF)
    assert isinstance(feed, DbDataFeed), "as_of() 是产出 DataFeed 的唯一入口（D3）"
    return feed


def _config(symbol: str, start: datetime, end: datetime):
    """借 CLI 的 `build_config` 拼那 18 个必填字段。

    它只读 `args`，**不碰** `--strategy-csv` 那个文件；本文件的数据一律来自存储层，
    真去 `CsvDataFeed(...)` 一下就正好把 D3 想防的事做了一遍。
    """
    args = build_parser().parse_args(["backtest", "--strategy-csv", CSV_FIXTURE])
    return build_config(args, symbol, start, end)


def _strategy_config(symbol: str, capital: float) -> dict:
    return {"short_window": 5, "long_window": 20, "capital": capital, "symbol": symbol}


def _build_engine(
    feed,
    symbol: str = SYMBOL,
    start: datetime = None,
    end: datetime = None,
    seed: int = 7,
    strategy_factory=MA_Cross_Strategy,
):
    """`strategy_factory(strategy_id, config) -> Strategy`：默认双均线。

    留这个口子是为了让"策略**真正收到**的 `MarketDataBundle`"可被观察
    （`test_adjusted_prices_reach_the_strategy_through_the_engine` 用一只记录版
    双均线）。口径一致：工厂拿到的 `config` 与默认实现**同一个**，所以记录版策略
    收到的 bundle 就是真实策略会收到的那个。
    """
    start = start if start is not None else datetime.combine(FIRST_DAY, time())
    end = end if end is not None else datetime.combine(AS_OF, time())
    config = _config(symbol, start, end)
    engine = BacktestEngine(config)
    engine.add_datafeed(symbol, feed)
    engine.set_broker(
        SimulatedBroker(
            initial_capital=config.initial_capital,
            seed=seed,
            commission=config.commission_config,
            slippage=config.slippage_config,
        )
    )
    capital = config.initial_capital * 0.9
    strategy = strategy_factory(STRATEGY_ID, _strategy_config(symbol, capital))
    engine.add_strategy(strategy, capital, dict(strategy.get_strategy_params()))
    return engine


def _run(feed):
    return _build_engine(feed).run()


# ── 数据版本：报告里那串必须来自存储层（D8） ──────────────────────────────
def test_report_data_version_is_the_stores_data_version():
    """D8：报告里的数据版本 = 这份快照的 `data_version`，不是 CSV 路径的替身。

    今天实现从 `getattr(feed, "csv_path", "")` 拼，`DbDataFeed` 没有这个属性 ⇒
    报告里出来的是 `600000.SH::35`：标的、**空路径**、根数。看着像版本，其实是根数。
    """
    result = _run(_feed(_center()))
    assert result.data_version == "%s@%s" % (SYMBOL, VERSION)


def test_two_snapshots_with_the_same_bar_count_do_not_collide():
    """两份不同的 `data_version`，根数完全相同 ⇒ 报告必须给出不同的版本串。

    这是「根数不是版本」的直接反例：只要报告靠根数区分数据，这条就红。
    """
    first = _run(_feed(_center(version=VERSION)))
    second = _run(_feed(_center(version=OTHER_VERSION)))
    assert first.data_version != second.data_version, (
        "两份不同的数据版本给出了同一个串 %r —— 它没在描述数据" % (first.data_version,)
    )


def test_a_feed_that_declares_a_blank_version_is_refused_not_faked():
    """版本字段是空串 ⇒ 抛 `DataVersionError`，不许照旧盖一个假章上去。

    `as_of()` 挡得住未知版本，挡不住「版本字段本身是空的」—— 那要在引擎盖章之前判。
    一纸 `600000.SH::35` 出了报告，读它的人没有任何办法知道这轮读的是哪份数据。
    """
    feed = DbDataFeed(
        InMemoryBarStore(_bars()),
        AS_OF,
        "",
        factor_store=InMemoryFactorStore(_factors()),
    )
    with pytest.raises(DataVersionError) as excinfo:
        _build_engine(feed).run()
    assert excinfo.value.code == "DATA_004"


def test_csv_feed_version_string_keeps_its_old_shape():
    """CSV 那一支**不许**跟着改：`.rounds/i1` 的报告是逐字节比对的证据。

    期望值独立算出来（自己读 CSV 数数据行），不是抄实现里那两行。
    """
    with open(CSV_FIXTURE, encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle.read().replace("\r\n", "\n").split("\n") if line.strip()]
    data_rows = len(lines) - 1
    assert data_rows > 0, "夹具读出来 0 行 ⇒ 这条断言形同虚设"

    feed = CsvDataFeed(CSV_FIXTURE)
    symbol = pick_symbol(feed, None)
    start, end = window_from_feed(feed, symbol, None, None)
    result = _build_engine(feed, symbol=symbol, start=start, end=end).run()
    assert result.data_version == "%s:%s:%d" % (symbol, CSV_NAME, data_rows)


# ── 端到端跑通 ────────────────────────────────────────────────────────────
def test_engine_runs_end_to_end_over_a_store_backed_feed():
    """可见的 35 根进曲线，库里那 10 根未来行一次都不进。"""
    feed = _feed(_center())
    result = _build_engine(feed).run()
    assert result.status is BacktestStatus.SUCCESS
    assert len(result.account_history) == VISIBLE, (
        "库里有 %d 行，回测只该用 as_of 之内的 %d 行" % (VISIBLE + FUTURE, VISIBLE)
    )
    assert len(result.trades) >= 1, "横盘后拉起、MA5 必然上穿 MA20：一笔都没有说明信号没跑起来"
    report = result.validation_report
    assert report is not None and report.is_valid
    assert report.row_count > 0, "0 笔样本的「没有未来函数」是空判据"


def test_adjusted_prices_reach_the_strategy_through_the_engine():
    """库里的不复权价 × 因子 ⇒ 策略收到的是复权价。这是本轮的**端到端控制组**。

    为什么必须端到端：`tests/test_data_center_pit.py` 的读侧用例证明 `get_bar` 会乘
    因子，本文件上面那条证明引擎能跑 —— **两边各自绿只说明各自自洽**，说明不了
    "乘过之后的价格真的到了策略手上"。中间任何一步把 bar 换回未复权（或只把
    `adjust_factor` 写进 bundle 而忘了乘价），都只会在这里露出来。

    因子取 2.0（不是 1.0），这样"乘过"与"没乘"一眼可分 —— 1.0 的夹具会让这条用例
    对"乘法没接上"完全免疫。

    钉五件事：
    ① 逐根：`close` = 库里那根 × 因子，且**不等于**库里的原值（未复权值不许漏出来）；
    ② `open/high/low` 也乘了 —— 只乘 `close` 是最容易漏的一种；
    ③ 策略真的被喂了 `VISIBLE` 根（防止"一条断言都在描述一个没跑起来的策略"）；
    ④ `previous_close` 是**前一根复权后**的 close，第一根退到**复权后**的 open；
    ⑤ bundle 里那句 `adjust_factor` 等于**实际乘上去**的那个数 —— 策略照它自己算一遍
       不该算出第二个答案；`volume`/`amount` **故意没乘**（B21 只谈价格口径）。

    变异归属（`tools/pytest_mutation_check.py`，2026-09-29 晚按实测报告回填）：本文件侧的
    五条是 `S11-store-decimals-leak-into-the-engine`（拿掉 `_as_float`，`Decimal` 漏进引擎）、
    `A11-volume-scaled-along-with-the-price`（把股数也乘了）、
    `A12-bundle-factor-not-from-the-feed`（bundle 里的因子钉成 1.0）—— **`A12` 全仓库只由
    这一条用例抓住**，所以"这里红"与"复权真的断了"是同一件事；
    数据层那侧的 `A6-qfq-base-factor-ignored` / `A8-rescale-price-becomes-identity` 也打红本条
    （它们改的是价格本身，端到端自然跟着红）。只顾 `close` 的那条（`A10`）由
    `tests/test_data_center_pit.py` 的 `test_all_four_price_columns_are_scaled_not_just_close` 负责。
    """
    feed = _feed(_center(factor=FACTOR))
    holder = []

    def factory(strategy_id, config):
        strategy = RecordingMA_Cross(strategy_id, config)
        holder.append(strategy)
        return strategy

    result = _build_engine(feed, strategy_factory=factory).run()
    assert result.status is BacktestStatus.SUCCESS
    assert len(holder) == 1, "策略工厂没被调用 ⇒ 下面每一条都在描述一个没跑起来的策略"
    bundles = holder[0].bundles
    assert len(bundles) == VISIBLE, (
        "as_of 之内共 %d 根，策略收到 %d 根" % (VISIBLE, len(bundles))
    )

    for index, (bundle, day) in enumerate(zip(bundles, _days())):
        raw = _close(index)
        assert bundle.symbol == SYMBOL and bundle.datetime.date() == day, (
            "第 %d 根的顺序/标的不对：%s" % (index, bundle.datetime)
        )
        assert bundle.close == pytest.approx(raw * FACTOR), (
            "第 %d 根没有乘上因子：库里 %.4f ⇒ 策略该看到 %.4f，实际 %.4f"
            % (index, raw, raw * FACTOR, bundle.close)
        )
        assert bundle.close != raw, "第 %d 根还是库里那个未复权价 ⇒ 复权没生效" % index

    for name in ("open", "high", "low", "close"):
        assert getattr(bundles[-1], name) == pytest.approx(_close(VISIBLE - 1) * FACTOR), (
            "四列价格都要乘因子，%s 没乘" % name
        )

    assert bundles[0].previous_close == pytest.approx(_close(0) * FACTOR), (
        "第一根没有前收盘 ⇒ 契约口径退到 bar.open，而且必须是**复权后**的 open"
    )
    assert bundles[1].previous_close == bundles[0].close, (
        "第二根的 previous_close 就该是第一根复权后的 close（引擎递的是它自己的上一根）"
    )

    last = bundles[-1]
    assert last.adjust_factor == pytest.approx(FACTOR)
    assert last.close / last.adjust_factor == pytest.approx(_close(VISIBLE - 1)), (
        "bundle 声明的因子与实际乘上去的数必须是同一个，否则策略自己算一遍会有第二个答案"
    )
    assert last.volume == 1000000, "成交量不该被乘"
    assert last.amount == pytest.approx(_close(VISIBLE - 1) * 1000000), "成交额不该被乘"


def test_the_same_snapshot_twice_gives_the_same_deterministic_section():
    """两个新引擎读同一份快照 ⇒ 报告的 `deterministic` 段逐字节相同。

    这条在版本进报告之前是**假绿**（`data_version` 恒等于根数，怎么跑都一样）；
    现在它才有内容 —— 版本串是这一段里唯一能区分两份数据的东西，所以顺手钉住它。
    """
    first = report_payload(_run(_feed(_center())), 7)
    second = report_payload(_run(_feed(_center())), 7)
    assert first["deterministic"] == second["deterministic"]
    assert first["deterministic"]["data_version"] == "%s@%s" % (SYMBOL, VERSION)
    assert "%s@%s" % (SYMBOL, VERSION) in dump_report(first), "版本串要真的进得了落盘的那份报告"


def test_the_session_guard_sees_every_row_the_engine_read():
    """引擎跑完之后，会话守卫要能回答「读过多少行、有没有越界」。

    这是 S1 留下的那个会话对象唯一的用处 —— 若它在引擎这条链路上恒为 `count=0`，
    `PITReport` 就是一张永远填不上的表。
    """
    feed = _feed(_center())
    _build_engine(feed).run()
    assert feed.pit_guard.count > 0
    assert feed.pit_guard.report().is_consistent is True
    assert feed.pit_guard.leakage_points() == []


# ── 两条防线的**端到端**控制组 ────────────────────────────────────────────
def test_window_past_as_of_is_refused_by_the_engine_not_truncated():
    """第一层：窗口越过 `as_of` ⇒ 抛，不是悄悄裁到 `as_of` 为止。

    「裁剪」会让一条越界请求拿到一份看起来正常的报告，而它问的日期根本没被回答。
    """
    feed = _feed(_center())
    end = datetime.combine(AS_OF + timedelta(days=1), time())
    with pytest.raises(FutureDataAccessError) as excinfo:
        _build_engine(feed, end=end).run()
    assert excinfo.value.code == "DATA_002"


def test_a_store_that_ignores_the_window_is_caught_through_the_engine():
    """第二层：存储忽略窗口 ⇒ 引擎这条调用链上就得炸。

    `tests/test_data_center_pit.py` 已经证过「feed 自己会拦」。但「feed 会拦」和
    「引擎拿到的 feed 是会拦的那个」是两件事，后者要一次真实的引擎组装才算数。
    这里在 `add_datafeed`（它要问 `get_available_symbols()`）就炸 —— 早炸，不带病上路。
    """
    feed = _feed(_center(store=LyingBarStore(_bars())))
    with pytest.raises(FutureDataAccessError) as excinfo:
        _build_engine(feed)
    assert excinfo.value.code == "DATA_002"
    assert "PITGuard" in str(excinfo.value), "必须是第二层拦住的那一条，而不是别处顺手抛的"


# ── S3 前半那个读侧真的接得上 ─────────────────────────────────────────────
def test_pgbarstore_plugs_into_the_engine_and_binds_the_data_version():
    """`PgBarStore` 能当 `BarStore` 用：真语句、真绑版本、真出报告。"""
    conn = FakeConn(_bar_rows(VERSION))
    store = PgBarStore(conn, VERSION)
    center = _center(store=store, version=VERSION)
    result = _run(_feed(center))

    assert result.data_version == "%s@%s" % (SYMBOL, VERSION)
    selects = [call for call in conn.calls if call[0] == SQL_SELECT_BARS]
    assert selects, "引擎一次都没读到 dc_daily_bar"
    assert all(params[0] == SYMBOL for _sql, params in selects), "只该读回测那个标的"
    assert all(params[1] == VERSION for _sql, params in selects), "每次读都要绑版本（D8）"
    assert (SQL_SELECT_SYMBOLS, (VERSION,)) in conn.calls

    # 防空转：未来那 10 行**在库里**且读得出来。否则「没进回测」只是因为它本来就不在，
    # 这条用例就变成了一句同义反复。
    everything = store.select_bars(SYMBOL, MIN_DATE, date(2100, 1, 1))
    assert len(everything) == VISIBLE + FUTURE, "库里必须有未来行，藏起来才叫 feed 的功劳"
    assert len(conn.calls) > 0, "假连接必须真的被用过，否则上面几条都在描述一个没跑起来的流程"
