"""风控层行为测试 —— 逐条对应《风控层接口契约文档》§3.8「测试要求」那 13 行。

**这是「红步」**（2026-09-24）：`quanauto/risk.py` 此刻只有类型面，行为方法是最小非实施桩。
按《开发工作流规范》§2.2，红的来源必须是**断言失败**，不能是 `ImportError` / `AttributeError`
—— 所以文件先把契约的类型面落全再写本文件，这里每一条失败都是「实现还没达到契约要求」。

读法建议：每条测试名即那条契约要求；`docstring` 里写了它为什么存在（不写的话，下一个人
会以为这些断言是随手加的，然后顺手把它删掉）。
"""

from __future__ import annotations

import os

import pytest

from quanauto.enums import Direction
from quanauto.errors import (
    IllegalStateError,
    RiskConfigInvalidError,
    RiskConfigLoadError,
    RiskRuleNotFoundError,
)
from quanauto.risk import (
    BreakerStateEnum,
    GLOBAL_SCOPE_KEY,
    KillSwitch,
    MemoryRiskRuleStore,
    RESUME_CONFIRMATION,
    RISK_PEAK_MISSING,
    RiskActionEnum,
    RiskCheckRequest,
    RiskEngine,
    RiskEngineConfig,
    RiskRule,
    RiskRuleStore,
    RiskRunStateEnum,
    RiskSnapshot,
    RULE_SPECS,
    RuleChangeRequest,
    RuleScopeEnum,
    RuleTypeEnum,
    SeverityEnum,
    default_global_rules,
    validate_threshold,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = RiskEngineConfig(lkg_cache_path="")
"""测试一律关掉 LKG 缓存落盘 —— 否则每个用例都会在仓库里写 `data/risk_lkg.json`。"""


# ── 测试替身 ──────────────────────────────────────────────────────────────
class CountingStore(MemoryRiskRuleStore):
    """内存存储 + 调用计数：用来断言 `check()` 的零 IO（D1）。

    为什么必须是**计数**而不是"看代码里没写 db"：`check()` 里多一句
    `self._store.get_version()` 是极容易被重构进去的，而它会让每笔委托都查一次库。
    """

    def __init__(self, rules=None, version: int = 1) -> None:
        super().__init__(rules)
        self._version = version
        self.calls = 0

    def load_rules(self):
        self.calls += 1
        return super().load_rules()

    def get_version(self) -> int:
        self.calls += 1
        return super().get_version()

    def save_change(self, change) -> int:
        self.calls += 1
        return super().save_change(change)

    def publish(self, rules, version: int) -> None:
        """把存储里的内容换成另一套（模拟"运维改了库"）。"""
        self._rules = list(rules)
        self._version = version


class PersistentStore(CountingStore):
    """带峰值/熔断落盘的存储 —— 对应 `DbRiskRuleStore` 的四个额外方法（D7/D8）。"""

    def __init__(self, rules=None, version: int = 1) -> None:
        super().__init__(rules, version)
        self.peaks = {}
        self.breakers = {}

    def save_equity_peak(self, peak_key: str, peak_value: float) -> None:
        self.peaks[peak_key] = peak_value

    def load_equity_peaks(self):
        return dict(self.peaks)

    def save_breaker_state(self, state) -> None:
        self.breakers[state.breaker_key] = state

    def load_breaker_states(self):
        return dict(self.breakers)


class BrokenStore(RiskRuleStore):
    """配置源完全不可达（D4/D5）。"""

    def load_rules(self):
        raise RiskConfigLoadError("配置源不可达")

    def get_version(self) -> int:
        raise RiskConfigLoadError("配置源不可达")

    def save_change(self, change) -> int:
        raise RiskConfigLoadError("配置源不可达")


def make_engine(rules=None, version: int = 1, store=None, config=CFG, **kwargs):
    store = store if store is not None else CountingStore(rules, version)
    engine = RiskEngine(store, config, **kwargs)
    engine.load()
    return engine, store


def snap(**overrides) -> RiskSnapshot:
    kwargs = dict(
        total_asset=1_000_000.0,
        available_capital=1_000_000.0,
        strategy_equity=1_000_000.0,
        symbol_avg_daily_amount=10_000_000.0,
        trading_date="2026-09-24",
        position_value_by_symbol={},
        position_value_by_sector={},
        daily_trade_count_by_symbol={},
    )
    kwargs.update(overrides)
    return RiskSnapshot(**kwargs)


def request(snapshot=None, **overrides) -> RiskCheckRequest:
    kwargs = dict(
        account_id="ACC1",
        strategy_id="S1",
        symbol="600000.SH",
        side=Direction.BUY,
        is_open=True,
        quantity=100,
        price=10.0,
        snapshot=snapshot if snapshot is not None else snap(),
    )
    kwargs.update(overrides)
    return RiskCheckRequest(**kwargs)


def rules_with(**thresholds):
    """在 §3.1.1 的 GLOBAL 默认值上打补丁，生成一套 GLOBAL 规则。"""
    rules = default_global_rules()
    for rule_id, value in thresholds.items():
        for rule in rules:
            if rule.rule_id == rule_id:
                rule.threshold = value
    return rules


def layer(rule_id: str, scope: RuleScopeEnum, scope_key: str, threshold: float, enabled: bool = True):
    spec = {item.rule_id: item for item in RULE_SPECS}[rule_id]
    return RiskRule(
        rule_id=rule_id,
        rule_type=spec.rule_type,
        scope=scope,
        scope_key=scope_key,
        threshold=threshold,
        unit=spec.unit,
        enabled=enabled,
        version=1,
    )


# ── §3.8 第 1 行：规则加载 ────────────────────────────────────────────────
def test_load_returns_effective_version():
    """`load()` 返回本次生效版本号；内存存储的默认版本与 DDL 种子同为 1。"""
    engine, store = make_engine()
    assert engine.load() == 1
    assert engine.get_rule_version() == 1
    assert store.get_version() == 1


def test_load_rejects_ratio_written_as_percent():
    """RATIO 一律 0~1 小数：把 0.10 写成 10 必须当场拒（D3，差 100 倍那类静默算错）。"""
    engine = RiskEngine(CountingStore(rules_with(max_position_pct=10)), CFG)
    with pytest.raises(RiskConfigInvalidError) as excinfo:
        engine.load()
    assert "RATIO" in str(excinfo.value)


def test_load_rejects_global_missing_rule():
    """GLOBAL 层缺规则 = 配置非法（D4）：没有兜底值的规则不是"不生效"，是"算不出来"。"""
    rules = [r for r in default_global_rules() if r.rule_id != "max_sector_pct"]
    engine = RiskEngine(CountingStore(rules), CFG)
    with pytest.raises(RiskConfigInvalidError) as excinfo:
        engine.load()
    assert "max_sector_pct" in str(excinfo.value)


def test_load_failure_keeps_lkg_untouched():
    """整批校验失败 ⇒ 整批拒绝、旧值继续生效（"宁可旧值，不可无值，更不可半新半旧"）。"""
    engine, store = make_engine()
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10
    store.publish(rules_with(max_position_pct=10), 2)
    with pytest.raises(RiskConfigInvalidError):
        engine.load()
    assert engine.get_rule_version() == 1
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10


def test_load_without_lkg_raises_when_store_unreachable():
    """存储不可达且**没有**任何 LKG ⇒ 抛 `RiskConfigLoadError`，绝不"空着放行"。"""
    engine = RiskEngine(BrokenStore(), CFG)
    with pytest.raises(RiskConfigLoadError):
        engine.load()


def test_load_falls_back_to_lkg_cache_file(tmp_path):
    """有 LKG 缓存 ⇒ 从缓存恢复并切 DEGRADED（只减仓），而不是拒绝启动。"""
    cache = str(tmp_path / "lkg.json")
    good, _ = make_engine()
    good.write_lkg_cache(cache)

    engine = RiskEngine(BrokenStore(), RiskEngineConfig(lkg_cache_path=cache))
    assert engine.load() == 1
    assert engine.get_run_state() is RiskRunStateEnum.DEGRADED
    assert engine.health().store_reachable is False
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10


# ── §3.8 第 2 行：分层覆盖 ────────────────────────────────────────────────
def test_symbol_layer_wins_over_all_others():
    rules = rules_with(max_position_pct=0.10)
    rules.append(layer("max_position_pct", RuleScopeEnum.ACCOUNT, "ACC1", 0.30))
    rules.append(layer("max_position_pct", RuleScopeEnum.STRATEGY, "S1", 0.20))
    rules.append(layer("max_position_pct", RuleScopeEnum.SYMBOL, "600000.SH", 0.05))
    engine, _ = make_engine(rules)
    resolved = engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH")
    assert resolved.threshold == 0.05
    assert resolved.scope is RuleScopeEnum.SYMBOL
    assert resolved.scope_key == "600000.SH"


def test_strategy_layer_wins_over_account_and_global():
    rules = rules_with(max_position_pct=0.10)
    rules.append(layer("max_position_pct", RuleScopeEnum.ACCOUNT, "ACC1", 0.30))
    rules.append(layer("max_position_pct", RuleScopeEnum.STRATEGY, "S1", 0.20))
    engine, _ = make_engine(rules)
    resolved = engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH")
    assert resolved.threshold == 0.20
    assert resolved.scope is RuleScopeEnum.STRATEGY


def test_account_layer_wins_over_global():
    rules = rules_with(max_position_pct=0.10)
    rules.append(layer("max_position_pct", RuleScopeEnum.ACCOUNT, "ACC1", 0.30))
    engine, _ = make_engine(rules)
    resolved = engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH")
    assert resolved.threshold == 0.30
    assert resolved.scope is RuleScopeEnum.ACCOUNT


def test_global_is_the_last_resort_and_effective_scope_is_backfilled():
    """命中 GLOBAL 时必须回填生效作用域 —— 事后归因要能说出"这条来自全局兜底"。"""
    engine, _ = make_engine()
    resolved = engine.get_effective_rule("max_position_pct", "ACC1", "S1", "000001.SZ")
    assert resolved.threshold == 0.10
    assert resolved.scope is RuleScopeEnum.GLOBAL
    assert resolved.scope_key == GLOBAL_SCOPE_KEY


def test_disabled_layer_is_skipped():
    """`enabled=0` 的层视为**不存在**，继续往下一层找（D2）。"""
    rules = rules_with(max_position_pct=0.10)
    rules.append(layer("max_position_pct", RuleScopeEnum.SYMBOL, "600000.SH", 0.05, enabled=False))
    engine, _ = make_engine(rules)
    resolved = engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH")
    assert resolved.threshold == 0.10
    assert resolved.scope is RuleScopeEnum.GLOBAL


def test_disabled_global_rule_is_unresolvable():
    """GLOBAL 只要求"存在"；存在但 `enabled=0` ⇒ 所有层都不存在 ⇒ 解析不到（D4）。"""
    rules = rules_with()
    for rule in rules:
        if rule.rule_id == "max_position_pct":
            rule.enabled = False
    engine, _ = make_engine(rules)
    with pytest.raises(RiskRuleNotFoundError):
        engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH")


def test_unknown_rule_id_is_rejected():
    """`rule_id` 是固定标识，不可自定义（§3.1.1）。"""
    engine, _ = make_engine()
    with pytest.raises(RiskConfigInvalidError):
        engine.get_effective_rule("my_own_rule", "ACC1", "S1", "600000.SH")


# ── §3.8 第 3 行：校验路径 ────────────────────────────────────────────────
def test_check_does_zero_io():
    """`check()` 内**禁止**访问存储（D1）：打桩 store 的调用次数必须恰好 0。"""
    engine, store = make_engine()
    before = store.calls
    engine.check(request())
    assert store.calls == before


def test_check_is_pure_and_reentrant():
    """`check()` 不改 request、不依赖上次调用的残留状态：同一请求两次结果一致。"""
    engine, _ = make_engine()
    req = request()
    first = engine.check(req)
    second = engine.check(req)
    assert req.quantity == 100
    assert (first.passed, first.action, first.adjusted_quantity) == (
        second.passed,
        second.action,
        second.adjusted_quantity,
    )


def test_single_violation_is_reported_with_effective_scope():
    """违规明细要带回**实际生效**的作用域（D2）：否则事后不知道按哪一层拦的。"""
    engine, _ = make_engine()
    snapshot = snap(position_value_by_symbol={"600000.SH": 120_000.0})
    response = engine.check(request(snapshot, quantity=1, price=10.0))
    assert response.passed is False
    fired = [v.rule_id for v in response.violations]
    # `RULE_ORDER` 固定了判定顺序，仓位类规则排第一：明细的**顺序**本身也是可复现性的一部分
    assert fired[0] == "max_position_pct"
    violation = response.violations[0]
    assert violation.scope is RuleScopeEnum.GLOBAL
    assert violation.scope_key == GLOBAL_SCOPE_KEY
    assert violation.rule_type is RuleTypeEnum.MAX_POSITION_PCT
    assert violation.severity == SeverityEnum.ERROR.value
    assert violation.action is RiskActionEnum.REJECT


def test_highest_severity_wins_when_multiple_rules_fire():
    """多规则并发触发取最高严重度：熔断（HALT/CRITICAL）要盖过流动性（REDUCE/WARNING）。"""
    engine, _ = make_engine(rules_with(max_order_amount_pct=0.00001))
    engine.check(request(snap(strategy_equity=1_000_000.0)))  # 先立峰值，再跌
    response = engine.check(request(snap(strategy_equity=500_000.0)))
    assert response.action is RiskActionEnum.HALT
    assert response.passed is False
    fired = [v.rule_id for v in response.violations]
    assert "strategy_drawdown_pct" in fired
    assert "max_order_amount_pct" in fired
    severities = {v.severity for v in response.violations}
    assert severities == {SeverityEnum.CRITICAL.value, SeverityEnum.WARNING.value}


def test_reduce_shrinks_quantity_to_the_allowed_room():
    """REDUCE 的数量计算：额度 = 阈值 × 总资产 − 已持仓市值，再按价格折算股数。"""
    engine, _ = make_engine()
    # 总资产 100 万、阈值 10% ⇒ 上限 10 万；已持 9.5 万 ⇒ 只剩 5000 元 ⇒ 500 股 @10 元
    snapshot = snap(position_value_by_symbol={"600000.SH": 95_000.0})
    response = engine.check(request(snapshot, quantity=2000, price=10.0))
    assert response.action is RiskActionEnum.REDUCE
    assert response.passed is False
    assert response.adjusted_quantity == 500


def test_reduce_with_no_room_becomes_reject():
    """额度减到 0 股 ⇒ 不是"减到 0"而是**拒绝**：放行 0 股的订单没有意义。"""
    engine, _ = make_engine()
    snapshot = snap(position_value_by_symbol={"600000.SH": 100_000.0})
    response = engine.check(request(snapshot, quantity=2000, price=10.0))
    assert response.action is RiskActionEnum.REJECT
    assert response.violations[0].rule_id == "max_position_pct"


def test_non_reduce_actions_echo_request_quantity():
    """契约 §3.2.5：`adjusted_quantity` 仅在 REDUCE 时是"允许的最大数量"，其余等于请求数量。"""
    engine, _ = make_engine()
    passed = engine.check(request())
    assert passed.adjusted_quantity == 100
    rejected = engine.check(request(snap(daily_trade_count_by_symbol={"600000.SH": 3})))
    assert rejected.action is RiskActionEnum.REJECT
    assert rejected.adjusted_quantity == 100


def test_daily_trade_cap_blocks_open_but_never_blocks_close():
    """频率上限是「买+卖合计」，但它拦平仓等于把仓位锁死 —— 只拦开仓。"""
    engine, _ = make_engine()
    snapshot = snap(daily_trade_count_by_symbol={"600000.SH": 3})
    assert engine.check(request(snapshot, is_open=True, side=Direction.BUY)).passed is False
    assert engine.check(request(snapshot, is_open=False, side=Direction.SELL)).passed is True


def test_liquidity_rule_applies_to_close_as_well():
    """流动性（`max_order_amount_pct`）与方向无关：减仓同样有冲击成本。"""
    engine, _ = make_engine()
    snapshot = snap(symbol_avg_daily_amount=10_000.0, available_capital=1_000_000.0)
    response = engine.check(request(snapshot, is_open=False, side=Direction.SELL, quantity=2000, price=10.0))
    assert response.action is RiskActionEnum.REDUCE
    assert response.adjusted_quantity == 100


def test_sector_rule_without_sector_data_warns_instead_of_blocking():
    """行业分类来源仍是空白（契约 §四已认的缺口）⇒ 记 WARNING 露面，**不许**静默放过。

    这条测的不是"实现对了"，而是"缺口被看见了"：没有行业数据时既不能假装合规
    （静默放行会让集中度上限形同虚设），也不能拿未知当违规去误拦。
    """
    engine, _ = make_engine()
    snapshot = snap(position_value_by_symbol={"600000.SH": 5_000.0}, position_value_by_sector={})
    response = engine.check(request(snapshot, quantity=1, price=10.0))
    sector = [v for v in response.violations if v.rule_id == "max_sector_pct"]
    assert len(sector) == 1
    assert sector[0].severity == SeverityEnum.WARNING.value
    assert sector[0].action is RiskActionEnum.PASS
    assert response.passed is True


def test_uncomputable_price_is_refused_not_passed():
    """无法判定一律拒绝（§2.3）：价格 <= 0 时算不出金额，静默放行是最坏结果。"""
    engine, _ = make_engine()
    with pytest.raises(RiskConfigInvalidError):
        engine.check(request(snap(), quantity=100, price=0.0))


# ── §3.8 第 4 行：单位口径 ────────────────────────────────────────────────
def test_ratio_threshold_must_be_decimal_not_percent():
    validate_threshold("max_position_pct", 0.1)
    with pytest.raises(RiskConfigInvalidError):
        validate_threshold("max_position_pct", 10)


def test_count_threshold_must_be_a_non_negative_integer():
    validate_threshold("max_daily_trades", 3)
    with pytest.raises(RiskConfigInvalidError):
        validate_threshold("max_daily_trades", 2.5)


def test_threshold_of_unknown_rule_is_rejected():
    with pytest.raises(RiskConfigInvalidError):
        validate_threshold("invented_rule", 0.5)


# ── §3.8 第 5 行：放行矩阵（4 状态 × 开/平仓 = 8 组合）───────────────────
def _matrix_engine(state: str):
    if state == "KILLED":
        switch = KillSwitch()
        switch.activate("演练", "ops")
        engine, _ = make_engine(kill_switch=switch)
        return engine
    if state == "BREAKER_TRIPPED":
        engine, _ = make_engine()
        engine.trip_breaker("ACCOUNT:ACC1", "手工演练")
        return engine
    if state == "DEGRADED":
        engine, store = make_engine()
        store.publish(rules_with(max_position_pct=10), 2)
        assert engine.reload().run_state is RiskRunStateEnum.DEGRADED
        return engine
    engine, _ = make_engine()
    return engine


@pytest.mark.parametrize(
    "state,is_open,expected",
    [
        ("NORMAL", True, True),
        ("NORMAL", False, True),
        ("DEGRADED", True, False),
        ("DEGRADED", False, True),
        ("BREAKER_TRIPPED", True, False),
        ("BREAKER_TRIPPED", False, True),
        ("KILLED", True, False),
        ("KILLED", False, False),
    ],
)
def test_release_matrix_has_all_eight_combinations(state, is_open, expected):
    """D5 放行矩阵：**8 个组合全部**要有断言，缺一个就等于少了一条口径。

    为什么平仓要单独测：契约的矩阵只在"开仓"那半有拦截力，平仓是全套放行 ——
    写实现时最容易"顺手把状态门也套到平仓上"，那样任何异常状态下都出不了货。
    """
    engine = _matrix_engine(state)
    response = engine.check(request(is_open=is_open))
    assert response.passed is expected
    assert response.run_state.value == state


def test_kill_switch_close_goes_through_emergency_channel_only():
    """KILLED 下平仓也走不了 `check()`：唯一出口是 `emergency_flatten` 显式通道（D5）。"""
    switch = KillSwitch()
    switch.activate("灾难演练", "ops")
    engine, _ = make_engine(kill_switch=switch)
    response = engine.check(request(is_open=False))
    assert response.passed is False
    assert "RISK_007" in response.message
    assert "emergency_flatten" in response.message


def test_degraded_close_can_be_switched_off():
    """`degraded_allow_close=False` 是运维自选的更严口径，配置项必须真的接上（否则是死配置）。"""
    config = RiskEngineConfig(lkg_cache_path="", degraded_allow_close=False)
    engine, store = make_engine(config=config)
    store.publish(rules_with(max_position_pct=10), 2)
    assert engine.reload().run_state is RiskRunStateEnum.DEGRADED
    assert engine.check(request(is_open=False)).passed is False


# ── §3.8 第 6 行：变更生效（D6）──────────────────────────────────────────
def test_tightening_applies_immediately():
    engine, _ = make_engine()
    result = engine.apply_change(
        RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.05, "ops", "收紧")
    )
    assert result.applied is True
    assert result.pending is False
    assert result.old_threshold == 0.10
    assert result.new_threshold == 0.05
    assert result.rule_version == engine.get_rule_version() == 2
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.05


