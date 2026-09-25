"""I3b：`DbRiskRuleStore` 真接线 + `risk_intercept_log` 写入方（`RiskInterceptLogWriter`）。

**这份文件要证的几件事**（对应 `docs/迭代计划.md` §四 的 I3b 收工记录 ——
I3b 是 I3 之后的一个子步，**不进 §三 的迭代表**，所以没有 §I4 那行可引）：

1. **`DbRiskRuleStore` 的七个方法真的会走 SQL**（此前三个方法一律
   `raise RiskConfigLoadError("尚未接线")`）—— 读侧把 DDL 的行映射回 `RiskRule`，
   写侧「递增版本 + UPSERT 规则 + 落审计」在**同一个事务**里，运行态两个方法
   （峰值/熔断）也照契约 §3.3.2 的形状落库。
2. **失败分两类且不混**：库里的 CHECK 拒绝（SQLSTATE 23514）⇒ `RISK_004`
   （「旧值继续生效」），其他任何失败 ⇒ `RISK_005`（`RiskConfigWriteError`，
   「不知道状态，要人看」）。两者的处置完全不同，混成一个就是「不知道自己在什么状态」。
3. **留痕按 violation 展开、只记真拦截**：一条 violation 一行；`action` 取订单级裁决
   （表的 `ck_risk_intercept_action` 只有 `REDUCE|REJECT|HALT`，**没有 PASS**）；
   没拦下却来留痕、或被拦下却零违规，都当场抛 `RiskInterceptError` —— 留痕是「这笔单
   为什么没成交」的唯一书证，少一行是藏证据，多一行是伪造证据。

**为什么这里全是假连接**：本机没有**原生** PostgreSQL 实例（`psql` 不在 PATH、无服务、无安装目录；
Docker 容器是另一回事，见下），而**测试不该依赖本地服务**（那会让
「跑不跑得起来」变成环境问题）。真库那一段走容器通道（`tools/run_sql_smoke.py`），
按纪律**不进仓、不当证据**；跨层那条端到端控制组在 `test_backtest_risk_gate.py` 里
（引擎 → 闸门 → 写入方 → SQL，两端真接起来 —— 单侧全绿证明不了缝上能跑）。

**为什么先有实现再有这份测试**：实现写在它之前，所以「先红后绿」的顺序证据不在 git
历史里。代替它的是 `tools/pytest_mutation_check.py` 里的 S14 系列 —— 把这几条判据逐处
改坏，确认本文件真的变红。
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quanauto.enums import Direction
from quanauto.errors import (
    DataStoreError,
    RiskConfigInvalidError,
    RiskConfigLoadError,
    RiskConfigWriteError,
    RiskInterceptError,
)
from quanauto import risk as risk_module
from quanauto.risk import (
    RULE_REGISTRY,
    BreakerState,
    BreakerStateEnum,
    DbRiskRuleStore,
    RiskActionEnum,
    RiskCheckRequest,
    RiskCheckResponse,
    RiskInterceptLogWriter,
    RiskRuleStore,
    RiskRunStateEnum,
    RiskSnapshot,
    RiskViolation,
    RuleChangeRequest,
    RuleScopeEnum,
    RuleUnitEnum,
    SeverityEnum,
    TighteningDirectionEnum,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = REPO_ROOT / "db" / "risk_control.sql"
DDL = DDL_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
SCOPE_KEY = "*"
RULE_ID = "max_position_pct"
RULE_VERSION = 7


# ── 假件 ──────────────────────────────────────────────────────────────────
class FakeConn:
    """假连接：按脚本吐行，记下每次 `(sql, params)` 与事务事件。

    比 `test_data_center_store.py` 里那本多一个 `error_at`：这里要测「**第几条**语句失败」
    —— 「UPSERT 被 CHECK 拒绝」与「读旧值就失败」是两条不同的分支（前者是 RISK_004，
    后者是 RISK_005），只有一个 `error` 字段就没法把两条分开测。
    再多一个 `commit_error`：驱动报约束错**不一定**在语句执行那一刻，也可能在提交时才报，
    而那条路径不经过 `_run_sql()` —— 不单独造样本它就没有主人。
    """

    def __init__(self, script=None, error=None, error_at=None, commit_error=None):
        self.script = list(script or [])
        self.error = error
        self.error_at = error_at
        self.commit_error = commit_error
        self.calls = []
        self.events = []

    def execute(self, sql, params=()):
        if self.error is not None and (self.error_at is None or len(self.calls) == self.error_at):
            raise self.error
        self.calls.append((sql, tuple(params)))
        return self.script.pop(0) if self.script else []

    @contextlib.contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            raise
        if self.commit_error is not None:
            self.events.append("rollback")
            raise self.commit_error
        self.events.append("commit")


class _DriverError(RuntimeError):
    """冒充驱动异常：`sqlstate` 属性的存在正是 `_sqlstate()` 要探的东西。"""

    def __init__(self, message: str, sqlstate: str = "") -> None:
        super().__init__(message)
        if sqlstate:
            self.sqlstate = sqlstate


def _wrapped_by_pgstore(sqlstate: str) -> DataStoreError:
    """造一个「`pgstore` 已经包过一层」的异常：外衣是 `DataStoreError`，真值在 `__cause__`。

    形状照抄 `quanauto/pgstore.py` 的 `PsycopgConnection.execute()`：
    `raise DataStoreError("执行 SQL 失败（...）：...") from exc`。
    真库上根本不存在「裸驱动异常」这条路径，所以**只测裸异常的用例证明不了真库也成立**
    —— 探针（postgres:17 容器）实测：`DataStoreError` 上取不到 sqlstate（`""`），
    而 `exc.__cause__.sqlstate == "23514"`。
    """
    try:
        raise DataStoreError(
            "[DATA_008] 执行 SQL 失败（CheckViolation）：boom"
        ) from _DriverError("violates check constraint", sqlstate=sqlstate)
    except DataStoreError as exc:
        return exc


_CHECK_VIOLATION = _DriverError("new row violates check constraint", sqlstate="23514")
_IO_FAILURE = _DriverError("connection is closed", sqlstate="08006")


class _BoomFactory:
    """连接工厂自己抛异常（生产里最常见的是连接池拿不到连接）。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise self.exc


