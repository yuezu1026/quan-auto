"""I4 绩效看板的回归测试：**看板只读数，不算数**。

这个文件盯着一件事：看板里出现的每一个数，都必须是**报告里已经存在的那个数**，
而不是看板自己又算了一遍。

为什么这件事需要机器来盯
------------------------
看板最容易出的错不是崩溃，而是**悄悄多算了几个百分点**。造一份「指标与净值曲线
对不上」的报告（或者看板自己用净值曲线重算一遍），两边都能跑绿、都能打印一张漂亮
的表，只有拿着两份数逐位比才会发现。所以这里全部断言都是**等式**
（`view` 里的值 == 报告里的值），没有一条是「大于 0」这种闭眼能过的东西。

三条硬规矩（对应 I4 DoD 的四件套）
---------------------------------
1. **字段清单双向对齐**：`METRIC_SPECS` 的 key 集合与 `PerformanceMetrics` 的
   dataclass 字段集合**完全相等**。少一个 = 看板漏显示一个指标；多一个 = 看板
   发明了一个上游没有的指标（`不得另立一套指标定义`）。
2. **读数即报告**：改报告里任意一个指标，`read_view` 出来的值跟着变 —— 这是
   「看板没有自己算」的可执行形式（`test_read_view_follows_the_report_not_the_curve`）。
3. **渲染两次逐字节相同**：同一份结果文件渲染两次，数值逐位一致（I4 DoD 产物判定）。

**关于「先红后绿」**：和 I1 一样，实现写在本文件之前，TDD 的顺序证据不在 git 历史里。
代替它的是 `tools/pytest_mutation_check.py` 里打在看板上的变异样本，
以及 `tools/verify_dashboard.py --selftest` 的触发测试。
"""

from __future__ import annotations

import copy
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from quanauto.cli import build_parser, run_backtest
from quanauto.dashboard import (
    METRIC_SPECS,
    UNIT_COUNT,
    UNIT_DAYS,
    UNIT_MONEY,
    UNIT_PERCENT,
    UNIT_RATIO,
    curve_stats,
    format_metric,
    read_view,
    render_html,
    render_text,
)
from quanauto.errors import DashboardError
from quanauto.models import PerformanceMetrics

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "sample_prices.csv")


# ── 夹具 ──────────────────────────────────────────────────────────────────
def synthetic_payload(seed: int = 7) -> dict:
    """一份**手写**的报告。刻意与真回测解耦：一致性判据不该依赖行情文件的内容。

    注意这里故意让 `total_return`（12.34%）与净值曲线对不上（曲线只涨 1%）：
    这正是「看板必须显示报告里那个数」的可观测形式 —— 若看板从曲线重算，
    两个数会长得一模一样，那条测试就永远绿了。

    回撤的符号按 `performance.drawdown_series` 的约定：**正数百分比**
    （0.05 = 回撤 5%）。写成负数会让「曲线最大回撤」永远显示 0。
    """
    curve = [
        {
            "timestamp": "2024-01-0%dT00:00:00" % (i + 1),
            "equity": 100000.0 + i * 250.0,
            "cash": 50000.0,
            "market_value": 50000.0 + i * 250.0,
            "drawdown": 0.001 * i,
        }
        for i in range(5)
    ]
    perf = asdict(PerformanceMetrics())
    perf.update(
        {
            "total_return": 0.1234,
            "annual_return": 0.25,
            "max_drawdown": 0.0876,
            "sharpe_ratio": 1.5,
            "sortino_ratio": 2.25,
            "calmar_ratio": 2.0,
            "win_rate": 0.5,
            "profit_factor": 1.75,
            "profit_loss_ratio": 1.25,
            "max_consecutive_losses": 3,
            "avg_hold_period": 4.5,
            "total_trades": 8,
            "total_commission": 12.34,
            "final_equity": 101000.0,
        }
    )
    return {
        "schema": "quanauto.backtest-report/1",
        "inputs": {"seed": seed},
        "deterministic": {
            "strategy_id": "ma-cross",
            "status": "completed",
            "data_version": "csv:abc#5",
            "strategy_version": "MA_Cross_Strategy@v1",
            "params_used": {"short_window": 5, "long_window": 20},
            "performance": perf,
            "equity_curve": curve,
            "trades": [{"trade_id": "t1"}, {"trade_id": "t2"}],
            "orders": [{"order_id": "o1"}, {"order_id": "o2"}, {"order_id": "o3"}],
            "account_history": [{"timestamp": "2024-01-01T00:00:00"}],
            "validation_report": {"is_valid": True, "row_count": 5},
        },
        "runtime": {"duration_ms": 0},
    }