def test_relax_with_confirmation_applies_immediately():
    engine, _ = make_engine(rules_with(max_position_pct=0.05))
    result = engine.apply_change(
        RuleChangeRequest(
            "max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.10, "ops", "放宽", confirm_relax=True
        )
    )
    assert result.applied is True
    assert result.pending is False
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10


def test_unconfirmed_relax_becomes_pending():
    """放宽未确认 ⇒ 落 PENDING、**旧值继续生效**（D6 的非对称：收紧急、放宽缓）。"""
    engine, _ = make_engine(rules_with(max_position_pct=0.05))
    result = engine.apply_change(
        RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.10, "ops", "放宽")
    )
    assert result.applied is False
    assert result.pending is True
    assert result.effective_at is not None
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.05


def test_pending_change_takes_effect_on_the_next_trading_day():
    engine, _ = make_engine(
        rules_with(max_position_pct=0.05), next_trading_day=lambda day: "2026-09-25"
    )
    engine.apply_change(
        RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.10, "ops", "放宽")
    )
    assert engine.apply_pending("2026-09-24") == 0
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.05
    assert engine.apply_pending("2026-09-25") == 1
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10


def test_invalid_change_is_rejected_and_old_value_stays():
    """阈值非法 ⇒ 抛 RISK_004 且**旧值继续生效**（不许"改一半"）。"""
    engine, _ = make_engine()
    with pytest.raises(RiskConfigInvalidError):
        engine.apply_change(
            RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 10, "ops", "越界")
        )
    assert engine.get_rule_version() == 1
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.10