def _store(conn) -> DbRiskRuleStore:
    return DbRiskRuleStore(lambda: conn)


def _rule_row(**overrides) -> dict:
    row = {
        "rule_id": RULE_ID,
        "scope": "GLOBAL",
        "scope_key": SCOPE_KEY,
        "threshold": Decimal("0.1"),
        "unit": "RATIO",
        "comparison": "LTE",
        "enabled": True,
        "version": 1,
        "updated_by": "system",
        "updated_at": datetime(2026, 1, 5, 9, 30),
        "description": "单票仓位上限",
    }
    row.update(overrides)
    return row


def _change(**overrides) -> RuleChangeRequest:
    payload = {
        "rule_id": RULE_ID,
        "scope": RuleScopeEnum.GLOBAL,
        "scope_key": SCOPE_KEY,
        "new_threshold": 0.05,
        "operator": "ops-a",
        "reason": "收紧单票上限",
    }
    payload.update(overrides)
    return RuleChangeRequest(**payload)


SAVE_SCRIPT = [
    [{"threshold": Decimal("0.1")}],       # 0 读旧值
    [{"version": RULE_VERSION}],           # 1 递增版本
    [{"rule_id": RULE_ID}],                # 2 UPSERT
    [],                                    # 3 审计
]


def _violation(**overrides) -> RiskViolation:
    payload = {
        "rule_id": RULE_ID,
        "rule_type": RULE_REGISTRY[RULE_ID].rule_type,
        "scope": RuleScopeEnum.GLOBAL,
        "scope_key": SCOPE_KEY,
        "threshold": 0.10,
        "observed": 0.93,
        "severity": SeverityEnum.ERROR.value,
        "action": RiskActionEnum.REDUCE,
        "message": "单票仓位超限",
    }
    payload.update(overrides)
    return RiskViolation(**payload)


def _request(**overrides) -> RiskCheckRequest:
    payload = {
        "account_id": "acc-1",
        "strategy_id": "ma-cross",
        "symbol": "600000.SH",
        "side": Direction.BUY,
        "is_open": True,
        "quantity": 9000,
        "price": 9.25,
        "snapshot": RiskSnapshot(
            total_asset=100000.0,
            available_capital=10000.0,
            strategy_equity=100000.0,
            symbol_avg_daily_amount=9050000.0,
            trading_date="2026-01-05",
        ),
    }
    payload.update(overrides)
    return RiskCheckRequest(**payload)


def _response(violations=None, **overrides) -> RiskCheckResponse:
    payload = {
        "passed": False,
        "action": RiskActionEnum.REJECT,
        "rule_version": RULE_VERSION,
        "violations": [_violation()] if violations is None else violations,
        "adjusted_quantity": 0,
        "run_state": RiskRunStateEnum.NORMAL,
        "message": "被风控拦下",
        "decision_id": "dec-20260105-1",
    }
    payload.update(overrides)
    return RiskCheckResponse(**payload)


