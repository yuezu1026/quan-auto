"""I1 竖切的回归测试：CSV → 双均线 → 撮合 → 绩效 → 报告。

**关于「先红后绿」**：I1 的实现写在本文件之前，所以 TDD 的顺序证据不在 git 历史里。
代替它的是 `tools/pytest_mutation_check.py` —— 把实现逐处改坏，确认**本文件真的变红**。
一条永远绿的测试和没有测试是一样的；本仓库对门禁要求「必须做触发测试」，对测试用同一把尺子。

**为什么断言多是「不变量」而不是「这个数应该等于 3」**：数值地板会被下一次调参整片带走，
不变量不会。例如「挂单冻结的钱只能算一次」这条，从 I1 到 I10 都得成立；而
「`max_drawdown < 0.2`」这条的来历写在它自己的 docstring 里（49.9% 那次事故）。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from quanauto.broker import SimulatedBroker
from quanauto.cli import (
    build_config,
    build_parser,
    pick_symbol,
    resolve_strategy_capital,
    run_backtest,
    window_from_feed,
)
from quanauto.datafeed import CsvDataFeed
from quanauto.engine import BacktestEngine, dump_report, report_payload
from quanauto.enums import BacktestStatus, Direction, OrderStatus, OrderType
from quanauto.errors import BacktestExecutionError, InsufficientFundsError, NoResultError
from quanauto.models import BarData, Order, SlippageConfig
from quanauto.strategies import MA_Cross_Strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "sample_prices.csv")
STRATEGY_ID = "ma-cross"


# ── 夹具 ──────────────────────────────────────────────────────────────────
def cli_args(*extra: str, csv_path: str = FIXTURE):
    """`csv_path` 是关键字参数（2026-09-29 晚 **Ⅲ** 加的）：默认那份夹具，
    换别的（如临时带 `adjust_factor` 列的 CSV）时不影响任何既有调用。
    """
    return build_parser().parse_args(["backtest", "--strategy-csv", csv_path, *extra])


def make_engine(seed: int = 7, *extra: str, csv_path: str = FIXTURE, strategy_factory=MA_Cross_Strategy):
    """照 `cli.run_backtest` 的顺序组装引擎（用的是契约里的公开方法）。

    两个可选口子是 2026-09-29 晚 **Ⅲ** 加的，只为让「CSV 侧的复权价」也能被端到端
    观察到：`csv_path` 指向一份带 `adjust_factor` 列的临时 CSV，`strategy_factory` 换成
    一只记录版双均线。两个都不传时行为与之前**逐字一致**。
    """
    args = cli_args(*extra, csv_path=csv_path)
    feed = CsvDataFeed(args.strategy_csv)
    symbol = pick_symbol(feed, args.symbol)
    start, end = window_from_feed(feed, symbol, args.start, args.end)
    config = build_config(args, symbol, start, end)
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
    strategy = strategy_factory(
        args.strategy_id,
        {
            "short_window": int(args.short),
            "long_window": int(args.long),
            "capital": resolve_strategy_capital(args),
            "symbol": symbol,
        },
    )
    engine.add_strategy(strategy, resolve_strategy_capital(args), dict(strategy.get_strategy_params()))
    return engine, feed, symbol


def make_bar(open_price: float = 10.0, close_price: float = 10.0, day: int = 1, symbol: str = "TEST") -> BarData:
    stamp = datetime(2024, 1, 1) + timedelta(days=day)
    return BarData(
        symbol=symbol,
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        volume=1_000_000,
        amount=open_price * 1_000_000,
        datetime=stamp,
        timestamp=int(stamp.timestamp()),
    )


def make_order(direction: Direction = Direction.BUY, quantity: int = 1000, price=None, symbol: str = "TEST") -> Order:
    stamp = datetime(2024, 1, 1)
    return Order(
        order_id="",
        strategy_id=STRATEGY_ID,
        symbol=symbol,
        direction=direction,
        order_type=OrderType.MARKET if price is None else OrderType.LIMIT,
        quantity=quantity,
        price=price,
        stop_price=None,
        status=OrderStatus.PENDING,
        submit_time=stamp,
        last_update_time=stamp,
        filled_quantity=0,
        filled_price=0.0,
        commission=0.0,
        slippage=0.0,
        error_message=None,
    )


def deterministic_text(payload: dict) -> str:
    """只取报告里的 `deterministic` 段。

    比可复现性**只能**比这一段：报告里还有 `runtime.duration_ms`（墙钟耗时）和
    `inputs.seed`。早期版本拿 `dump_report(整份)` 比 —— 那是两头都错的：
      * 跟墙钟耗时比 ⇒ 同一种子两次跑会随机红；
      * 跟 `inputs.seed` 比 ⇒ 「换种子结果变了」会在结果根本没变时也通过（空判据）。
    实测：把 broker.py 里一句注释改掉都能把当时那条「同种子逐字节一致」弄红。
    """
    return json.dumps(payload["deterministic"], sort_keys=True, ensure_ascii=False)


# ── 端到端：这条竖切真的在跑 ───────────────────────────────────────────────
def test_slice_produces_orders_and_trades() -> None:
    """端到端产物非空：整条链路跑得出订单与成交。

    这是「产物」那一行的空转守卫 —— 一个 0 成交的回测照样能导出一份
    看起来完整的报告，指标全是 0，人不去看 `trades` 就发现不了。
    """
    payload = run_backtest(cli_args())
    assert payload["summary"]["trades"] > 0, "回测一笔都没成交，整条竖切等于没跑通"


def test_same_seed_gives_byte_identical_report() -> None:
    """同种子两次跑，报告的确定性段逐字相同（门禁另行逐段比对全文）。"""
    first = deterministic_text(run_backtest(cli_args("--seed", "7")))
    second = deterministic_text(run_backtest(cli_args("--seed", "7")))
    assert first == second, "同种子的两份报告确定性段不一致"


def test_different_seed_changes_deterministic_section_under_slippage() -> None:
    """换种子必须真的改变**确定性段**（默认滑点非 0，所以随机源确实被用到）。

    这条和上一条是一对：只测「同种子一致」时，把随机数抽掉（滑点恒 0）也能全绿，
    那样的报告「可复现」到没有任何随机性，`--seed` 就成了摆设。
    """
    first = deterministic_text(run_backtest(cli_args("--seed", "7")))
    second = deterministic_text(run_backtest(cli_args("--seed", "8")))
    assert first != second, "换种子确定性段没变：随机源没被真正使用（或滑点被悄悄归零）"


def test_report_splits_deterministic_from_runtime() -> None:
    """`seed` 属于 inputs，`duration_ms` 属于 runtime —— 两者都不能混进 deterministic。"""
    payload = run_backtest(cli_args("--seed", "7"))
    assert "seed" in payload["inputs"], "种子没进 inputs，报告无法自证随机源"
    assert "duration_ms" not in payload["deterministic"], "墙钟耗时混进了 deterministic 段"


# ── 引擎不变量 ────────────────────────────────────────────────────────────
def test_equity_curve_has_one_point_per_bar() -> None:
    """净值曲线的点数 == 回测窗口内的 K 线数（少一根就是漏拍快照）。"""
    engine, feed, symbol = make_engine()
    result = engine.run()
    bars = feed.get_bars(symbol, result.start_time, result.end_time)
    assert len(result.equity_curve) == len(bars)


def test_final_equity_matches_last_curve_point() -> None:
    """绩效里的期末净值必须等于曲线最后一个点 —— 两处算同一件事就会漂移。"""
    engine, _, _ = make_engine()
    result = engine.run()
    assert result.performance.final_equity == pytest.approx(result.equity_curve[-1].equity)


def test_max_drawdown_is_plausible() -> None:
    """最大回撤要落在合理范围。

    上限 0.2 不是拍脑袋：`get_account()` 曾经把挂单冻结的钱算两遍
    （`cash + frozen + market_value`，而 `cash` 里本来就含冻结），每根「刚下单」的
    K 线净资产凭空翻倍，净值曲线上每隔几天一个尖峰，`max_drawdown` 报到 49.9%
    （真实值 2.8%）、`sharpe` 报到 1.20（真实值为负）。这条测试就是那次事故的下限守卫。
    """
    engine, _, _ = make_engine()
    result = engine.run()
    assert 0.0 <= result.performance.max_drawdown < 0.2


def test_trades_fill_at_bar_open() -> None:
    """成交价必须是**当根** K 线的开盘价（挂单在上一根下、这一根开盘成交）。

    这条把「下一根开盘撮合」钉成不变量：改成当根 `close` 就变成未来函数，
    而回测报告看上去只会「更赚钱」。
    """
    engine, feed, symbol = make_engine()
    result = engine.run()
    opens = {bar.datetime: bar.open for bar in feed.get_bars(symbol, result.start_time, result.end_time)}
    assert result.trades, "没有成交，无法检验成交价"
    for trade in result.trades:
        assert trade.timestamp in opens, "成交时间戳不在任何一根 K 线的时间上"
        assert opens[trade.timestamp] == pytest.approx(trade.price)


def test_no_leakage_passes_on_a_real_run() -> None:
    """跑完之后未来函数检查必须有真实样本且通过。"""
    engine, _, _ = make_engine()
    engine.run()
    report = engine.validate_no_leakage()
    assert report.is_valid and not report.errors


def test_no_leakage_before_run_reports_warning_instead_of_silent_pass() -> None:
    """没跑过回测时「没有样本」要写进 warnings —— 没有数据不等于通过。"""
    engine, _, _ = make_engine()
    report = engine.validate_no_leakage()
    assert report.warnings, "没跑过回测却报了一个没有任何说明的通过"
    assert report.row_count == 0, "没跑过回测却报了一个非零的已校验行数"


def test_no_leakage_can_actually_fail() -> None:
    """未来函数检查自己也要能被触发 —— 永远绿的检查器等于没有检查器。

    做法是把一笔成交的时间戳改到窗口开始之前（必然早于它的下单时刻），
    检查器必须红。`tools/pytest_mutation_check.py` 的 M5 会把
    `trade.timestamp <= submitted` 改成 `if False:`，靠的就是这条测试来抓。
    """
    engine, _, _ = make_engine()
    result = engine.run()
    assert result.trades, "没有成交，构造不出未来函数样本"
    result.trades[0].timestamp = result.start_time - timedelta(days=1)
    report = engine.validate_no_leakage()
    assert not report.is_valid and report.errors


def test_get_result_before_run_raises() -> None:
    engine, _, _ = make_engine()
    with pytest.raises(NoResultError):
        engine.get_result()


def test_export_report_before_run_raises() -> None:
    engine, _, _ = make_engine()
    with pytest.raises(NoResultError):
        engine.export_report(str(REPO_ROOT / ".rounds" / "should-not-exist.json"))


def test_run_partial_rejects_window_outside_config() -> None:
    """`run_partial` 的区间超出配置区间必须抛，不许悄悄截断。

    断言里必须带 `match`：只写 `pytest.raises(BacktestExecutionError)` 时，把
    「超出区间」那道判断删掉后，这个区间里一根 K 线都没有，`_prepare` 会抛另一条
    `BacktestExecutionError`（"区间内没有任何 K 线"）—— 测试照样绿。
    （实测：变异检查 M7 就是这么一度没抓到。）
    """
    engine, _, _ = make_engine()
    with pytest.raises(BacktestExecutionError, match="超出"):
        engine.run_partial("1990-01-01", "1990-12-31")


def test_report_payload_is_json_serialisable() -> None:
    """报告的序列化路径不能有不可序列化的对象（它是产物，不是内存对象）。"""
    engine, _, _ = make_engine()
    payload = report_payload(engine.run(), 7)
    assert dump_report(payload)


# ── 撮合器不变量 ──────────────────────────────────────────────────────────
def test_pending_buy_does_not_change_total_capital() -> None:
    """挂单只是冻结资金，净资产不变（冻结的钱只能算一次）。"""
    broker = SimulatedBroker(initial_capital=100_000.0, seed=7)
    before = broker.get_account().total_capital
    broker.on_bar(make_bar())
    broker.submit_order(make_order(quantity=1000, price=10.0))
    account = broker.get_account()
    assert account.total_capital == pytest.approx(before), (
        "挂单后净资产变了：可用+冻结+市值 里冻结被算了两次（max_drawdown 虚高的根因）"
    )


def test_submitted_buy_freezes_part_of_the_cash() -> None:
    """另一半：冻结确实发生了（否则「净资产不变」可以靠「什么都不冻结」骗过）。"""
    broker = SimulatedBroker(initial_capital=100_000.0, seed=7)
    broker.on_bar(make_bar())
    before = broker.get_account().available_capital
    broker.submit_order(make_order(quantity=1000, price=10.0))
    assert broker.get_account().available_capital < before


def test_slippage_reduces_available_cash() -> None:
    """滑点必须真的从现金里扣掉，而不只是写进 `Trade.slippage` 好看。"""
    plain = SimulatedBroker(initial_capital=100_000.0, seed=7, slippage=SlippageConfig())
    slipped = SimulatedBroker(
        initial_capital=100_000.0,
        seed=7,
        slippage=SlippageConfig(percentage_slippage=0.01, fixed_slippage=0.0, min_slippage=0.0),
    )
    for broker in (plain, slipped):
        broker.on_bar(make_bar(open_price=10.0))
        broker.submit_order(make_order(quantity=1000))
        broker.on_bar(make_bar(open_price=10.0, day=2))
    assert slipped.get_account().available_capital < plain.get_account().available_capital


def test_oversell_is_rejected_without_raising() -> None:
    """卖超被拒：订单标 REJECTED、不产生成交，且**不抛异常**（拒单是正常业务事件）。"""
    broker = SimulatedBroker(initial_capital=100_000.0, seed=7)
    broker.on_bar(make_bar())
    order = make_order(direction=Direction.SELL, quantity=1000)
    broker.submit_order(order)
    broker.on_bar(make_bar(day=2))
    assert order.status is OrderStatus.REJECTED
    assert broker.get_trades(STRATEGY_ID) == [], "被拒的卖单留下了成交记录"


def test_oversell_leaves_cash_untouched() -> None:
    """卖超被拒之后现金一分不动（拒绝必须发生在改状态之前）。"""
    broker = SimulatedBroker(initial_capital=100_000.0, seed=7)
    broker.on_bar(make_bar())
    before = broker.get_account().available_capital
    broker.submit_order(make_order(direction=Direction.SELL, quantity=1000))
    broker.on_bar(make_bar(day=2))
    assert broker.get_account().available_capital == pytest.approx(before)


def test_rejected_order_keeps_a_reason() -> None:
    """拒单要留下原因：否则「策略发了信号但什么都没发生」在结果里查无实据。"""
    broker = SimulatedBroker(initial_capital=100_000.0, seed=7)
    broker.on_bar(make_bar())
    order = make_order(direction=Direction.SELL, quantity=1000)
    broker.submit_order(order)
    broker.on_bar(make_bar(day=2))
    assert order.error_message


def test_insufficient_cash_is_refused_at_submit_time() -> None:
    """资金不足在下单那一刻就拒绝 —— 拖到成交时才发现，回测会静默跳过订单。"""
    broker = SimulatedBroker(initial_capital=1_000.0, seed=7)
    broker.on_bar(make_bar())
    with pytest.raises(InsufficientFundsError):
        broker.submit_order(make_order(quantity=1000, price=10.0))

# ── CSV 侧的复权口径：与 DB 侧同一份算式、同一形状的控制组 ──────────────
# 背景：`DataFeed` 是**一个**协议、有两条实现体（`CsvDataFeed` / `DbDataFeed`）。
# 只让其中一条真的乘因子时，同一个类名会交出两种**量纲**的价格而两边都不报错 ——
# 而 `engine.py` 又把 `feed.get_adjustment_factor()` 原样写进 `ReportBundle.adjust_factor`，
# 于是 CSV 侧那句因子会与它自己那根 bar 的价格互相矛盾（登记在 DC 契约附录 A6）。
# 下面三条就是把这个口径钉到 CSV 侧：一条证明该乘的乘了、一条证明不该动的一分没动、
# 一条端到端证明「声明的因子」与「实际乘上去的数」是同一个。
CSV_ADJUST_SYMBOL = "600519.SH"
CSV_ADJUST_FACTOR = 2.0   # 故意非 1.0：1.0 的因子会让「乘法没接上」完全隐身
CSV_ADJUST_ROWS = 30


def _adjust_csv_text(factor=None):
    """造一份 CSV；`factor=None` ⇒ **不写** `adjust_factor` 列。返回 `(文本, 逐根原始 close)`。

    四列价格**故意取互不相等的数**（open/high/low/close = base, base+2, base-1, base+1）：
    `open == high == low == close` 的写法会让「只把 close 乘了因子」与「四列都乘了」在
    数值上完全一致，用例对最容易漏的那种改法免疫（同族的教训写在 DB 侧那条
    `test_all_four_price_columns_are_scaled_not_just_close` 的 docstring 里）。

    也**不写** `amount` 列：这样 `amount` 走的是 `close * volume` 折算，正好能测出
    折算用的是**未复权**的 close。
    """
    header = "symbol,datetime,open,high,low,close,volume"
    if factor is not None:
        header += ",adjust_factor"
    lines = [header]
    closes = []
    for index in range(CSV_ADJUST_ROWS):
        base = round(10.0 + index * 0.1, 4)
        day = (datetime(2024, 3, 1) + timedelta(days=index)).strftime("%Y-%m-%d")
        open_price, high = base, round(base + 2.0, 4)
        low, close = round(base - 1.0, 4), round(base + 1.0, 4)
        closes.append(close)
        line = "%s,%s,%s,%s,%s,%s,1000" % (CSV_ADJUST_SYMBOL, day, open_price, high, low, close)
        if factor is not None:
            line += ",%s" % factor
        lines.append(line)
    return "\n".join(lines) + "\n", closes


def test_csv_feed_scales_all_four_price_columns(tmp_path) -> None:
    """CSV 侧的 `adjust_factor` 真的乘到四列价格上，而且**只乘价格**。

    观察点选在 feed 交出来的 `BarData` 上（不是内部字典）：那是策略与引擎唯一看得到的东西。
    """
    text, closes = _adjust_csv_text(factor=CSV_ADJUST_FACTOR)
    path = tmp_path / "prices_with_factor.csv"
    path.write_text(text, encoding="utf-8")
    feed = CsvDataFeed(str(path))
    stamp = datetime(2024, 3, 1)

    bar = feed.get_bar(CSV_ADJUST_SYMBOL, stamp)
    assert bar is not None, "第一根没进索引 ⇒ 下面每条都在描述一个不存在的对象"
    raw = {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0}
    for name, value in raw.items():
        got = getattr(bar, name)
        assert got == pytest.approx(value * CSV_ADJUST_FACTOR), (
            "%s 没乘因子：文件 %.4f ⇒ 该是 %.4f，实际 %.4f"
            % (name, value, value * CSV_ADJUST_FACTOR, got)
        )
        assert got != value, "%s 还是文件里的未复权价 ⇒ 复权没生效" % name

    assert bar.volume == 1000, "成交量不该被乘"
    assert bar.amount == pytest.approx(11.0 * 1000), (
        "成交额不该被乘 —— 缺列时 `close * volume` 的折算也必须用**未复权**的 close"
    )
    assert feed.get_adjustment_factor(CSV_ADJUST_SYMBOL, stamp) == pytest.approx(CSV_ADJUST_FACTOR)

    # 最后一根也乘了：只乘第一根是最容易犯的那种「接了一半」
    last_stamp = datetime(2024, 3, 1) + timedelta(days=CSV_ADJUST_ROWS - 1)
    last = feed.get_bar(CSV_ADJUST_SYMBOL, last_stamp)
    assert last is not None, "最后一根没进索引 ⇒ 上面那句「最后一根也乘了」是空话"
    assert last.close == pytest.approx(closes[-1] * CSV_ADJUST_FACTOR)


def test_csv_feed_without_a_factor_column_leaves_file_prices_untouched() -> None:
    """真夹具没有 `adjust_factor` 列 ⇒ 四列价格**逐位**等于文件里的原值。

    这条是防误报的那一半：上面那条证明「该乘的乘了」，这条证明「不该乘的一分没动」。
    它同时钉住 I1 的证据 —— `.rounds/i1/report-seed7-*.json` 是逐字节比对的，CSV 侧的价格
    只要被乘上一个非 1.0 的数，那份证据就当场作废。期望值**自己从文件里读**，
    不是抄 `CsvDataFeed` 的实现。

    先断言前提：夹具真没有因子列。哪天给夹具加了因子列，这条用例的语义就变了 ——
    那时候该看的是它还要不要存在，而不是让它默默变成一个恒真的断言。
    """
    with open(FIXTURE, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, "夹具读出来 0 行 ⇒ 这条断言形同虚设"
    assert "adjust_factor" not in rows[0], "夹具现在有因子列了 ⇒ 本条用例的前提变了，先看它还要不要"

    feed = CsvDataFeed(FIXTURE)
    for row in rows:
        stamp = datetime.strptime(row["datetime"], "%Y-%m-%d")
        bar = feed.get_bar(row["symbol"], stamp)
        assert bar is not None, "夹具里的行没进索引: %s %s" % (row["symbol"], row["datetime"])
        for name in ("open", "high", "low", "close"):
            expected = float(row[name])
            got = getattr(bar, name)
            assert got == expected, (
                "%s 的 %s 被动过了：文件 %.10g ⇒ feed %.10g" % (row["symbol"], name, expected, got)
            )
        assert feed.get_adjustment_factor(row["symbol"], stamp) == 1.0, "没有因子列时该报 1.0"


class _RecordingMA_Cross(MA_Cross_Strategy):
    """双均线 + 把**真正送到策略手上**的 bundle 记下来（与 DB 侧那只记录版同形）。

    行为与父类完全一致（`on_data` 记一笔就交回去），所以它跑出来的成交与信号跟真跑
    双均线一样 —— 记录版不会把「被测对象」换成另一个东西。
    """

    def __init__(self, strategy_id, config):
        super().__init__(strategy_id, config)
        self.bundles = []

    def on_data(self, data):
        self.bundles.append(data)
        return super().on_data(data)


def test_csv_feed_bundle_factor_matches_the_multiplier_it_applied(tmp_path) -> None:
    """端到端：策略收到的是复权价，而 bundle 声明的因子就是**实际乘上去**的那个数。

    这是 DB 侧那条端到端控制组（`test_adjusted_prices_reach_the_strategy_through_the_engine`）
    在**第二条实现体**上的同形版本。为什么要两边各一条：`DataFeed` 是一个协议、两条实现体
    —— 只端到端钉住一条时，另一条可以悄悄退回不复权口径，而所有单层用例各自仍是绿的。

    钉三件事：① 每一根的 `close` 都是文件里那根 × 因子，且**不等于**原值；② bundle 里那句
    `adjust_factor` 等于实际乘上去的数（策略照它自己除回去必须拿到文件里的原值）；
    ③ 策略真被喂满了 `CSV_ADJUST_ROWS` 根 —— 否则上面两条只是在描述一个没跑起来的策略。
    """
    text, closes = _adjust_csv_text(factor=CSV_ADJUST_FACTOR)
    path = tmp_path / "prices_with_factor.csv"
    path.write_text(text, encoding="utf-8")

    holder = []

    def factory(strategy_id, config):
        strategy = _RecordingMA_Cross(strategy_id, config)
        holder.append(strategy)
        return strategy

    engine, _feed, symbol = make_engine(
        7, "--short", "2", "--long", "3", csv_path=str(path), strategy_factory=factory
    )
    assert symbol == CSV_ADJUST_SYMBOL, "标的选错了 ⇒ 下面每一条都在看另一只股票"
    result = engine.run()
    assert result.status is BacktestStatus.SUCCESS
    assert len(holder) == 1, "策略工厂没被调用 ⇒ 下面每条都在描述一个没跑起来的策略"
    bundles = holder[0].bundles
    assert len(bundles) == CSV_ADJUST_ROWS, (
        "策略收到 %d 根，该是 %d 根" % (len(bundles), CSV_ADJUST_ROWS)
    )

    for index, bundle in enumerate(bundles):
        raw = closes[index]
        assert bundle.close == pytest.approx(raw * CSV_ADJUST_FACTOR), (
            "第 %d 根没有乘上因子：文件 %.4f ⇒ 策略该看到 %.4f，实际 %.4f"
            % (index, raw, raw * CSV_ADJUST_FACTOR, bundle.close)
        )
        assert bundle.close != raw, "第 %d 根还是文件里的未复权价 ⇒ 复权没生效" % index
        assert bundle.adjust_factor == pytest.approx(CSV_ADJUST_FACTOR)
        assert bundle.close / bundle.adjust_factor == pytest.approx(raw), (
            "第 %d 根：bundle 声明的因子与实际乘上去的数不是同一个，策略自己算一遍会有第二个答案"
            % index
        )