def test_change_requires_a_reason():
    """`reason` 必填（写审计）—— 没有原因的门槛变更事后无法归因。"""
    engine, _ = make_engine()
    with pytest.raises(RiskConfigInvalidError):
        engine.apply_change(
            RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.05, "ops", "")
        )


# ── §3.8 第 7 行：熔断（D7）──────────────────────────────────────────────
def test_drawdown_trips_breaker_and_halts_open():
    engine, _ = make_engine()
    engine.check(request(snap(strategy_equity=1_000_000.0)))
    response = engine.check(request(snap(strategy_equity=800_000.0)))
    assert response.action is RiskActionEnum.HALT
    assert response.passed is False
    assert engine.get_breaker_state("STRATEGY:S1").state is BreakerStateEnum.TRIPPED
    violation = [v for v in response.violations if v.rule_id == "strategy_drawdown_pct"][0]
    assert violation.observed == pytest.approx(0.2)
    assert violation.threshold == 0.15


def test_breaker_keeps_blocking_open_after_trip():
    engine, _ = make_engine()
    engine.check(request(snap(strategy_equity=1_000_000.0)))
    engine.check(request(snap(strategy_equity=800_000.0)))
    blocked = engine.check(request(snap(strategy_equity=800_000.0)))
    assert blocked.action is RiskActionEnum.REJECT
    assert blocked.run_state is RiskRunStateEnum.BREAKER_TRIPPED


