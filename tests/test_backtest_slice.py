"""I1 竖切的回归测试：CSV → 双均线 → 撮合 → 绩效 → 报告。

**关于「先红后绿」**：I1 的实现写在本文件之前，所以 TDD 的顺序证据不在 git 历史里。
代替它的是 `tools/pytest_mutation_check.py` —— 把实现逐处改坏，确认**本文件真的变红**。
一条永远绿的测试和没有测试是一样的；本仓库对门禁要求「必须做触发测试」，对测试用同一把尺子。

**为什么断言多是「不变量」而不是「这个数应该等于 3」**：数值地板会被下一次调参整片带走，
不变量不会。例如「挂单冻结的钱只能算一次」这条，从 I1 到 I10 都得成立；而
「`max_drawdown < 0.2`」这条的来历写在它自己的 docstring 里（49.9% 那次事故）。
"""

from __future__ import annotations

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
from quanauto.enums import Direction, OrderStatus, OrderType
from quanauto.errors import BacktestExecutionError, InsufficientFundsError, NoResultError
from quanauto.models import BarData, Order, SlippageConfig
from quanauto.strategies import MA_Cross_Strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "sample_prices.csv")
STRATEGY_ID = "ma-cross"


# ── 夹具 ──────────────────────────────────────────────────────────────────
def cli_args(*extra: str):
    return build_parser().parse_args(["backtest", "--strategy-csv", FIXTURE, *extra])


def make_engine(seed: int = 7, *extra: str):
    """照 `cli.run_backtest` 的顺序组装引擎（用的是契约里的公开方法）。"""
    args = cli_args(*extra)
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
    strategy = MA_Cross_Strategy(
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
    assert report.is_valid and not report.issues


def test_no_leakage_before_run_reports_warning_instead_of_silent_pass() -> None:
    """没跑过回测时「没有样本」要写进 warnings —— 没有数据不等于通过。"""
    engine, _, _ = make_engine()
    report = engine.validate_no_leakage()
    assert report.warnings, "没跑过回测却报了一个没有任何说明的通过"


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
    assert not report.is_valid and report.issues


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