def _table_body(name: str) -> str:
    """从 DDL 里切出一张表的表体（到行首的 `);` 为止）。"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+%s\s*\((.*?)\n\)\s*;" % re.escape(name), DDL, re.S
    )
    assert match is not None, "db/risk_control.sql 里找不到表 %s —— DDL 改过名字？" % name
    return match.group(1)


def _table_columns(name: str) -> list:
    """表体里的列名，按出现顺序（靠「列名 + 类型」的形状取，约束行取不到）。"""
    body = _table_body(name)
    return re.findall(
        r"^\s{2}(\w+)\s+(?:varchar|numeric|bigint|smallint|integer|boolean|timestamptz)\b",
        body,
        re.M,
    )


def _writes(conn) -> list:
    keywords = ("INSERT", "UPDATE", "DELETE")
    return [c for c in conn.calls if c[0].upper().lstrip().startswith(keywords)]


# ── 读侧：load_rules ─────────────────────────────────────────────────────
def test_load_rules_maps_ddl_rows_and_takes_rule_type_from_the_registry() -> None:
    """DDL 里**没有** `rule_type` 列，它只能来自注册表 —— 这条就是守这件事的。"""
    conn = FakeConn(script=[[_rule_row(threshold=Decimal("0.10000000"))]])
    rules = _store(conn).load_rules()

    assert len(rules) == 1
    rule = rules[0]
    assert rule.rule_id == RULE_ID
    assert rule.rule_type is RULE_REGISTRY[RULE_ID].rule_type
    assert rule.unit is RuleUnitEnum.RATIO
    assert rule.scope is RuleScopeEnum.GLOBAL and rule.scope_key == SCOPE_KEY
    assert rule.comparison == "LTE"
    assert rule.enabled is True
    assert rule.version == 1
    assert rule.updated_at == datetime(2026, 1, 5, 9, 30)
    assert rule.tightening_direction is TighteningDirectionEnum.DECREASE


def test_load_rules_does_not_filter_disabled_rows_so_d2_is_decided_in_one_place() -> None:
    """`enabled=0` 是**引擎**的判据（D2「视为不存在，继续向下层找」），SQL 里不许再筛一遍。

    两处各筛一次的结果是两套方言：一边「引擎找不到就报 RISK_004」，另一边「SQL 里就没了、
    引擎以为顶层缺失」。筛的地方越少，口径越只有一个。
    """
    conn = FakeConn(script=[[_rule_row(enabled=False)]])
    rules = _store(conn).load_rules()

    assert [r.enabled for r in rules] == [False], "enabled 必须原样传到引擎，不能在存储层消失"
    sql = conn.calls[0][0].upper()
    assert "ENABLED" in sql, "读的时候得把 enabled 取回来 —— 不然引擎没法按 D2 判「视为不存在」"
    assert "WHERE" not in sql, "读路径不该加 WHERE：筛选是引擎的判据，两处各筛一次就是两套方言"


def test_load_rules_rejects_a_threshold_that_is_a_hundred_times_too_big() -> None:
    """库里躺着 `10` 而不是 `0.10`：**读的时候就要炸**（D3 的 100 倍事故）。

    让 `10` 生效的后果不是「阈值大了点」：RATIO 语义下它是「1000% 仓位」，规则从此永不触发，
    而且日志里一切正常。库的 `ck_risk_rule_ratio_range` 已经挡一层，这里再挡一层是因为
    本类可能接到一个没有约束的库（副本、旧快照、手改过的表）。
    """
    conn = FakeConn(script=[[_rule_row(threshold=Decimal("10"))]])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).load_rules()


def test_load_rules_rejects_a_rule_id_outside_the_registry() -> None:
    """D3：`rule_id` 不可自定义 —— 库里多一行自定义规则不会有任何代码去读它。"""
    conn = FakeConn(script=[[_rule_row(rule_id="my_own_rule")]])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).load_rules()


def test_load_rules_rejects_a_unit_that_disagrees_with_the_registry() -> None:
    """单位错位比数值错位更隐蔽：同一个 `0.10` 在 RATIO 下是 10% 仓位，在 COUNT 下是 0 次。"""
    conn = FakeConn(script=[[_rule_row(unit="COUNT")]])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).load_rules()


def test_load_rules_rejects_an_enum_the_database_should_not_hold() -> None:
    """`scope` 被手改成枚举外的值 ⇒ 报「约束被绕过」，而不是让它静默变成 `RuleScopeEnum` 崩栈。"""
    conn = FakeConn(script=[[_rule_row(scope="EVERYTHING")]])
    with pytest.raises(RiskConfigLoadError):
        _store(conn).load_rules()


def test_load_rules_missing_column_names_the_ddl() -> None:
    """缺列要说清「与 DDL 不一致」：改过 `db/risk_control.sql` 就会走到这条。"""
    row = _rule_row()
    del row["threshold"]
    conn = FakeConn(script=[[row]])
    with pytest.raises(RiskConfigLoadError) as info:
        _store(conn).load_rules()
    assert "db/risk_control.sql" in str(info.value)


def test_load_rules_is_read_only() -> None:
    """读路径一条写语句都不许有 —— 「读一次库顺手把默认值补进去」是权限事故的常见开端。"""
    conn = FakeConn(script=[[_rule_row()]])
    _store(conn).load_rules()
    assert _writes(conn) == []
    assert conn.events == [], "读路径不该开事务"


# ── 读侧：get_version ────────────────────────────────────────────────────
def test_get_version_returns_the_single_row() -> None:
    conn = FakeConn(script=[[{"version": 12}]])
    assert _store(conn).get_version() == 12


def test_get_version_refuses_a_missing_singleton() -> None:
    """0 行 ⇒ 种子没跑。此时「取不到版本」不能变成 0：0 会被当成「版本没变过」而放行旧配置。"""
    conn = FakeConn(script=[[]])
    with pytest.raises(RiskConfigLoadError):
        _store(conn).get_version()


def test_get_version_refuses_a_broken_singleton() -> None:
    """两行 ⇒ `ck_risk_config_version_singleton` 被人拿掉了。「取第一行算了」会随手选一个版本号。"""
    conn = FakeConn(script=[[{"version": 3}, {"version": 9}]])
    with pytest.raises(RiskConfigLoadError):
        _store(conn).get_version()


# ── 失败收口（读侧）───────────────────────────────────────────────────────
def test_read_side_driver_error_is_wrapped_with_cause() -> None:
    conn = FakeConn(error=_IO_FAILURE)
    with pytest.raises(RiskConfigLoadError) as info:
        _store(conn).load_rules()
    assert isinstance(info.value.__cause__, _DriverError), "必须留 cause，否则排障只剩一句中文"
    assert "08006" in str(info.value)


def test_read_side_connection_factory_error_is_wrapped() -> None:
    """连接池拿不到连接时抛的是**池的**异常 —— 只捕驱动异常会让它绕过本层错误族。"""
    store = DbRiskRuleStore(_BoomFactory(RuntimeError("pool exhausted")))
    with pytest.raises(RiskConfigLoadError):
        store.load_rules()


def test_our_own_errors_are_not_double_wrapped() -> None:
    """本项目异常不许被二次包装：套一层会让错误码从 RISK_004 变成 RISK_005。"""
    own = RiskConfigInvalidError("[RISK_004] 阈值非法")
    conn = FakeConn(error=own)
    with pytest.raises(RiskConfigInvalidError) as info:
        _store(conn).load_rules()
    assert info.value is own


# ── 写侧：save_change 的正常路径 ─────────────────────────────────────────
def test_save_change_runs_four_statements_in_one_transaction() -> None:
    """顺序是量出来的：先读旧值（审计要记「变更前是什么」），再递增版本、UPSERT、落审计。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    _store(conn).save_change(_change())

    assert len(conn.calls) == 4, "语句条数变了：审计一条、版本一条、规则一条、读旧值一条"
    assert conn.calls[0][0].upper().lstrip().startswith("SELECT")
    assert conn.calls[1][0].upper().lstrip().startswith("UPDATE RISK_CONFIG_VERSION")
    assert conn.calls[2][0].upper().lstrip().startswith("INSERT INTO RISK_RULE ")
    assert conn.calls[3][0].upper().lstrip().startswith("INSERT INTO RISK_RULE_AUDIT")
    assert conn.events == ["begin", "commit"], "四条必须同事务，否则会出现半新半旧"