def test_breaker_survives_restart():
    """熔断必须跨重启保留（D7）：重启能绕过熔断的熔断，等于没有熔断。"""
    store = PersistentStore()
    first, _ = make_engine(store=store)
    first.check(request(snap(strategy_equity=1_000_000.0)))
    first.check(request(snap(strategy_equity=800_000.0)))
    assert first.get_breaker_state("STRATEGY:S1").state is BreakerStateEnum.TRIPPED

    second = RiskEngine(store, CFG)
    second.load()
    assert second.get_breaker_state("STRATEGY:S1").state is BreakerStateEnum.TRIPPED
    assert second.check(request(snap(strategy_equity=800_000.0))).passed is False


def test_relaxing_threshold_does_not_auto_resume_breaker():
    """D7：熔断绝不因阈值放宽而隐式恢复 —— 放宽只是"以后还犯就不拦了"，不是"这次不算"。"""
    engine, _ = make_engine()
    engine.check(request(snap(strategy_equity=1_000_000.0)))
    engine.check(request(snap(strategy_equity=800_000.0)))
    engine.apply_change(
        RuleChangeRequest(
            "strategy_drawdown_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.50, "ops", "放宽", confirm_relax=True
        )
    )
    assert engine.get_breaker_state("STRATEGY:S1").state is BreakerStateEnum.TRIPPED
    assert engine.check(request(snap(strategy_equity=800_000.0))).passed is False