@pytest.fixture()
def payload() -> dict:
    return synthetic_payload()


@pytest.fixture(scope="module")
def real_payload() -> dict:
    """真跑一次回测（跨层控制组：引擎 → 报告 → 看板，这条缝必须真的接起来）。"""
    args = build_parser().parse_args(
        ["backtest", "--strategy-csv", FIXTURE, "--slippage-pct", "0", "--seed", "7"]
    )
    return run_backtest(args)


# ── 1. 字段清单：看板不许自己发明指标 ─────────────────────────────────────
def test_specs_match_performance_metrics_exactly():
    """双向相等。少一个 = 漏显示；多一个 = 看板发明了上游不存在的指标。"""
    spec_keys = {spec.key for spec in METRIC_SPECS}
    model_keys = {f.name for f in fields(PerformanceMetrics)}
    assert spec_keys == model_keys, (
        "看板字段清单与 PerformanceMetrics 不一致："
        "只在看板里=%s，只在模型里=%s"
        % (sorted(spec_keys - model_keys), sorted(model_keys - spec_keys))
    )


def test_specs_are_unique_and_ordered_by_group():
    keys = [spec.key for spec in METRIC_SPECS]
    assert len(keys) == len(set(keys)), "同一个指标登记了两次：%s" % keys
    seen: list = []
    for spec in METRIC_SPECS:
        if not seen or seen[-1] != spec.group:
            seen.append(spec.group)
    assert len(seen) == len(set(seen)), "同一个分组被拆成不相邻的几段：%s" % seen


def test_every_spec_has_a_label_and_known_unit():
    units = {UNIT_PERCENT, UNIT_RATIO, UNIT_COUNT, UNIT_MONEY, UNIT_DAYS}
    for spec in METRIC_SPECS:
        assert spec.label.strip(), "%s 没有显示名" % spec.key
        assert spec.unit in units, "%s 的量纲 %r 没登记" % (spec.key, spec.unit)
        assert spec.digits >= 0


# ── 2. 读数：只从报告里取 ─────────────────────────────────────────────────
def test_read_view_reads_every_metric_from_the_report(payload):
    view = read_view(payload)
    report_perf = payload["deterministic"]["performance"]
    assert len(view.metrics) == len(METRIC_SPECS)
    for read in view.metrics:
        assert read.value == report_perf[read.key], read.key
        assert read.text == format_metric(
            next(s for s in METRIC_SPECS if s.key == read.key), read.value
        )


def test_read_view_follows_the_report_not_the_curve(payload):
    """把报告里一个指标改掉，看板跟着变。

    这是「看板没有自己算」的可执行形式：若看板从 `equity_curve` 重算，
    改这个字段不会影响任何输出，本条立刻红。
    """
    payload["deterministic"]["performance"]["sharpe_ratio"] = 9.75
    view = read_view(payload)
    sharpe = next(r for r in view.metrics if r.key == "sharpe_ratio")
    assert sharpe.value == 9.75
    assert sharpe.text == format_metric(
        next(s for s in METRIC_SPECS if s.key == "sharpe_ratio"), 9.75
    )


def test_read_view_is_pure(payload):
    """读一次不许改动输入（看板是只读视图，不是流水线上的一站）。"""
    before = copy.deepcopy(payload)
    first = read_view(payload)
    second = read_view(payload)
    assert payload == before, "read_view 改动了传入的报告"
    assert first == second


def test_header_comes_from_the_report(payload):
    view = read_view(payload)
    det = payload["deterministic"]
    assert view.strategy_id == det["strategy_id"]
    assert view.status == det["status"]
    assert view.data_version == det["data_version"]
    assert view.strategy_version == det["strategy_version"]
    assert view.seed == payload["inputs"]["seed"]
    assert view.trade_count == len(det["trades"])
    assert view.order_count == len(det["orders"])
    assert len(view.curve) == len(det["equity_curve"])