def test_save_change_returns_the_version_the_database_reported() -> None:
    """版本号只能来自库（`RETURNING version`）：自己 +1 会在并发下造出两个「版本 8」。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    assert _store(conn).save_change(_change()) == RULE_VERSION


def test_save_change_binds_the_threshold_as_decimal_not_float() -> None:
    """绑 float 等于把二进制尾数交给 `numeric`：`0.1` 会写成 `0.1000000000000000055…`。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    _store(conn).save_change(_change())
    threshold = conn.calls[2][1][3]
    assert isinstance(threshold, Decimal), "阈值必须以 Decimal 绑定，收到 %r" % (type(threshold),)
    assert str(threshold) == "0.05"


def test_save_change_keeps_the_registry_unit_and_comparison() -> None:
    """单位和比较符来自注册表：请求里没有它们，写错等于把 D3 的口径从库里改掉。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    _store(conn).save_change(_change())
    _rule_id, _scope, _key, _threshold, unit, comparison = conn.calls[2][1][:6]
    assert unit == "RATIO"
    assert comparison == "LTE"


def test_save_change_writes_the_audit_direction_tighten() -> None:
    """0.10 → 0.05 是收紧。审计里的 `old` 用**定点**写法（`0.1` 而不是 `1E-1`）。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    _store(conn).save_change(_change(new_threshold=0.05))
    old, new, direction = conn.calls[3][1][3], conn.calls[3][1][4], conn.calls[3][1][5]
    assert (old, new, direction) == ("0.1", "0.05", "TIGHTEN")


def test_save_change_writes_the_audit_direction_relax() -> None:
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    _store(conn).save_change(_change(new_threshold=0.2))
    assert conn.calls[3][1][5] == "RELAX"


def test_save_change_direction_follows_the_registry_not_the_size(monkeypatch) -> None:
    """方向按**注册表**判，不按大小判。

    今天六条规则的收紧方向都是 DECREASE，所以「变小=收紧」看着永远对 —— 这正是它危险
    的地方：将来加一条「次数上限」这类反向规则（阈值变大才是收紧）时，写死的判据会把
    方向记反，而审计里两个词看起来都很正常。
    """
    spec = RULE_REGISTRY["max_daily_trades"]
    monkeypatch.setitem(
        risk_module.RULE_REGISTRY, "max_daily_trades",
        replace(spec, tightening_direction=TighteningDirectionEnum.INCREASE),
    )
    script = [
        [{"threshold": Decimal("3")}],
        [{"version": RULE_VERSION}],
        [{"rule_id": "max_daily_trades"}],
        [],
    ]
    conn = FakeConn(script=[list(step) for step in script])
    _store(conn).save_change(_change(rule_id="max_daily_trades", new_threshold=5))
    assert conn.calls[3][1][5] == "TIGHTEN", "3 → 5 在 INCREASE 语义下是收紧，方向记反了"


def test_save_change_unchanged_value_has_no_direction() -> None:
    """值没变 ⇒ 方向留空（DDL 允许 `''`）。写 `TIGHTEN` 会让审计表出现一笔没发生过的收紧。"""
    script = [
        [{"threshold": Decimal("0.05")}],
        [{"version": RULE_VERSION}],
        [{"rule_id": RULE_ID}],
        [],
    ]
    conn = FakeConn(script=[list(step) for step in script])
    _store(conn).save_change(_change(new_threshold=0.05))
    assert conn.calls[3][1][5] == ""


def test_save_change_without_an_existing_row_leaves_the_old_threshold_blank() -> None:
    """新建一层（D2 允许）时没有「变更前」可言。

    变更前的**生效**值来自更高的层，存储层算不出继承链 —— 编一个（比如拿默认值顶）就是
    把分层再实现一遍，而那正是「两套方言」的来源。
    """
    script = [
        [],
        [{"version": RULE_VERSION}],
        [{"rule_id": RULE_ID}],
        [],
    ]
    conn = FakeConn(script=[list(step) for step in script])
    _store(conn).save_change(_change(scope=RuleScopeEnum.STRATEGY, scope_key="ma-cross"))
    assert conn.calls[3][1][3] == "", "没有旧行时 old_threshold 必须留空"
    assert conn.calls[3][1][5] == ""