def test_resume_requires_a_tripped_breaker():
    engine, _ = make_engine()
    with pytest.raises(IllegalStateError):
        engine.resume_breaker("STRATEGY:S1", "ops", "无中生有")


def test_resume_clears_breaker_and_audits_operator():
    engine, _ = make_engine(
        rules_with(strategy_drawdown_pct=0.50),  # 放到很宽，恢复后不该再因回撤被拦
    )
    engine.check(request(snap(strategy_equity=1_000_000.0)))
    engine.check(request(snap(strategy_equity=800_000.0)))
    engine.trip_breaker("STRATEGY:S1", "演练")
    state = engine.resume_breaker("STRATEGY:S1", "ops", "已诊断")
    assert state.state is BreakerStateEnum.NORMAL
    assert state.resumed_by == "ops"
    assert state.resumed_at is not None
    assert engine.check(request(snap(strategy_equity=800_000.0))).passed is True


# ── §3.8 第 8 行：峰值持久化（D8）────────────────────────────────────────
def test_equity_peak_survives_restart():
    """峰值不持久化 ⇒ 重启后回撤从 0 起算 ⇒ 回撤熔断静默失效（D8 的原话）。"""
    store = PersistentStore()
    first, _ = make_engine(store=store)
    first.check(request(snap(strategy_equity=1_000_000.0)))
    first.persist_state(force=True)
    assert store.peaks["STRATEGY:S1"] == 1_000_000.0

    second = RiskEngine(store, CFG)
    second.load()
    response = second.check(request(snap(strategy_equity=800_000.0)))
    assert response.action is RiskActionEnum.HALT