def test_curve_is_taken_verbatim_from_the_report(payload):
    """资金曲线逐点照抄，包括 drawdown —— 看板不重画一条自己的曲线。"""
    view = read_view(payload)
    for point, raw in zip(view.curve, payload["deterministic"]["equity_curve"]):
        assert point.timestamp == raw["timestamp"]
        assert point.equity == raw["equity"]
        assert point.drawdown == raw["drawdown"]


def test_missing_structure_segment_reads_as_dash_not_zero(payload):
    """结构计数缺段 ⇒ 显示 `-`，不是 0。

    「报告里没有 trades 段」与「一笔成交都没有」是两件事，写成 0 就是把前者伪装成
    后者 —— 看板会安安静静地告诉读者这个策略没交易过。
    """
    del payload["deterministic"]["trades"]
    del payload["deterministic"]["orders"]
    view = read_view(payload)
    assert view.trade_count is None
    assert view.order_count is None
    text = render_text(view)
    assert "  %-14s %s" % ("成交笔数", "-") in text
    assert "  %-14s %s" % ("订单笔数", "-") in text


def test_curve_stats_are_taken_from_the_curve(payload):
    """曲线汇总值只能是曲线的 min / max / 末值，不是别处来的数。"""
    stats = curve_stats(read_view(payload))
    equities = [p["equity"] for p in payload["deterministic"]["equity_curve"]]
    assert stats.points == len(equities)
    assert stats.low == min(equities)
    assert stats.high == max(equities)
    assert stats.last == equities[-1]
    assert stats.max_drawdown == max(
        p["drawdown"] for p in payload["deterministic"]["equity_curve"]
    )


def test_max_drawdown_uses_max_not_min(payload):
    """回撤是**正数百分比**，所以最大回撤取 `max`。

    这条守着一个真实踩过的坑：报告里 drawdown 全是正数（`drawdown_series`
    返回 `(peak - value) / peak`），写成 `min` 会恒等于 0.000000，
    而那看起来就像「这个策略从来没回过撤」—— 一个不会报错、只会骗人的错。
    """
    stats = curve_stats(read_view(payload))
    assert stats.max_drawdown > 0, "回撤样本必须非零，否则 min/max 写错也看不出来"
    assert stats.max_drawdown != min(
        p["drawdown"] for p in payload["deterministic"]["equity_curve"]
    )


# ── 3. 防空转：读不到就抛，绝不退回 0 ─────────────────────────────────────
def test_missing_deterministic_section_raises():
    with pytest.raises(DashboardError) as exc:
        read_view({"schema": "quanauto.backtest-report/1"})
    assert "deterministic" in str(exc.value)


def test_payload_must_be_a_mapping():
    with pytest.raises(DashboardError):
        read_view(["not", "a", "report"])  # type: ignore[arg-type]


def test_empty_performance_section_raises(payload):
    """`{}` 与「14 个指标恰好都是 0」长得一样 —— 前者必须拒绝，否则就是空转。"""
    payload["deterministic"]["performance"] = {}
    with pytest.raises(DashboardError) as exc:
        read_view(payload)
    assert "performance" in str(exc.value)


def test_missing_metric_key_is_named(payload):
    del payload["deterministic"]["performance"]["sortino_ratio"]
    with pytest.raises(DashboardError) as exc:
        read_view(payload)
    assert "sortino_ratio" in str(exc.value)


def test_negative_or_non_numeric_metric_raises(payload):
    payload["deterministic"]["performance"]["sharpe_ratio"] = "很高"
    with pytest.raises(DashboardError) as exc:
        read_view(payload)
    assert "sharpe_ratio" in str(exc.value)


def test_nan_metric_raises(payload):
    payload["deterministic"]["performance"]["sharpe_ratio"] = float("nan")
    with pytest.raises(DashboardError):
        read_view(payload)


def test_missing_curve_raises(payload):
    payload["deterministic"]["equity_curve"] = []
    with pytest.raises(DashboardError) as exc:
        read_view(payload)
    assert "equity_curve" in str(exc.value)


def test_curve_point_missing_field_raises(payload):
    del payload["deterministic"]["equity_curve"][2]["drawdown"]
    with pytest.raises(DashboardError) as exc:
        read_view(payload)
    assert "drawdown" in str(exc.value)


def test_fractional_count_raises(payload):
    """计数类指标出现小数 ⇒ 拒绝。

    直接 `int(3.7)` 会静默变成 3，报告里写 3.7、看板显示 3，两个数再也对不上。
    """
    payload["deterministic"]["performance"]["total_trades"] = 3.7
    with pytest.raises(DashboardError):
        read_view(payload)