# ── 写侧：应用层校验重做（绕过引擎的调用方也要撞上同一道闸）────────────────
def test_save_change_rejects_an_out_of_range_threshold_before_any_sql() -> None:
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).save_change(_change(new_threshold=10.0))
    assert conn.calls == [], "非法值必须在开事务之前就拦住，不许先写进去再回滚"


def test_save_change_rejects_a_blank_operator_before_any_sql() -> None:
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).save_change(_change(operator="   "))
    assert conn.calls == []


def test_save_change_rejects_a_blank_reason_before_any_sql() -> None:
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).save_change(_change(reason=""))
    assert conn.calls == []


def test_save_change_rejects_a_global_change_without_the_star_key() -> None:
    """GLOBAL 是兜底层，`scope_key` 必须固定 `*`（DDL 里也有 `ck_risk_rule_global_key`）。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT])
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).save_change(_change(scope_key="600000.SH"))
    assert conn.calls == []


# ── 写侧：两类失败必须分开 ────────────────────────────────────────────────
def test_save_change_check_violation_becomes_risk_004() -> None:
    """CHECK 拒绝 ⇒ RISK_004「这次变更不合法，**旧值继续生效**」（可改可重试）。

    这条是本文件里唯一抓到过真 bug 的判据：第一版把 `23514` 的判据写在外层 `except`，
    而 `_run_sql()` 已经把驱动异常包成了 RISK_005（本项目异常）⇒ 外层原样放行 ⇒
    「库拒绝 ⇒ RISK_004」只在 docstring 里成立。
    """
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT],
                    error=_CHECK_VIOLATION, error_at=2)
    with pytest.raises(RiskConfigInvalidError) as info:
        _store(conn).save_change(_change())
    assert "RISK_004" in str(info.value)
    assert isinstance(info.value.__cause__, _DriverError)
    assert conn.events == ["begin", "rollback"], "被拒的那笔不许留下 commit"


def test_save_change_check_violation_at_the_commit_boundary_is_also_risk_004() -> None:
    """同一个 `23514` 若在**提交边界**才冒出来，也得是 RISK_004。

    这条路径不经过 `_run_sql()`，是「判据只写在一半路径上」最容易漏的那一半。
    """
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT], commit_error=_CHECK_VIOLATION)
    with pytest.raises(RiskConfigInvalidError) as info:
        _store(conn).save_change(_change())
    assert "RISK_004" in str(info.value)
    assert conn.events == ["begin", "rollback"]


def test_save_change_check_violation_wrapped_by_pgstore_is_still_risk_004() -> None:
    """真库路径的**真实形状**：`CheckViolation` 先被 `pgstore` 包成 `DataStoreError`。

    这条是**只在真库上才看得见**的缺陷（探针实测，postgres:17 容器）：
    `PsycopgConnection.execute()` 把驱动异常收口成 `DataStoreError`（`DATA_008`，
    **数据中心**错误族）并 `from exc` ⇒ `sqlstate` 只存在于 `__cause__` 上。旧实现
    `except QuanAutoError: raise` 把它原样放行，于是：
      * 调用方拿到 `DATA_008`，`except RiskError` **接不住**（它不是风控族）；
      * 即使走进 `_wrapped_sql_error()`，`_sqlstate()` 只探本层属性也取不到 `23514`
        ⇒ 落到 RISK_005，冒充「状态未知」。
    两条都错，而**裸驱动异常的假连接永远测不到** —— 这正是「跨层必须真接起来」的样本。
    """
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT],
                    error=_wrapped_by_pgstore("23514"), error_at=2)
    with pytest.raises(RiskConfigInvalidError) as info:
        _store(conn).save_change(_change())
    assert "RISK_004" in str(info.value)
    assert isinstance(info.value.__cause__, DataStoreError), "包装层要留在链上，排障才看得到 CheckViolation"
    assert "23514" in str(info.value)
    assert conn.events == ["begin", "rollback"], "被拒的那笔不许留下 commit"


def test_save_change_check_violation_wrapped_by_pgstore_at_the_commit_boundary_too() -> None:
    """同一个包装过的 `23514` 若在**提交边界**才冒出来，也得是 RISK_004（两条路径同一判据）。"""
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT],
                    commit_error=_wrapped_by_pgstore("23514"))
    with pytest.raises(RiskConfigInvalidError) as info:
        _store(conn).save_change(_change())
    assert "RISK_004" in str(info.value)
    assert conn.events == ["begin", "rollback"]


def test_save_change_wrapped_io_failure_still_becomes_a_write_error() -> None:
    """反面对照：包装过的 `08006` **不许**被当成约束拒绝 —— 追链不等于「一律转 RISK_004」。

    少了这条，把判据写成「链上有异常就算 CHECK 拒绝」也能全绿，而那样一来
    「连不上」会被冒充成「值不合法，改一下再试」，运维会去改一个没问题的阈值。
    """
    conn = FakeConn(script=[[]], error=_wrapped_by_pgstore("08006"), error_at=0)
    with pytest.raises(RiskConfigWriteError) as info:
        _store(conn).save_change(_change())
    assert info.value.code == "RISK_005"


def test_sqlstate_walks_the_cause_chain_and_stops_on_a_cycle() -> None:
    """`_sqlstate()` 自身的形状：穿一层包装、无链给 `""`、有环不死循环。"""
    assert risk_module._sqlstate(_wrapped_by_pgstore("23514")) == "23514"
    assert risk_module._sqlstate(_DriverError("nothing here")) == ""
    first = _DriverError("a")
    second = _DriverError("b")
    first.__cause__, second.__cause__ = second, first
    assert risk_module._sqlstate(first) == "", "有环的异常链不许把取 SQLSTATE 变成死循环"


def test_driver_facing_errors_from_pgstore_are_collected_into_the_risk_family() -> None:
    """真库上驱动失败的**唯一**形状是 `DataStoreError` —— 取连接这条也得收口。

    取连接失败（池拿不到连接 / DSN 不通）在真库上同样先被 `pgstore` 包一层。若放行
    `QuanAutoError`，调用方拿到 `DATA_008`：风控的写入失败以**别的族**的码逃出去，
    `except RiskError` 接不住，而熔断降级逻辑全靠那个 except。
    """
    store = DbRiskRuleStore(_BoomFactory(_wrapped_by_pgstore("08006")))
    with pytest.raises(RiskConfigWriteError) as info:
        store.save_change(_change())
    assert info.value.code == "RISK_005"
    assert isinstance(info.value.__cause__, DataStoreError)


def test_save_change_other_failure_becomes_a_write_error() -> None:
    """连不上/超时 ⇒ RISK_005（`RiskConfigWriteError`）：状态未知，不能冒充「旧值仍生效」。"""
    conn = FakeConn(script=[[]], error=_IO_FAILURE, error_at=0)
    with pytest.raises(RiskConfigWriteError) as info:
        _store(conn).save_change(_change())
    assert info.value.code == "RISK_005"
    assert isinstance(info.value.__cause__, _DriverError)


def test_save_change_rolls_back_when_a_later_statement_fails() -> None:
    """审计行写不进去 ⇒ 整笔回滚。

    只留下「阈值改了但审计里查不到」的后果是 D10 追责时查无实据，而版本号已经动过
    （D1 的变更检测以为配置换了一版）—— 半新半旧比整笔失败危险得多。
    """
    conn = FakeConn(script=[list(step) for step in SAVE_SCRIPT],
                    error=_IO_FAILURE, error_at=3)
    with pytest.raises(RiskConfigWriteError):
        _store(conn).save_change(_change())
    assert conn.events == ["begin", "rollback"]


def test_save_change_refuses_when_the_version_bump_touched_nothing() -> None:
    """`UPDATE ... RETURNING` 返回 0 行 ⇒ 单例行 id=1 不见了，不许默默返回 0 版本。"""
    script = [[{"threshold": Decimal("0.1")}], [], [{"rule_id": RULE_ID}], []]
    conn = FakeConn(script=[list(step) for step in script])
    with pytest.raises(RiskConfigWriteError):
        _store(conn).save_change(_change())


def test_save_change_refuses_when_the_upsert_returned_nothing() -> None:
    """UPSERT 没返回行 ⇒ 数据没落盘。`rowcount`/`RETURNING` 二选一，这里选后者免得口径分裂。"""
    script = [[{"threshold": Decimal("0.1")}], [{"version": RULE_VERSION}], [], []]
    conn = FakeConn(script=[list(step) for step in script])
    with pytest.raises(RiskConfigWriteError):
        _store(conn).save_change(_change())


def test_write_side_connection_factory_error_is_wrapped() -> None:
    store = DbRiskRuleStore(_BoomFactory(TypeError("dsn is not a string")))
    with pytest.raises(RiskConfigWriteError):
        store.save_change(_change())


# ── 运行态：峰值（D8）与熔断（D7）────────────────────────────────────────
def test_peak_upsert_is_monotonic_in_sql() -> None:
    """`GREATEST` 让「写一个更小的值」在库里也降不下来 —— 峰值是单调的（D8）。"""
    assert "GREATEST" in DbRiskRuleStore.PEAK_UPSERT_SQL
    conn = FakeConn()
    _store(conn).save_equity_peak("account:acc-1", 123456.789)
    params = conn.calls[0][1]
    assert params[0] == "account:acc-1"
    assert isinstance(params[1], Decimal) and str(params[1]) == "123456.789"


def test_load_equity_peaks_returns_pairs() -> None:
    conn = FakeConn(script=[[
        {"peak_key": "account:acc-1", "peak_value": Decimal("123456.789")},
        {"peak_key": "strategy:ma-cross", "peak_value": Decimal("90000")},
    ]])
    peaks = _store(conn).load_equity_peaks()
    assert peaks == [("account:acc-1", 123456.789), ("strategy:ma-cross", 90000.0)]


def test_save_equity_peak_refuses_a_blank_key() -> None:
    conn = FakeConn()
    with pytest.raises(RiskConfigInvalidError):
        _store(conn).save_equity_peak("  ", 1.0)
    assert conn.calls == []


def test_breaker_upsert_never_touches_the_resume_reason_column() -> None:
    """`resume_reason` 不在 `BreakerState` 里，所以**更新时也不覆盖它**。

    一次恢复的理由不该被下一次触发抹掉 —— 那正是事后追责要看的东西（「上次是谁在什么
    理由下解开的」）。取不到就不写，而不是写个空串把它盖掉。
    """
    assert "resume_reason" not in DbRiskRuleStore.BREAKER_UPSERT_SQL.lower()


def test_breaker_upsert_binds_enum_values_not_objects() -> None:
    conn = FakeConn()
    _store(conn).save_breaker_state(BreakerState(
        breaker_key="strategy:ma-cross",
        state=BreakerStateEnum.TRIPPED,
        triggered_at=datetime(2026, 1, 5, 10, 0),
        trigger_reason="单策略回撤 16%",
    ))
    params = conn.calls[0][1]
    assert params[0] == "strategy:ma-cross"
    assert params[1] == "TRIPPED", "绑的是枚举成员而不是它的值，驱动会把它写成对象 repr"
    assert params[3] == "单策略回撤 16%"


def test_load_breaker_states_normalises_aware_timestamps_to_naive_local() -> None:
    """`timestamptz` 回读是 aware，`_now()` 是 naive —— 不归一化则在 `==` 上永远不等。

    断言用的是「同一个绝对时刻」的判据（`fromtimestamp(timestamp())`），所以与本机时区无关：
    写死小时数会在非 UTC+8 的机器上假红。
    """
    aware = datetime(2026, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    conn = FakeConn(script=[[{
        "breaker_key": "account:acc-1",
        "state": "TRIPPED",
        "triggered_at": aware,
        "trigger_reason": "全账户回撤 21%",
        "resumed_at": None,
        "resumed_by": "",
    }]])
    state = _store(conn).load_breaker_states()[0]
    assert state.state is BreakerStateEnum.TRIPPED
    assert state.triggered_at is not None and state.triggered_at.tzinfo is None
    assert state.triggered_at == datetime.fromtimestamp(aware.timestamp())
    assert state.resumed_at is None


def test_load_breaker_states_rejects_an_unknown_state() -> None:
    conn = FakeConn(script=[[{"breaker_key": "k", "state": "HALF_OPEN",
                              "triggered_at": None, "trigger_reason": "",
                              "resumed_at": None, "resumed_by": ""}]])
    with pytest.raises(RiskConfigLoadError):
        _store(conn).load_breaker_states()


def test_the_store_offers_every_method_the_contract_declares() -> None:
    """契约 §3.3.1 三个 + §3.3.2 四个。少一个的后果是「看着接线了但其实没有」。"""
    declared = {
        "load_rules", "get_version", "save_change",
        "save_equity_peak", "load_equity_peaks", "save_breaker_state", "load_breaker_states",
    }
    missing = {name for name in declared if not hasattr(DbRiskRuleStore, name)}
    assert missing == set(), "契约声明了但存储没实现：%s" % sorted(missing)
    # 「三方法面」的 ABC 必须仍然只是那三个：多塞进 ABC 会让契约的 3/7 分层失去意义。
    assert RiskRuleStore.__abstractmethods__ == frozenset({"load_rules", "get_version", "save_change"})


def test_risk_module_does_not_import_a_database_driver() -> None:
    """`quanauto` 不为风控多背一个依赖（连接由外部工厂给）。

    判据看**源码文本**而不是 `sys.modules`：别的测试可能已经 import 过 psycopg（真库探针
    那条路径），用 `sys.modules` 会随测试顺序变红 —— 那是最说不清的一类假红。
    """
    source = (REPO_ROOT / "quanauto" / "risk.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    offenders = [
        line for line in source.split("\n")
        if re.match(r"\s*(import|from)\s+(psycopg|psycopg2|sqlalchemy|asyncpg)\b", line)
    ]
    assert offenders == []


# ── 留痕写入方：展开 ─────────────────────────────────────────────────────
def test_writer_sql_is_one_statement_for_all_rows() -> None:
    """一次拦截 = N 行 = **一条** INSERT（批量），而不是 N 条 —— 订单路径上的往返次数要可控。"""
    sql = RiskInterceptLogWriter.insert_sql(3)
    assert sql.count("%s") == 14 * 3
    assert sql.upper().count("INSERT INTO RISK_INTERCEPT_LOG") == 1


def test_writer_refuses_zero_rows_in_the_insert_sql() -> None:
    """0 行的 INSERT 是空操作：它会安静地什么都不写，而调用方以为记下了。"""
    with pytest.raises(RiskInterceptError):
        RiskInterceptLogWriter.insert_sql(0)


def test_writer_expands_one_row_per_violation_with_the_order_level_action() -> None:
    """一条 violation 一行；`action` 取**订单级**裁决。

    粒度不一样：内存里的 `blocked_orders` 是一订单一行、`rule_ids` 是列表，而这里的
    `rule_id` / `threshold` / `observed` 是逐条字段 —— 把列表塞进一个 `rule_id` 列会让
    这一行无法归属到规则（「哪条规则拦的」当场失传）。
    `action` 取订单级还让这些行与 `risk_summary()["blocked_orders"][].action` 同值。
    """
    writer = RiskInterceptLogWriter(FakeConn)
    violations = [
        _violation(rule_id="max_order_amount_pct", severity=SeverityEnum.WARNING.value,
                   action=RiskActionEnum.PASS, threshold=0.10, observed=9.93),
        _violation(rule_id="max_position_pct", severity=SeverityEnum.ERROR.value,
                   action=RiskActionEnum.REDUCE, threshold=0.10, observed=0.93),
    ]
    rows = writer.rows_for(_request(), _response(violations=violations),
                           created_at=datetime(2026, 1, 5, 9, 30))

    assert len(rows) == 2, "两条 violation 必须展开成两行"
    assert [row[4] for row in rows] == ["max_order_amount_pct", "max_position_pct"]
    assert [row[11] for row in rows] == ["REJECT", "REJECT"], (
        "action 必须是订单级裁决：单条 violation 的 action 可以是 PASS（WARNING），"
        "而表的 CHECK 里根本没有 PASS"
    )
    assert [row[10] for row in rows] == ["WARNING", "ERROR"]
    assert [row[13] for row in rows] == [datetime(2026, 1, 5, 9, 30)] * 2


def test_writer_binds_the_request_identity_and_the_violation_scope() -> None:
    writer = RiskInterceptLogWriter(FakeConn)
    rows = writer.rows_for(
        _request(),
        _response(violations=[_violation(scope=RuleScopeEnum.STRATEGY, scope_key="ma-cross")]),
    )
    row = rows[0]
    assert row[:4] == ("dec-20260105-1", "acc-1", "ma-cross", "600000.SH")
    assert row[5] == "STRATEGY" and row[6] == "ma-cross"
    assert row[7] == RULE_VERSION


def test_writer_binds_threshold_and_observed_as_decimal() -> None:
    writer = RiskInterceptLogWriter(FakeConn)
    row = writer.rows_for(_request(), _response())[0]
    assert isinstance(row[8], Decimal) and isinstance(row[9], Decimal)
    assert str(row[8]) == "0.1"


def test_writer_refuses_a_pass_decision() -> None:
    """PASS 不该留痕。

    这不是「少记一笔」而是「记错」—— 最常见的原因是调用方用 try/except 把整个闸门包起来，
    把没拦的也写了进去。表的 `ck_risk_intercept_action` 没有 PASS，所以这种行要么当场炸、
    要么被库拒掉，两种都比安静写进去好。
    """
    writer = RiskInterceptLogWriter(FakeConn)
    with pytest.raises(RiskInterceptError):
        writer.rows_for(_request(), _response(action=RiskActionEnum.PASS))


def test_writer_refuses_a_block_without_violations() -> None:
    """被拦下却零违规 ⇒ 展开出 0 行（「拦了一次但没有原因」），不许静默变成空写入。"""
    writer = RiskInterceptLogWriter(FakeConn)
    with pytest.raises(RiskInterceptError):
        writer.rows_for(_request(), _response(violations=[]))


def test_writer_truncates_a_message_longer_than_the_column() -> None:
    """`message` 是 `varchar(512)`：超长由**我们**截断，否则库会整笔拒掉一次拦截的留痕。"""
    writer = RiskInterceptLogWriter(FakeConn)
    row = writer.rows_for(_request(), _response(violations=[_violation(message="长" * 900)]))[0]
    assert len(row[12]) == RiskInterceptLogWriter.MESSAGE_MAX


def test_writer_uses_the_caller_timestamp_not_the_wall_clock() -> None:
    """`created_at` 必须是调用方给的那根 K 线时间。

    用墙钟的后果：同一条命令跑两次得到两份不同的留痕，回测库里两轮的行对不上，
    「这轮为什么没成交」就查不出来了 —— 那正是 `R5` 逐字节可复现要防的事。
    """
    conn = FakeConn()
    writer = RiskInterceptLogWriter(lambda: conn)
    stamp = datetime(2026, 1, 5, 9, 30)
    assert writer.record(_request(), _response(), created_at=stamp) == 1

    sql, params = conn.calls[0]
    assert params[13] == stamp
    assert sql == RiskInterceptLogWriter.insert_sql(1)
    assert len(params) == 14


def test_writer_wraps_a_driver_error_and_passes_our_errors_through() -> None:
    with pytest.raises(RiskConfigWriteError):
        RiskInterceptLogWriter(lambda: FakeConn(error=_IO_FAILURE)).record(_request(), _response())

    own = RiskConfigInvalidError("[RISK_004] 自家人抛的")
    with pytest.raises(RiskConfigInvalidError) as info:
        RiskInterceptLogWriter(lambda: FakeConn(error=own)).record(_request(), _response())
    assert info.value is own


# ── 与 DDL 对表（列清单、列宽）───────────────────────────────────────────
def test_writer_column_list_matches_the_ddl_table_body() -> None:
    """写入方的列清单必须**等于** DDL 的列（除了 IDENTITY 主键）。

    这是本文件里唯一的跨层判据：列名少一个 ⇒ 那一列永远留默认值（`scope_key` 空着，
    「哪一层拦的」失传）；多一个 ⇒ 库整笔拒掉，而报错只说「column does not exist」。
    """
    columns = set(_table_columns("risk_intercept_log"))
    assert columns, "从 DDL 里一个列名都没提取到 —— 提取器坏了，这条判据就在空转"
    declared = set(RiskInterceptLogWriter.ROW_COLUMNS)
    assert columns - declared == {"intercept_id"}, "DDL 有写入方不写的列（只许是 IDENTITY 主键）"
    assert declared - columns == set(), "写入方绑了 DDL 里没有的列：%s" % sorted(declared - columns)


def test_writer_message_width_matches_the_ddl() -> None:
    match = re.search(r"^\s*message\s+varchar\((\d+)\)", _table_body("risk_intercept_log"), re.M)
    assert match is not None, "DDL 里找不到 message 的列宽 —— 提取器坏了"
    assert RiskInterceptLogWriter.MESSAGE_MAX == int(match.group(1)) == 512


def test_store_rule_columns_match_the_ddl_table_body() -> None:
    """读侧显式列出的列也必须在 DDL 里存在（`SELECT a` 少了列是运行时才发现的事）。"""
    columns = set(_table_columns("risk_rule"))
    assert columns, "从 DDL 里一个列名都没提取到 —— 提取器坏了"
    selected = set(re.findall(r"[,\s]([a-z_]+)(?=,| FROM)", DbRiskRuleStore.RULES_SQL))
    missing = {name for name in selected if name not in columns}
    assert missing == set(), "SELECT 里出现了 DDL 没有的列：%s" % sorted(missing)
    assert {"rule_id", "threshold", "unit", "enabled"} <= selected