def test_missing_equity_peak_reports_risk_011():
    """峰值没恢复时要**露面**：打 RISK_011 告警，而不是安静地当"回撤 0%"。

    用带落盘能力的空存储模拟重启后 `risk_equity_peak` 表为空 —— 这是 D8 真正担心的
    场景：引擎能算出回撤，但算出来的回撤基数是错的（拿今天当历史最高）。
    """
    engine, _ = make_engine(store=PersistentStore())
    response = engine.check(request(snap(total_asset=1_000_000.0)))
    codes = [v.rule_id for v in response.violations]
    assert RISK_PEAK_MISSING in codes
    assert response.passed is True  # 告警不是拦截：峰值缺失只影响回撤类规则的灵敏度
    again = engine.check(request(snap(total_asset=1_000_000.0)))
    assert RISK_PEAK_MISSING not in [v.rule_id for v in again.violations]


def test_restored_peak_does_not_warn():
    """反向对照：峰值真从存储恢复回来了，就**不许**再打 RISK_011（否则告警变噪音）。"""
    store = PersistentStore()
    store.peaks["ACCOUNT:ACC1"] = 1_000_000.0
    store.peaks["STRATEGY:S1"] = 1_000_000.0
    engine, _ = make_engine(store=store)
    response = engine.check(request(snap(total_asset=1_000_000.0)))
    assert RISK_PEAK_MISSING not in [v.rule_id for v in response.violations]