# ── 4. 渲染：确定性 + 可打印 ──────────────────────────────────────────────
def test_render_text_twice_is_byte_identical(payload):
    first = render_text(read_view(payload))
    second = render_text(read_view(payload))
    assert first == second


def test_render_html_twice_is_byte_identical(payload):
    first = render_html(read_view(payload))
    second = render_html(read_view(payload))
    assert first == second


def test_render_text_shows_every_metric(payload):
    view = read_view(payload)
    text = render_text(view)
    for read in view.metrics:
        assert read.label in text, "看板没显示 %s" % read.key
        assert read.text in text, "看板显示的 %s 不是读数 %s" % (read.key, read.text)


def test_render_text_is_gbk_printable(payload):
    """控制台是 cp936：任何超出 GBK 的字符都会让打印直接抛 UnicodeEncodeError。

    这条把「能打印」变成机器可检查的事实，而不是等某次在 Windows 控制台上翻车。
    """
    render_text(read_view(payload)).encode("gbk")


def test_render_html_is_self_contained(payload):
    """单文件：不许外部脚本/样式/图片，也不许 fetch 任何东西（离线也能看）。"""
    html = render_html(read_view(payload))
    assert html.lstrip().lower().startswith("<!doctype html")
    for needle in ("http://", "https://", "<script", "<link", "<img"):
        assert needle not in html.lower(), "看板引用了外部资源：%s" % needle


def test_render_html_draws_the_curve(payload):
    """资金曲线必须真的画出来（svg 折线），不能只是一句话。"""
    html = render_html(read_view(payload))
    assert "<svg" in html and "<polyline" in html
    for point in read_view(payload).curve:
        assert "%.4f" % point.equity in html, point.timestamp


def test_render_html_shows_every_metric(payload):
    view = read_view(payload)
    html = render_html(view)
    for read in view.metrics:
        assert read.label in html
        assert read.text in html


# ── 5. 跨层：真报告也要能读（缝上必须有一条控制组） ───────────────────────
def test_real_backtest_report_roundtrip(real_payload):
    """引擎 → 报告 → 看板，这条缝接起来跑一遍。

    单层测试全绿、缝上一行都没跑过，是本仓库踩过三次的坑。
    """
    view = read_view(real_payload)
    report_perf = real_payload["deterministic"]["performance"]
    assert [r.value for r in view.metrics] == [report_perf[s.key] for s in METRIC_SPECS]
    assert len(view.curve) == len(real_payload["deterministic"]["equity_curve"])
    assert view.trade_count == len(real_payload["deterministic"]["trades"])
    assert view.trade_count >= 1, "样本必须非空，否则本条只是比了两份空报告"
    assert len({p.equity for p in view.curve}) > 1, "净值恒为常数 ⇒ 样本是空的"
    render_text(view)


def test_real_report_curve_agrees_with_two_performance_metrics(real_payload):
    """真报告上，两个字段与曲线**本就该相等**（`performance.py` 构造时就相等）。

    - `final_equity = equity[-1]`（performance.py:172）
    - `max_drawdown = max(drawdown_series(equity))`（performance.py:274）

    所以这里可以用等式断言 —— 它同时守住两件事：看板读曲线的方式没歪
    （尤其是 min/max 没写反），以及看板读的不是另一份数据的曲线。
    """
    view = read_view(real_payload)
    stats = curve_stats(view)
    report_perf = real_payload["deterministic"]["performance"]
    assert stats.last == report_perf["final_equity"]
    assert stats.max_drawdown == report_perf["max_drawdown"]


def test_real_report_renders_identically_after_json_roundtrip(real_payload):
    """报告从磁盘读回来再渲染，结果必须一样（看板不依赖 python 对象的内存形态）。

    `dump_report` 的字节由 `tools/verify_backtest_reproducibility.py` 守；这里守的是
    「读回来的 JSON 与内存里的 payload 给出同一个看板」。
    """
    from quanauto.engine import dump_report

    import json

    reloaded = json.loads(dump_report(real_payload))
    assert render_text(read_view(reloaded)) == render_text(read_view(real_payload))


