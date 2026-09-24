"""I3 端到端：回测下单前必经 `RiskEngine`，KillSwitch 触发后市场侧订单数为 0。

**这份文件要证的三件事**（对应 `docs/迭代计划.md` §I3 的「产物」与「触发测试」两行）：

1. **不接风控时，订单流与 I1 完全一致** —— 闸门是显式接上的（`attach_risk_engine`），
   不是偷偷默认开的。默认开会让契约 §3.1.1 的「单票 ≤ 10%」把 I1 按 90% 建仓的每一单
   都缩量，`.rounds/i1/` 的复现证据当场作废。
2. **接上之后，开仓单在进撮合器之前被缩量或拦下**；平仓单不受「开仓类」规则牵连
   （契约 D5：异常状态也要留一条出货的路）。缩到 0 股时按拦截处理 —— 0 股订单交给
   撮合器只会被拒，还会把拒绝原因写成一条误导人的「资金不足」。
3. **`KillSwitch.activate()` 之后进市场的订单数为 0**，且触发**之前**已提交的订单不受影响
   （拦截的是「后续」下单，不是回头篡改历史）。

**为什么断言都跑完整回测，而不是直接调 `_risk_gate()`**：闸门是「接线」的一部分。
只测函数会漏掉三类真实的错 —— 接在 `_bar_handler` 的哪一步、计数放在提交前还是提交后、
`_reset_risk_run_state()` 有没有落在 `_prepare()` 里。跑完整回测还能顺带把
`risk_summary()` 的统计与**市场侧的订单/成交记录**对账（`checked == len(result.orders)`）。

**为什么先有实现再有这份测试**：I3 的实现写在它之前，所以「先红后绿」的顺序证据不在
git 历史里。代替它的是 `tools/pytest_mutation_check.py` —— 把闸门逐处改坏，确认本文件
真的变红（一条永远绿的测试和没有测试是一样的）。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quanauto.broker import SimulatedBroker
from quanauto.cli import (
    build_config,
    build_parser,
    pick_symbol,
    resolve_strategy_capital,
    window_from_feed,
)
from quanauto.datafeed import CsvDataFeed
from quanauto.engine import BacktestEngine, report_payload
from quanauto.enums import Direction, OrderStatus
from quanauto.errors import ConfigValidationError, RiskConfigInvalidError
from quanauto.risk import (
    KillSwitch,
    MemoryRiskRuleStore,
    RiskEngine,
    RiskEngineConfig,
    default_global_rules,
)
from quanauto.strategies import LOT_SIZE, MA_Cross_Strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "sample_prices.csv")
STRATEGY_ID = "ma-cross"


def _load_gate_module():
    """把**门禁本体**当模块加载，而不是抄一份它的常量。

    抄一份的后果是两边各自漂移：门禁改了键集，这里仍拿旧键集断言 ⇒ 测试永远绿。
    加载而不是 import，是因为 `tools/` 不是包（没有 `__init__.py`）。
    """
    path = REPO_ROOT / "tools" / "verify_backtest_reproducibility.py"
    spec = importlib.util.spec_from_file_location("_gate_reproducibility", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_gate_module()
GATE_DETERMINISTIC_KEYS = frozenset(GATE.DETERMINISTIC_KEYS)
GATE_SCHEMA = GATE.SCHEMA
# `inputs` / `runtime` 两段门禁只做形式检查（`runtime` 整段被排除），键集写在这里。
GATE_INPUTS_KEYS = frozenset({"seed"})
GATE_RUNTIME_KEYS = frozenset({"duration_ms", "start_time", "end_time"})
# 只用来兜住「键名恰好不含 risk」这种巧合：**不**作为主判据（主判据是上面的精确键集）。
RISK_KEY_HINTS = frozenset(
    {"risk", "risk_summary", "blocked", "blocked_orders", "violations", "kill_switch", "run_state"}
)

# 契约 §3.1.1 的 GLOBAL 单票上限。写成**字面量**而不是从 `quanauto.risk` 里读：
# 读的话，阈值被改坏时这条断言会跟着一起变 —— 那就成了空判据。
MAX_POSITION_PCT = 0.10
# 放宽到「不拦」的阈值。RATIO 的合法域是 (0, 1]（`validate_threshold`），1.0 就是上限。
WIDEST = {
    "max_position_pct": 1.0,
    "max_daily_trades": 999,
    "strategy_drawdown_pct": 1.0,
    "account_drawdown_pct": 1.0,
    "max_order_amount_pct": 1.0,
    "max_sector_pct": 1.0,
}


# ── 夹具 ──────────────────────────────────────────────────────────────────
def cli_args(*extra: str):
    return build_parser().parse_args(["backtest", "--strategy-csv", FIXTURE, *extra])


def widened_rules(**overrides) -> list:
    """把 GLOBAL 阈值放宽到不拦（用来做「不管阈值的接线」这类对照）。"""
    thresholds = dict(WIDEST)
    thresholds.update(overrides)
    return [replace(rule, threshold=thresholds[rule.rule_id]) for rule in default_global_rules()]


def build(*, attach: bool = False, load: bool = True, rules=None, store=None,
          kill_switch=None, strategy_factory=None):
    """照 `cli.run_backtest` 的顺序组装引擎（用的是契约里的公开方法）。

    `strategy_factory(symbol, capital, args)` 只为「需要自定义策略」的用例而留；
    默认就是 I1 的双均线。
    """
    args = cli_args()
    feed = CsvDataFeed(args.strategy_csv)
    symbol = pick_symbol(feed, args.symbol)
    start, end = window_from_feed(feed, symbol, args.start, args.end)
    config = build_config(args, symbol, start, end)
    broker = SimulatedBroker(
        initial_capital=config.initial_capital,
        seed=int(args.seed),
        commission=config.commission_config,
        slippage=config.slippage_config,
    )
    engine = BacktestEngine(config)
    engine.add_datafeed(symbol, feed)
    engine.set_broker(broker)
    capital = resolve_strategy_capital(args)
    if strategy_factory is None:
        strategy = MA_Cross_Strategy(
            args.strategy_id,
            {
                "short_window": int(args.short),
                "long_window": int(args.long),
                "capital": capital,
                "symbol": symbol,
            },
        )
    else:
        strategy = strategy_factory(symbol, capital, args)
    engine.add_strategy(strategy, capital, dict(strategy.get_strategy_params()))

    risk = None
    if attach:
        engine_store = store if store is not None else MemoryRiskRuleStore(
            rules if rules is not None else default_global_rules()
        )
        # `lkg_cache_path=""`：`MemoryRiskRuleStore` 本来就没有持久化能力，这里只是把
        # 「测试不写盘」写死，免得将来换成带持久化的 store 时悄悄在仓库里生成文件。
        risk = RiskEngine(engine_store, RiskEngineConfig(lkg_cache_path=""), kill_switch=kill_switch)
        if load:
            risk.load()
        engine.attach_risk_engine(risk)
    return SimpleNamespace(
        engine=engine, broker=broker, feed=feed, symbol=symbol, config=config,
        strategy=strategy, risk=risk,
    )


def market_orders(broker) -> list:
    """**市场侧**真实收到的订单（撮合器里的那一份，不含被闸门拦下的）。"""
    return [
        (o.submit_time, o.direction.value, o.quantity, o.status.value)
        for o in broker.get_orders(STRATEGY_ID)
    ]


def buys(result) -> list:
    return [o for o in result.orders if o.direction is Direction.BUY]


# ── 1. 默认不接（接线是显式的，不是偷偷默认开的）────────────────────────
def test_gate_off_by_default_keeps_i1_order_flow() -> None:
    """不调 `attach_risk_engine()` ⇒ 一笔也不拦，结果与 I1 完全一致。

    这条是**对照组**：后面几条「被缩量/被拦」的结论，全靠它才说明得了是闸门干的。
    """
    ctx = build()
    result = ctx.engine.run()

    assert ctx.engine.risk_summary() == {
        "attached": False,
        "rule_version": None,
        "run_state": None,
        "checked": 0,
        "passed": 0,
        "reduced": 0,
        "blocked": 0,
        "blocked_orders": [],
    }, "没接风控时统计应当是「未接入」的形状，而不是 zeros 里混一个 None"
    assert len(result.orders) == 3, "I1 的订单流变了，后面的对照全部失效"
    assert all(o.status is OrderStatus.FILLED for o in result.orders)
    assert len(result.trades) == 3


def test_attach_rejects_non_risk_engine() -> None:
    """接错东西要当场报错，不能等到下单时才发现「闸门其实没接上」。"""
    ctx = build()
    with pytest.raises(ConfigValidationError):
        ctx.engine.attach_risk_engine(object())


def test_unloaded_engine_refuses_and_nothing_slips_through() -> None:
    """没 `load()` 过的引擎：**拒绝放行**（D4），且不能放走任何一单。

    这一条防的是「先接上、后加载」的顺序错误：如果 `check()` 在未加载时悄悄放行，
    那风控就不是「没生效」，而是「存在却零约束」—— 那比不接更危险。
    """
    ctx = build(attach=True, load=False)
    with pytest.raises(RiskConfigInvalidError):
        ctx.engine.run()
    assert market_orders(ctx.broker) == [], "拒绝发生在下单路径上，但订单已经漏进市场了"


# ── 2. 缩量与拦截 ─────────────────────────────────────────────────────────
def test_reduce_shrinks_open_order_to_position_cap() -> None:
    """默认阈值下，按 90% 本金建仓的买入单被缩到「单票 ≤ 10% 总资产」；平仓单不被缩。"""
    plain = build()
    plain_result = plain.engine.run()
    plain_buy = buys(plain_result)[0]

    ctx = build(attach=True)
    result = ctx.engine.run()
    summary = ctx.engine.risk_summary()

    first_buy = buys(result)[0]
    # 闸门用的是信号那根 K 线的收盘价（`RiskCheckRequest.price`）；下单价在 t+1 撮合，
    # 所以不能用成交价反推。这里直接把引擎看过的那根 K 线取回来。
    bar = ctx.feed.get_bar(ctx.symbol, first_buy.submit_time)
    assert bar is not None, "取不到信号那根 K 线，下面的上限就算不出来（不许猜一个价）"
    cap = int((MAX_POSITION_PCT * ctx.config.initial_capital) // bar.close)

    assert summary["checked"] == len(result.orders), "统计口径与市场侧订单对不上"
    assert summary["passed"] == summary["checked"], "默认阈值下不该有 REJECT（只有缩量）"
    assert summary["reduced"] >= 1
    assert first_buy.quantity > 0 and first_buy.quantity <= cap
    assert cap - first_buy.quantity < LOT_SIZE, "缩量档位应当就是单票上限（误差不足一手）"
    assert first_buy.quantity < plain_buy.quantity, "没比不接风控时更少 ⇒ 闸门没真的动过数量"

    # 平仓单不受「开仓类」规则牵连：数量原样保留。它后来被**撮合器**以「卖出超过持仓」
    # 拒掉 —— 那是真实后果（风控改了持仓，策略自己的账本还不知道），不是闸门拦的。
    sell = [o for o in result.orders if o.direction is Direction.SELL][0]
    plain_sell = [o for o in plain_result.orders if o.direction is Direction.SELL][0]
    assert sell.quantity == plain_sell.quantity
    assert sell.error_message is not None and "kill_switch" not in sell.error_message
    assert "卖出超过持仓" in sell.error_message, "这条拒单应当来自撮合器的持仓检查，不是风控"


def test_zero_share_reduction_is_a_block_not_a_zero_order() -> None:
    """缩量缩到 0 股 ⇒ 按**拦截**记，不能把 0 股订单交给撮合器。

    交给撮合器的后果不是「无事发生」：它会以「资金不足」之类的理由被拒，于是
    `result.orders` 里出现一条**原因错误**的拒单，事后排查会往完全错误的方向查。

    `action` 记成 `REJECT` 是既定口径（`RiskEngine._append_capped`：`cap <= 0` 时
    「减到 0 等于全拒」），不是 `REDUCE` —— 断言写反了会把口径记错，比不写更糟。

    阈值取 `1e-7` 是量出来的不是拍的：夹具里该标的的至今日均成交额是 9.05e6 量级，
    只有 `阈值 × 日均成交额 < 价格(9.25)` 时 `cap = int(额度 // price)` 才落到 0。
    """
    ctx = build(attach=True, rules=widened_rules(max_order_amount_pct=1e-7))
    result = ctx.engine.run()
    summary = ctx.engine.risk_summary()

    assert summary["checked"] == len(result.orders) == 3, "策略没发出这 3 单 ⇒ 这条在空转"
    assert summary["blocked"] == 3, "阈值小到算不出一手，居然还有单子放行"
    assert all(o.quantity > 0 for o in ctx.broker.get_orders(STRATEGY_ID)), "0 股订单进了撮合器"
    assert all(o.quantity > 0 for o in result.orders), "0 股订单进了结果清单"
    assert all(b["action"] == "REJECT" for b in summary["blocked_orders"]), (
        "缩到 0 股应当仍是拦截（全拒），不能变成一条 0 股的「放行」"
    )
    assert all("max_order_amount_pct" in b["rule_ids"] for b in summary["blocked_orders"])
    assert result.trades == [], "没有任何一单该成交"


def test_snapshot_equity_counts_floating_value_of_long_position() -> None:
    """快照里的 `strategy_equity` 必须含多头浮动市值 —— 漏了它，回撤类规则看到的是假回撤。

    **这是回归守卫，守的是一个真发生过的 bug**：`BacktestEngine._strategy_equity()` 曾经把
    「现金流符号」与「持仓方向符号」共用一个 `sign`，多头持仓在净持仓表里被记成负数 ⇒
    `quantity > 0` 不成立 ⇒ 浮动市值永远加不上来。满仓那一刻权益变成
    `90000 - 89725 - 335.25 = -60.25`，`strategy_drawdown_pct` 于是观测到 `1.0007` 的
    假回撤、把策略单元熔断掉（表现是「第一笔正常建仓之后，第三笔单莫名被拒」）。

    做法：把闸门**实际看到的那份快照**逐次记下来（包一层 `check()`，这正是接线的一部分），
    再用测试里独立写的式子对账 —— 算术在测试里重写一遍，才不会被实现里的同一个符号错误
    一起带走。
    """
    recorded: list = []
    ctx = build(attach=True, rules=widened_rules())
    original_check = ctx.risk.check

    def spy(request):  # noqa: ANN001 - 与 RiskEngine.check 同签名
        snapshot = request.snapshot
        recorded.append(
            (
                float(snapshot.strategy_equity),
                [
                    (
                        trade.direction,
                        int(trade.quantity),
                        float(trade.price),
                        float(trade.commission),
                        float(trade.slippage),
                    )
                    for trade in ctx.broker.get_trades(STRATEGY_ID)
                ],
                {
                    name: (int(position.quantity), float(position.current_price))
                    for name, position in ctx.broker.get_positions().items()
                },
            )
        )
        return original_check(request)

    ctx.risk.check = spy
    ctx.engine.run()

    assert len(recorded) == 3, "闸门没被调用 3 次 ⇒ 下面的对账在空转"
    assert any(positions for _equity, _trades, positions in recorded), (
        "全程没有持仓时浮动市值恒为 0 ⇒ 断言测不出符号写错（用放宽阈值的场景保证真的建了仓）"
    )
    capital = resolve_strategy_capital(cli_args())
    for equity, trades, positions in recorded:
        flow = sum(
            (-1 if direction is Direction.BUY else 1) * price * quantity - (commission + slippage)
            for direction, quantity, price, commission, slippage in trades
        )
        floating = sum(
            current_price * quantity for _name, (quantity, current_price) in positions.items()
        )
        assert equity == pytest.approx(capital + flow + floating, rel=1e-9, abs=1e-6), (
            "快照权益 = 本金 + 已实现现金流 + 多头浮动市值，实测对不上"
        )
        assert equity > 0.0, "多头持仓（本系统不做空）不可能把权益打到负数"


# ── 3. KillSwitch（DoD 逐字：触发后订单数为 0）──────────────────────────
def test_kill_switch_before_run_keeps_every_order_out_of_market() -> None:
    """拉闸之后再跑回测：进市场的订单数为 **0**，且每一单都被拦下且可解释。"""
    switch = KillSwitch()
    switch.activate("测试：跑之前拉闸", "tester")
    ctx = build(attach=True, kill_switch=switch)
    result = ctx.engine.run()
    summary = ctx.engine.risk_summary()

    assert market_orders(ctx.broker) == [], "拉闸后还有订单进了市场"
    assert result.trades == [], "拉闸后还成交了"
    assert ctx.broker.get_account().total_capital == ctx.config.initial_capital, "资金被动了"

    # 空转守卫：如果策略一根信号都没发，上面两条断言在「什么都没发生」时也会通过。
    assert len(result.orders) == 3, "这批单必须真的产生过，否则「0 单进市场」说明不了任何事"
    assert all(o.status is OrderStatus.REJECTED for o in result.orders)
    assert summary["checked"] == 3 == summary["blocked"]
    assert summary["passed"] == 0 and summary["reduced"] == 0
    assert summary["run_state"] == "KILLED"
    for record in summary["blocked_orders"]:
        assert record["run_state"] == "KILLED"
        assert "kill_switch" in record["rule_ids"]
    # 平仓同样被拦（D5：Kill Switch 下清仓的唯一出口是 explicit 的 emergency_flatten）。
    assert {"BUY", "SELL"} <= {record["side"] for record in summary["blocked_orders"]}

    flatten_id = switch.emergency_flatten("测试：显式清仓", "tester")
    assert flatten_id and switch.is_active(), "清仓动作不得顺手把开关关掉（D5 禁止「一键清仓」后门）"


def test_kill_switch_mid_run_blocks_only_subsequent_orders() -> None:
    """跑到中途人工拉闸：**之前**已成交的不动，**之后**的信号一单也进不了市场。"""

    class TripSwitchOnFirstSubmit(MA_Cross_Strategy):
        """第一笔订单真的进了撮合器之后，把 KillSwitch 拉起来。

        挂在 `on_order` 上是因为 `BacktestEngine` 只在**提交成功之后**回调它 ——
        这个时点恰好等于「市场侧刚多了一笔单」，再往后的信号理当全被拦。
        """

        def __init__(self, *args, switch=None, **kwargs):
            self._switch = switch
            self._accepted = 0
            super().__init__(*args, **kwargs)

        def on_order(self, order) -> None:  # noqa: ANN001 - 与基类同签名
            self._accepted += 1
            if self._accepted == 1 and self._switch is not None:
                self._switch.activate("测试：跑到一半人工拉闸", "tester")
            super().on_order(order)

    switch = KillSwitch()

    def factory(symbol, capital, args):
        return TripSwitchOnFirstSubmit(
            args.strategy_id,
            {
                "short_window": int(args.short),
                "long_window": int(args.long),
                "capital": capital,
                "symbol": symbol,
            },
            switch=switch,
        )

    ctx = build(
        attach=True,
        rules=widened_rules(),  # 放宽阈值：拉闸前的成交应当足额，别把缩量混进来
        kill_switch=switch,
        strategy_factory=factory,
    )
    result = ctx.engine.run()
    summary = ctx.engine.risk_summary()

    assert len(market_orders(ctx.broker)) == 1, "拉闸之后还放行了订单"
    assert summary["blocked"] == 2 and summary["checked"] == 3
    assert len(result.trades) == 1 and result.trades[0].direction is Direction.BUY
    assert result.trades[0].quantity == 9700, "放宽阈值后这一单应当足额（证明缩量不是必然的）"

    blocked_at = [datetime.fromisoformat(record["datetime"]) for record in summary["blocked_orders"]]
    last_accepted = max(stamp for stamp, _d, _q, _s in market_orders(ctx.broker))
    assert last_accepted < min(blocked_at), "被拦下的单不该早于已成交的那一单"
    assert all(record["run_state"] == "KILLED" for record in summary["blocked_orders"])
    assert switch.is_active()


# ── 4. 接线本身不该改行为 ─────────────────────────────────────────────────
def test_widened_thresholds_reproduce_the_ungated_run() -> None:
    """阈值放宽到拦不住任何东西时，结果必须与「不接闸门」逐笔相同。

    这是**正向对照**：证明前面那些缩量/拦截来自阈值判定，而不是接线顺手改了订单。
    """
    plain = build()
    plain_result = plain.engine.run()

    ctx = build(attach=True, rules=widened_rules())
    result = ctx.engine.run()
    summary = ctx.engine.risk_summary()

    assert market_orders(ctx.broker) == market_orders(plain.broker)
    assert [(o.submit_time, o.direction, o.quantity, o.status) for o in result.orders] == [
        (o.submit_time, o.direction, o.quantity, o.status) for o in plain_result.orders
    ]
    assert ctx.broker.get_account().total_capital == plain.broker.get_account().total_capital
    assert summary["checked"] == 3 and summary["passed"] == 3
    assert summary["reduced"] == 0 and summary["blocked"] == 0
    assert ctx.engine.risk_summary()["run_state"] == "NORMAL", (
        "阈值全放宽还进了熔断态 —— 回撤类规则看到的权益是错的（见 `_strategy_equity` 的符号口径）"
    )


def test_check_does_no_store_io_during_the_backtest() -> None:
    """契约 D1：`check()` 是零 IO 的 —— 回测热路径不得访问规则存储。"""

    class CountingStore(MemoryRiskRuleStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = {"load_rules": 0, "get_version": 0, "save_change": 0}

        def load_rules(self):
            self.calls["load_rules"] += 1
            return super().load_rules()

        def get_version(self) -> int:
            self.calls["get_version"] += 1
            return super().get_version()

        def save_change(self, change):  # noqa: ANN001 - 与基类同签名
            self.calls["save_change"] += 1
            return super().save_change(change)

    store = CountingStore()
    ctx = build(attach=True, store=store, rules=widened_rules())
    after_load = dict(store.calls)
    # 计数器自身的守卫：`load()` 必须已经真的读过存储，否则下面那条「调用次数没变」
    # 在计数器坏掉（恒 0）时也会通过 —— 那就是拿空转当证据。
    assert after_load == {"load_rules": 1, "get_version": 1, "save_change": 0}, (
        "加载阶段的调用计数不对，本次「热路径零 IO」的结论不成立：%r" % (after_load,)
    )

    ctx.engine.run()

    assert store.calls == after_load, "回测热路径访问了规则存储（违反 D1，且引入了不可复现的 IO）"


def test_report_payload_carries_no_risk_fields() -> None:
    """风控统计**刻意不进回测报告**：`deterministic` 段是 `.rounds/i1/` 逐段比对的证据。

    断言用的是 `tools/verify_backtest_reproducibility.py` 的 **`R4` 键集本身**
    （`BLOCKED_IN_PAYLOAD`）—— 手写一份「不许出现的词」清单，会因为真实字段名不同而空转；
    共用键集则「接线把某个风控字段塞进 `deterministic`」与「`R4` 判红」是同一件事。
    """
    ctx = build(attach=True, rules=widened_rules())
    payload = report_payload(ctx.engine.run(), 7)

    # 与 `R4` 同源：键集只能比它更严，不能更松。
    assert payload["schema"] == GATE_SCHEMA, "报告 schema 变了，`.rounds/i1/` 的证据随之失效"
    assert set(payload) == {"schema", "inputs", "deterministic", "runtime"}, (
        "报告的段结构变了 —— `deterministic` 段的比对基准（`.rounds/i1/`）会随之失效"
    )
    assert set(payload["inputs"]) == GATE_INPUTS_KEYS
    assert set(payload["runtime"]) == GATE_RUNTIME_KEYS
    assert set(payload["deterministic"]) == GATE_DETERMINISTIC_KEYS, (
        "deterministic 段的键集与可复现性门禁（R4）不一致：多出的键 %s / 缺的键 %s —— "
        "多键会让同种子两次跑不再逐字节相同，缺键会让门禁自己也判红"
        % (
            sorted(set(payload["deterministic"]) - GATE_DETERMINISTIC_KEYS),
            sorted(GATE_DETERMINISTIC_KEYS - set(payload["deterministic"])),
        )
    )
    assert not RISK_KEY_HINTS & set(payload["deterministic"]), (
        "风控字段进了 deterministic 段 —— 判据段里不许有观测字段（见 `开工前缺口清单.md` §七之二）"
    )
    assert ctx.engine.risk_summary()["checked"] == 3, "风控确实跑过（否则这条是空断言）"


def test_rerun_resets_counters_but_keeps_rule_version() -> None:
    """同一个引擎跑两次：统计**必须按本次重算**（归零在 `_prepare()` 里），规则版本跨次保留。

    这里**不**断言两次的订单流相同：策略对象带着自己的滚动窗口状态跨 run，第二次跑出来
    的信号本来就不同（实测 `2/2, 2/6, 3/8` vs `1/26, 2/6, 3/1, 3/20`）。「同一实例两次
    `run()` 结果相同」不是引擎的承诺，写进断言就是把不存在的东西当契约。
    能断言的是**计数口径**：`checked` 等于**本次**的订单数（不是两次累加）。
    """
    ctx = build(attach=True, rules=widened_rules())
    first = ctx.engine.run()
    first_summary = ctx.engine.risk_summary()

    second = ctx.engine.run()
    second_summary = ctx.engine.risk_summary()

    assert len(first.orders) > 0 and len(second.orders) > 0, "有人一笔没发 ⇒ 计数断言在空转"
    assert first_summary["checked"] == len(first.orders), "第一次的统计与它自己的订单数对不上"
    assert second_summary["checked"] == len(second.orders), (
        "计数器没在 `_prepare()` 里归零：报的 %d 是两次回测累加出来的，本次只有 %d 单"
        % (second_summary["checked"], len(second.orders))
    )
    assert second_summary["rule_version"] == first_summary["rule_version"] == 1
    assert first_summary["checked"] == first_summary["passed"] + first_summary["blocked"]
    assert second_summary["checked"] == second_summary["passed"] + second_summary["blocked"]
    assert market_orders(ctx.broker) == [
        (o.submit_time, o.direction.value, o.quantity, o.status.value) for o in second.orders
    ]