# ── §3.8 第 9 行：Kill Switch ────────────────────────────────────────────
def test_kill_switch_activate_is_idempotent():
    switch = KillSwitch()
    first = switch.activate("灾难演练", "ops")
    second = switch.activate("灾难演练", "ops")
    assert switch.is_active() is True
    assert first.activated_at == second.activated_at


def test_kill_switch_deactivate_requires_the_exact_confirmation_string():
    switch = KillSwitch()
    switch.activate("灾难演练", "ops")
    with pytest.raises(IllegalStateError):
        switch.deactivate("ops", "误触防护", "CONFIRM")
    assert switch.is_active() is True
    state = switch.deactivate("ops", "已排查", RESUME_CONFIRMATION)
    assert state.active is False
    assert switch.is_active() is False


def test_kill_switch_activates_without_any_working_storage():
    """存储完全不可达也必须能激活（契约 §3.4.1 的原话）—— 否则"救命按钮"会因配置源挂掉而失灵。"""
    switch = KillSwitch(store=BrokenStore())
    switch.activate("配置源不可达时的人工熔断", "ops")
    assert switch.is_active() is True


def test_kill_switch_survives_restart(tmp_path):
    path = str(tmp_path / "kill_switch.json")
    first = KillSwitch(state_path=path)
    first.activate("灾难演练", "ops")
    second = KillSwitch(state_path=path)
    assert second.is_active() is True
    assert second.get_state().reason == "灾难演练"


def test_kill_switch_has_no_one_click_liquidation():
    """`emergency_flatten` 是**独立通道**：它不许顺手改变 Kill Switch 状态（否则是"一键清仓"）。"""
    switch = KillSwitch()
    task_id = switch.emergency_flatten("极端行情", "ops")
    assert isinstance(task_id, str) and task_id
    assert switch.is_active() is False
    assert switch.get_state().activated_at is None


# ── §3.8 第 10 行：版本留痕（D9）─────────────────────────────────────────
def test_every_decision_carries_the_rule_version():
    """每一个裁决（含放行）都要带上版本号 —— 回测用当前阈值跑历史，只有版本号能事后解释。"""
    engine, _ = make_engine()
    passed = engine.check(request())
    assert passed.rule_version == 1
    engine.apply_change(
        RuleChangeRequest("max_position_pct", RuleScopeEnum.GLOBAL, GLOBAL_SCOPE_KEY, 0.05, "ops", "收紧")
    )
    after = engine.check(request())
    assert after.rule_version == 2
    assert after.decision_id != passed.decision_id