# ── 6. CLI 接线：`dashboard` 子命令（跨层控制组） ──────────────────────────
# 这一节是后来补的，补的理由得写下来：**§1~§5 全绿的时候，`run_dashboard` 一行
# 都没跑过。** 库里能渲染、CLI 里能分派，两边各自都对，而缝上的那几行（读文件 /
# 选格式 / 落盘 / 分派）没有任何检查。本仓库已经因为「两组测试各自全绿、接起来跑
# 不通」吃过三次亏，味道完全一样。
#
# 所以下面每条都**真的从磁盘读一次文件**（不把内存里的 payload 直接喂给渲染函数），
# 并且断言 CLI 产出的字节与库函数**逐字节相同** —— 不是「差不多」，是等式。
def _write_report(tmp_path, payload):
    from quanauto.engine import dump_report

    path = tmp_path / "report.json"
    path.write_text(dump_report(payload), encoding="utf-8", newline="\n")
    return path


def _read_raw(path):
    """按原样读回（`newline=""` 关掉换行翻译），否则一个 CRLF 回归会被悄悄吃掉。"""
    with open(path, encoding="utf-8", newline="") as fp:
        return fp.read()


def test_cli_dashboard_writes_the_library_render_to_the_out_file(tmp_path, real_payload, capsys):
    """`--out` 落盘：内容必须**等于**库函数的渲染结果，而不是"看起来像一张看板"。"""
    from quanauto.cli import run_dashboard

    report = _write_report(tmp_path, real_payload)
    # 故意给一个还不存在的子目录：`--out` 那一支里有个 makedirs，别让它只被
    # 已存在的目录路径覆盖着（那样写错了也看不出来）。
    out = tmp_path / "nested" / "board.txt"
    args = build_parser().parse_args(
        ["dashboard", "--report", str(report), "--out", str(out)]
    )
    assert run_dashboard(args) == 0
    assert _read_raw(out) == render_text(read_view(real_payload))
    # 落盘时 stdout 只留一行纯 ASCII 摘要（与 backtest 一样的口径）。
    assert capsys.readouterr().out.startswith("dashboard: ")


def test_cli_dashboard_html_format_writes_the_html_render(tmp_path, real_payload):
    """`--format html` 选的是 html 渲染，不是同一个文本渲染换个文件名。"""
    from quanauto.cli import run_dashboard

    report = _write_report(tmp_path, real_payload)
    out = tmp_path / "board.html"
    args = build_parser().parse_args(
        ["dashboard", "--report", str(report), "--format", "html", "--out", str(out)]
    )
    assert run_dashboard(args) == 0
    body = _read_raw(out)
    assert body.lstrip().lower().startswith("<!doctype html")
    assert body == render_html(read_view(real_payload))


def test_cli_main_dispatches_the_dashboard_command_to_stdout(tmp_path, real_payload, capsys):
    """`main()` 里那条 `dashboard` 分派。删掉它不会让任何库函数测试变红 —— 它只会
    让 `quanauto dashboard` 打印帮助并退 2，而没人知道。
    """
    from quanauto.cli import main

    report = _write_report(tmp_path, real_payload)
    assert main(["dashboard", "--report", str(report)]) == 0
    assert capsys.readouterr().out == render_text(read_view(real_payload))


def test_cli_dashboard_missing_file_is_a_return_code_not_a_traceback(tmp_path, capsys):
    """读不到文件要退 1 并写一行 ERROR；抛 FileNotFoundError 出去会让调用方看到崩溃。"""
    from quanauto.cli import run_dashboard

    args = build_parser().parse_args(
        ["dashboard", "--report", str(tmp_path / "nope.json")]
    )
    assert run_dashboard(args) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_dashboard_invalid_json_is_a_return_code(tmp_path, capsys):
    from quanauto.cli import run_dashboard

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8", newline="\n")
    args = build_parser().parse_args(["dashboard", "--report", str(bad)])
    assert run_dashboard(args) == 1
    assert "ERROR" in capsys.readouterr().err


def test_cli_dashboard_refuses_a_report_it_cannot_read(tmp_path, capsys):
    """合法 JSON 但读不出 14 个指标 ⇒ 退 1，绝不打印半张空表（防空转）。"""
    from quanauto.cli import run_dashboard

    bad = tmp_path / "empty-perf.json"
    bad.write_text('{"deterministic": {"performance": {}}}', encoding="utf-8", newline="\n")
    args = build_parser().parse_args(["dashboard", "--report", str(bad)])
    assert run_dashboard(args) == 1
    assert "ERROR" in capsys.readouterr().err