def test_reload_reports_version_and_state_without_raising():
    """`reload()` 失败**不抛异常**（D4）：它是轮询路径，抛异常会把轮询线程打死。"""
    engine, store = make_engine()
    store.publish(rules_with(max_position_pct=10), 2)
    result = engine.reload()
    assert result.reloaded is False
    assert result.rule_version == 1
    assert result.attempted_version == 2
    assert result.run_state is RiskRunStateEnum.DEGRADED
    assert result.error


def test_reload_rejects_version_rollback():
    """版本回退（RISK_010）：运维回滚要显式操作，不能让轮询悄悄把配置退回去。"""
    engine, store = make_engine()
    store.publish(rules_with(max_position_pct=0.05), 0)
    result = engine.reload()
    assert result.reloaded is False
    assert "RISK_010" in result.error
    assert engine.get_rule_version() == 1


def test_reload_picks_up_a_new_valid_version():
    engine, store = make_engine()
    store.publish(rules_with(max_position_pct=0.05), 2)
    result = engine.reload()
    assert result.reloaded is True
    assert result.rule_version == 2
    assert engine.get_effective_rule("max_position_pct", "ACC1", "S1", "600000.SH").threshold == 0.05


# ── §3.8 第 11 行：接口边界（D10）────────────────────────────────────────
def test_threshold_write_api_exists_only_in_the_risk_module():
    """D10 静态断言：策略 SDK 与 Agent 沙箱里**不得**存在阈值写入方法。

    守的是"某个策略顺手把自己的仓位上限调大"这类事故 —— 它不是逻辑 bug，写多少测试也测不出来。
    """
    package = os.path.join(ROOT, "quanauto")
    offenders = []
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py") or name == "risk.py":
            continue
        with open(os.path.join(package, name), "r", encoding="utf-8") as handle:
            source = handle.read()
        if "apply_change" in source or "RuleChangeRequest" in source or "save_change" in source:
            offenders.append(name)
    assert offenders == []


def test_strategy_sdk_does_not_import_the_risk_module():
    with open(os.path.join(ROOT, "quanauto", "strategies.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "from .risk import" not in source
    assert "import risk" not in source


# ── §3.8 第 12 行：名单型规则（黑名单，§3.6.3）───────────────────────────
def test_blacklist_blocks_open_only():
    """黑名单是名单型规则（不存 `risk_rule`）：它必须允许平仓，否则被禁标的会砸在手里。"""
    engine, _ = make_engine()
    engine.set_blacklist(["600519.SH"])
    blocked = engine.check(request(symbol="600519.SH", is_open=True))
    assert blocked.passed is False
    assert blocked.violations[0].severity == SeverityEnum.CRITICAL.value
    assert engine.check(request(symbol="600519.SH", is_open=False, side=Direction.SELL)).passed is True
    assert engine.check(request(symbol="600000.SH")).passed is True


# ── 附加：`health()` 与 `get_run_state()` ────────────────────────────────
def test_health_reports_the_lkg_and_reachability():
    engine, store = make_engine()
    health = engine.health()
    assert health.available is True
    assert health.rule_version == 1
    assert health.lkg_version == 1
    assert health.store_reachable is True
    assert health.last_load_at is not None

    store.publish(rules_with(max_position_pct=10), 2)
    engine.reload()
    degraded = engine.health()
    assert degraded.run_state is RiskRunStateEnum.DEGRADED
    assert degraded.lkg_version == 1
    assert degraded.available is True


def test_run_state_escalates_from_global_to_unit_scope():
    """`get_run_state()` 是"全局有没有事"；任意单元熔断也算 `BREAKER_TRIPPED`。"""
    engine, _ = make_engine()
    assert engine.get_run_state() is RiskRunStateEnum.NORMAL
    engine.trip_breaker("ACCOUNT:ACC9", "别的账户熔断")
    assert engine.get_run_state() is RiskRunStateEnum.BREAKER_TRIPPED
    assert engine.check(request()).run_state is RiskRunStateEnum.NORMAL


def test_watchdog_can_be_started_and_stopped():
    """短轮询是 MVP 口径（契约 §四）：启停必须成对可用，且不残留线程。"""
    engine, _ = make_engine()
    engine.start_watchdog()
    engine.start_watchdog()  # 幂等：重复启动不该起第二个线程
    engine.stop_watchdog()
    engine.stop_watchdog()  # 幂等
    assert engine.is_watchdog_running() is False
