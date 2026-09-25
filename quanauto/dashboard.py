"""I4 回测绩效看板：把一份回测报告里的数摆到人眼前。

存在的理由
----------
一份回测结果（`BacktestResult`，或 `engine.dump_report` 写出的 JSON）里已经有 14 个
绩效指标和一条净值曲线（点数 = K 线根数）。它们**只是一堆字段**：想看一眼「夏普多少、
最大回撤多少、曲线长什么样」，得自己写脚本去翻 JSON。

看板要做的只有一件事：**把它们按固定口径摆出来**。

因此本模块的设计约束只有一条
----------------------------
    **只读数，不算数。**

具体是三个可检查的后果：

1. 本模块**不 import** `quanauto.performance`，也**不含任何指标公式** ——
   每个指标的值只从报告的 `deterministic.performance` 段取（`METRIC_SPECS` 是
   一个「取哪些字段、怎么显示」的清单，不是一份「怎么算」的定义）。
   I4 DoD 里那句「**不得另立一套指标定义**」在代码里的落地就是这一条，
   并由 `tools/verify_dashboard.py` 的 C6 静态检查盯着。
2. 取不到就**抛**（`DashboardError`），绝不退回 0。`0.0` 与「这个字段不存在」
   长得一样，退回 0 会得到一张「所有指标都是 0」的看板，而它和「真的算出来全是 0」
   无法区分。
3. 纯函数：同一个 `payload` 读两次得到相等的视图，同一份视图渲染两次得到
   **逐字节相同**的文本 —— 这是 I4 DoD 的产物判定（`同一份结果文件渲染两次，
   数值逐位一致`）在实现上的形式。

边界 —— 这个模块**不**证明什么
------------------------------
* 它**不保证数算得对**。报告里写 -12% 它就显示 -12%；算错了是 `performance.py`
  的事（那边由 `tools/verify_backtest_reproducibility.py` 与若干回归测试盯着）。
* 它**不产生新指标**，也不做「归一化 / 年化 / 去除异常值」这类再加工 ——
  哪怕某个数看上去不合理（例如 252 个交易日年化出来的 +300%），也照原样显示。
  看板的职责是**忠实**，不是**好看**。
* HTML 是**单文件**（内联样式 + 内联 SVG），不引任何外部资源，离线可看；
  它**不是** Web 前端（I4 DoD 注明「实盘实时看板不在本轮」，PRD 里的 Vue/React
  前端属于平台层，与这里无关）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import DashboardError

REPORT_SCHEMA = "quanauto.backtest-report/1"

# 量纲。只影响**显示**（乘 100 加 %、固定小数位），不影响取值。
UNIT_PERCENT = "percent"
UNIT_RATIO = "ratio"
UNIT_COUNT = "count"
UNIT_MONEY = "money"
UNIT_DAYS = "days"

GROUP_RETURN = "收益"
GROUP_RISK = "风险"
GROUP_TRADE = "成交"
GROUP_COST = "成本"

CURVE_SAMPLES = 60
# 只用 GBK 里存在的字符，且全部是 ASCII —— 这个控制台是 cp936，
# 一个 `↔` 就能让整份报告在打印时抛 UnicodeEncodeError（I1 翻过一次）。
SPARK_CHARS = " .:-=+*#%@"


@dataclass(frozen=True)
class MetricSpec:
    """一个指标**怎么取、怎么显示**。注意它没有「怎么算」的字段 —— 这是刻意的。"""

    key: str
    group: str
    label: str
    unit: str
    digits: int


# 14 条，与 `models.PerformanceMetrics` 的 dataclass 字段**双向相等**（少一个＝漏显示，
# 多一个＝发明了上游没有的指标）。`tools/verify_dashboard.py` 的 C2 与
# `tests/test_dashboard.py::test_specs_match_performance_metrics_exactly` 都盯着它。
METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec("total_return", GROUP_RETURN, "累计收益", UNIT_PERCENT, 4),
    MetricSpec("annual_return", GROUP_RETURN, "年化收益", UNIT_PERCENT, 4),
    MetricSpec("final_equity", GROUP_RETURN, "期末净值", UNIT_MONEY, 2),
    MetricSpec("max_drawdown", GROUP_RISK, "最大回撤", UNIT_PERCENT, 4),
    MetricSpec("sharpe_ratio", GROUP_RISK, "夏普比率", UNIT_RATIO, 4),
    MetricSpec("sortino_ratio", GROUP_RISK, "索提诺比率", UNIT_RATIO, 4),
    MetricSpec("calmar_ratio", GROUP_RISK, "卡玛比率", UNIT_RATIO, 4),
    MetricSpec("win_rate", GROUP_TRADE, "胜率", UNIT_PERCENT, 4),
    MetricSpec("profit_factor", GROUP_TRADE, "盈亏比（总额）", UNIT_RATIO, 4),
    MetricSpec("profit_loss_ratio", GROUP_TRADE, "盈亏比（均额）", UNIT_RATIO, 4),
    MetricSpec("max_consecutive_losses", GROUP_TRADE, "最大连续亏损", UNIT_COUNT, 0),
    MetricSpec("avg_hold_period", GROUP_TRADE, "平均持仓天数", UNIT_DAYS, 2),
    MetricSpec("total_trades", GROUP_TRADE, "成交笔数", UNIT_COUNT, 0),
    MetricSpec("total_commission", GROUP_COST, "累计佣金", UNIT_MONEY, 2),
)

SPEC_BY_KEY: Dict[str, MetricSpec] = {spec.key: spec for spec in METRIC_SPECS}

# 报告结构里的三个计数。它们是**结构计数**，不是绩效指标：不进 METRIC_SPECS，
# 也不参与「字段清单双向对齐」，但同样来自报告、同样不重算。
STRUCTURE_COUNTS = ("equity_curve", "trades", "orders")


@dataclass(frozen=True)
class MetricRead:
    """一个指标的一次读数。`value` 是报告里的原值，`text` 是它的显示形式。"""

    key: str
    group: str
    label: str
    value: Any
    text: str


@dataclass(frozen=True)
class CurvePoint:
    """净值曲线上的一个点 —— 逐字段照抄报告的 `EquityPoint`，不做任何插值/平滑。"""

    timestamp: str
    equity: float
    drawdown: float


@dataclass(frozen=True)
class DashboardView:
    """看板要显示的全部内容。**没有派生指标字段** —— 只有报告里已有的东西。"""

    strategy_id: str
    status: str
    data_version: str
    strategy_version: str
    seed: Optional[int]
    symbol: Optional[str]
    window: Optional[Tuple[str, str]]
    metrics: Tuple[MetricRead, ...]
    curve: Tuple[CurvePoint, ...]
    counts: Dict[str, Optional[int]] = field(default_factory=dict)

    @property
    def trade_count(self) -> Optional[int]:
        return self.counts.get("trades")

    @property
    def order_count(self) -> Optional[int]:
        return self.counts.get("orders")

    @property
    def curve_points(self) -> int:
        return len(self.curve)

    def metric(self, key: str) -> MetricRead:
        for read in self.metrics:
            if read.key == key:
                return read
        raise KeyError(key)


# ── 显示层（纯函数，无副作用） ────────────────────────────────────────────
def format_metric(spec: MetricSpec, value: Any) -> str:
    """把报告里的原值变成显示文本。**只做量纲换算与定点小数，不做四舍五入以外的加工。**"""
    number = _require_number(spec.key, value)
    if spec.unit == UNIT_PERCENT:
        return "%.*f%%" % (spec.digits, number * 100.0)
    if spec.unit == UNIT_COUNT:
        if float(number) != int(number):
            raise DashboardError(
                "%s 是计数类指标，报告里却是 %r —— 显示时会被静默截断成整数，"
                "报告与看板从此对不上" % (spec.key, number)
            )
        return "%d" % int(number)
    if spec.unit in (UNIT_MONEY, UNIT_DAYS):
        return "%.*f" % (spec.digits, number)
    return "%.*f" % (spec.digits, number)


def _require_number(key: str, value: Any) -> float:
    """只接受有限实数。`bool` 不算数（`True` 是 `int` 的子类，退化成 1 会非常隐蔽）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DashboardError(
            "%s 不是数字（%r）—— 看板只显示报告里的数，取不到就报错，不退回 0"
            % (key, value)
        )
    number = float(value)
    if not math.isfinite(number):
        raise DashboardError(
            "%s 不是有限实数（%r）—— 报告里有 NaN/Inf 说明上游算出了非数，"
            "看板不该把它当正常值显示" % (key, value)
        )
    return number


# ── 读数 ─────────────────────────────────────────────────────────────────
def _require_mapping(value: Any, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise DashboardError("%s 不是对象（%r），读不到任何字段" % (what, type(value).__name__))
    return value


def _performance_section(payload: Any) -> Mapping:
    report = _require_mapping(payload, "报告")
    deterministic = report.get("deterministic")
    if not isinstance(deterministic, Mapping):
        raise DashboardError(
            "报告里没有 deterministic 段 —— 看板只认 `engine.report_payload` 这种形状，"
            "读不到就停在这里（不去猜别的键名）"
        )
    performance = deterministic.get("performance")
    if not isinstance(performance, Mapping) or not performance:
        # 这一条是防空转守卫：{} 会让下面 14 个指标全部读不到，
        # 若不拦住，看板会安静地显示一张空表。
        raise DashboardError(
            "报告里的 performance 段是空的或不存在（%r）—— 14 个指标一个都读不到，"
            "继续渲染只会得到一张空表" % (performance,)
        )
    return performance


def _read_metrics(performance: Mapping) -> Tuple[MetricRead, ...]:
    missing = [spec.key for spec in METRIC_SPECS if spec.key not in performance]
    if missing:
        raise DashboardError(
            "报告 performance 段缺 %d 个指标：%s —— 看板不替上游补默认值"
            % (len(missing), ", ".join(missing))
        )
    reads: List[MetricRead] = []
    for spec in METRIC_SPECS:
        value = performance[spec.key]
        reads.append(
            MetricRead(
                key=spec.key,
                group=spec.group,
                label=spec.label,
                value=value,
                text=format_metric(spec, value),
            )
        )
    return tuple(reads)


def _read_curve(deterministic: Mapping) -> Tuple[CurvePoint, ...]:
    raw = deterministic.get("equity_curve")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 2:
        raise DashboardError(
            "报告的 equity_curve 不是长度 >= 2 的序列（%r）—— 一个点画不出曲线，"
            "0 个点画出来的「曲线」与「没有数据」无法区分" % (len(raw) if isinstance(raw, Sequence) else raw,)
        )
    points: List[CurvePoint] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise DashboardError("equity_curve 第 %d 个点不是对象" % index)
        for key in ("timestamp", "equity", "drawdown"):
            if key not in item:
                raise DashboardError(
                    "equity_curve 第 %d 个点缺 %s —— 缺字段时退回 0 会让曲线上的"
                    "一段「贴地」，与真的亏到底长得一样" % (index, key)
                )
        points.append(
            CurvePoint(
                timestamp=str(item["timestamp"]),
                equity=_require_number("equity", item["equity"]),
                drawdown=_require_number("drawdown", item["drawdown"]),
            )
        )
    return tuple(points)


def _read_counts(deterministic: Mapping) -> Dict[str, Optional[int]]:
    """三个结构计数。取不到就是 None（**不是 0**）—— 显示成 `-`）。

    它们不是绩效指标，所以缺了不判 FAIL；但也绝不退回 0，免得看板把
    「报告里没有 trades 段」显示成「0 笔成交」。
    """
    counts: Dict[str, Optional[int]] = {}
    for key in STRUCTURE_COUNTS:
        value = deterministic.get(key)
        counts[key] = len(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else None
    return counts


def read_view(payload: Any) -> DashboardView:
    """从一份报告里读出看板要显示的一切。**只读，不重算，不改动输入。**"""
    performance = _performance_section(payload)
    report = payload
    deterministic = report["deterministic"]
    inputs = report.get("inputs")
    summary = report.get("summary")
    seed = inputs.get("seed") if isinstance(inputs, Mapping) else None
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        seed = None
    symbol = summary.get("symbol") if isinstance(summary, Mapping) else None
    window = summary.get("window") if isinstance(summary, Mapping) else None
    window_tuple: Optional[Tuple[str, str]] = None
    if isinstance(window, Sequence) and not isinstance(window, (str, bytes)) and len(window) == 2:
        window_tuple = (str(window[0]), str(window[1]))
    return DashboardView(
        strategy_id=str(deterministic.get("strategy_id", "")),
        status=str(deterministic.get("status", "")),
        data_version=str(deterministic.get("data_version", "")),
        strategy_version=str(deterministic.get("strategy_version", "")),
        seed=seed,
        symbol=str(symbol) if symbol is not None else None,
        window=window_tuple,
        metrics=_read_metrics(performance),
        curve=_read_curve(deterministic),
        counts=_read_counts(deterministic),
    )


# ── 文本渲染 ─────────────────────────────────────────────────────────────
def _cell(value: Optional[Any]) -> str:
    return "-" if value is None else str(value)


def sparkline(values: Sequence[float]) -> str:
    """等距抽样 + 分档字符。纯 ASCII，确定性（同输入同输出）。"""
    if not values:
        return ""
    low = min(values)
    high = max(values)
    span = high - low
    step = max(1, len(values) // CURVE_SAMPLES)
    out = []
    for index in range(0, len(values), step):
        value = values[index]
        level = 0 if span <= 0 else int(round((value - low) / span * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[level])
    return "".join(out)


@dataclass(frozen=True)
class CurveStats:
    """资金曲线的四个汇总读数。**都是对报告里已有字段的取值**，不是新指标。

    `max_drawdown` 的取法是 `max` 而不是 `min`：报告里 `EquityPoint.drawdown` 是
    **正数百分比**（`performance.drawdown_series` 的约定，0.05 表示回撤 5%），
    所以「最大回撤」= 曲线上最大的那个值。写成 `min` 会永远显示 0.000000，
    而那看起来就像一个从来没回过撤的策略。
    """

    points: int
    low: float
    high: float
    last: float
    max_drawdown: float


def curve_stats(view: DashboardView) -> CurveStats:
    equities = [point.equity for point in view.curve]
    return CurveStats(
        points=len(view.curve),
        low=min(equities),
        high=max(equities),
        last=equities[-1],
        max_drawdown=max(point.drawdown for point in view.curve),
    )


def group_reads(view: DashboardView) -> List[Tuple[str, List[MetricRead]]]:
    """按 `METRIC_SPECS` 的顺序分组（不重新排序 —— 顺序本身是显示口径的一部分）。"""
    groups: List[Tuple[str, List[MetricRead]]] = []
    for read in view.metrics:
        if not groups or groups[-1][0] != read.group:
            groups.append((read.group, []))
        groups[-1][1].append(read)
    return groups


def render_text(view: DashboardView) -> str:
    """纯文本看板。只输出 GBK 里存在的字符，Windows 控制台可直接打印。"""
    line = "=" * 62
    thin = "-" * 62
    out: List[str] = [line, "回测绩效看板  %s" % REPORT_SCHEMA, line]
    out.append("  %-14s %s" % ("策略", _cell(view.strategy_id) or "-"))
    out.append("  %-14s %s" % ("状态", _cell(view.status) or "-"))
    out.append("  %-14s %s" % ("数据版本", _cell(view.data_version) or "-"))
    out.append("  %-14s %s" % ("策略版本", _cell(view.strategy_version) or "-"))
    out.append("  %-14s %s" % ("标的", _cell(view.symbol)))
    out.append(
        "  %-14s %s"
        % ("区间", "%s .. %s" % view.window if view.window else "-")
    )
    out.append("  %-14s %s" % ("随机种子", _cell(view.seed)))
    out.append("  %-14s %s" % ("净值点数", _cell(view.counts.get("equity_curve"))))
    out.append("  %-14s %s" % ("成交笔数", _cell(view.counts.get("trades"))))
    out.append("  %-14s %s" % ("订单笔数", _cell(view.counts.get("orders"))))
    out.append(thin)
    for group, reads in group_reads(view):
        out.append("【%s】" % group)
        for read in reads:
            out.append("  %-20s %s" % (read.label, read.text))
        out.append("")
    equities = [point.equity for point in view.curve]
    stats = curve_stats(view)
    out.append("资金曲线（%d 点，按等距抽样画 %d 列）" % (stats.points, CURVE_SAMPLES))
    out.append(
        "  最低 %s   最高 %s   期末 %s"
        % ("%.4f" % stats.low, "%.4f" % stats.high, "%.4f" % stats.last)
    )
    out.append("  曲线最大回撤 %s" % ("%.6f" % stats.max_drawdown))
    out.append("  %s" % sparkline(equities))
    out.append(
        "  首点 %s（%.4f）  末点 %s（%.4f）"
        % (view.curve[0].timestamp, equities[0], view.curve[-1].timestamp, equities[-1])
    )
    out.append(line)
    return "\n".join(out) + "\n"


# ── HTML 渲染（单文件） ───────────────────────────────────────────────────
_HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>回测绩效看板</title>
<style>
body { font-family: Consolas, Menlo, monospace; margin: 24px; color: #1f1f1f; }
h1 { font-size: 18px; margin: 0 0 12px 0; }
table { border-collapse: collapse; margin: 8px 0 16px 0; }
th, td { border: 1px solid #cccccc; padding: 2px 10px; font-size: 13px; }
th { background: #f2f2f2; text-align: left; }
td.num { text-align: right; }
dl { margin: 0 0 16px 0; font-size: 13px; }
dt { display: inline-block; width: 110px; color: #666666; }
dd { display: inline; margin: 0 24px 0 0; }
svg { border: 1px solid #cccccc; background: #fafafa; }
footer { color: #888888; font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
"""


def render_html(view: DashboardView) -> str:
    """单文件 HTML 看板（内联样式 + 内联 SVG 折线）。不引任何外部资源。"""
    out: List[str] = [_HTML_HEAD, "<h1>回测绩效看板</h1>\n"]
    out.append("<dl>")
    for label, value in (
        ("策略", _cell(view.strategy_id) or "-"),
        ("状态", _cell(view.status) or "-"),
        ("数据版本", _cell(view.data_version) or "-"),
        ("策略版本", _cell(view.strategy_version) or "-"),
        ("标的", _cell(view.symbol)),
        ("随机种子", _cell(view.seed)),
        ("净值点数", _cell(view.counts.get("equity_curve"))),
        ("成交笔数", _cell(view.counts.get("trades"))),
        ("订单笔数", _cell(view.counts.get("orders"))),
    ):
        out.append("<dt>%s</dt><dd>%s</dd>" % (label, _escape(value)))
    if view.window:
        out.append("<dt>区间</dt><dd>%s .. %s</dd>" % (_escape(view.window[0]), _escape(view.window[1])))
    out.append("</dl>\n")
    out.append("<table>")
    out.append("<tr><th>类别</th><th>指标</th><th>数值</th></tr>")
    for group, reads in group_reads(view):
        for index, read in enumerate(reads):
            out.append(
                "<tr><td>%s</td><td>%s</td><td class=\"num\">%s</td></tr>"
                % (_escape(group if index == 0 else ""), _escape(read.label), _escape(read.text))
            )
    out.append("</table>\n")
    out.append(_svg_curve(view))
    out.append(
        "<footer>数据直接取自报告 deterministic.performance 与 equity_curve，看板不重算任何指标。</footer>\n"
    )
    out.append("</body>\n</html>\n")
    return "".join(out)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_curve(view: DashboardView) -> str:
    width, height, pad = 900, 260, 40
    equities = [point.equity for point in view.curve]
    low, high = min(equities), max(equities)
    span = high - low
    inner_w = float(width - 2 * pad)
    inner_h = float(height - 2 * pad)
    coords: List[str] = []
    for index, value in enumerate(equities):
        x = pad + (0.0 if len(equities) == 1 else inner_w * index / (len(equities) - 1))
        ratio = 0.5 if span <= 0 else (value - low) / span
        y = pad + inner_h * (1.0 - ratio)
        coords.append("%.4f,%.4f" % (x, y))
    parts = [
        '<svg width="%d" height="%d" viewBox="0 0 %d %d">' % (width, height, width, height),
        '<polyline fill="none" stroke="#1f77b4" stroke-width="1.5" points="%s"/>' % " ".join(coords),
    ]
    for index, value in enumerate(equities):
        x = pad + (0.0 if len(equities) == 1 else inner_w * index / (len(equities) - 1))
        ratio = 0.5 if span <= 0 else (value - low) / span
        y = pad + inner_h * (1.0 - ratio)
        # 净值也写进 HTML（`data-equity`）—— 让「每个点都在页面上」成为机器可核对的事实，
        # 而不是只能靠人眼看图里有没有少一段。
        parts.append(
            '<circle cx="%.4f" cy="%.4f" r="1.5" fill="#1f77b4" data-equity="%.4f"/>'
            % (x, y, value)
        )
    parts.append('</svg>\n')
    parts.append(
        '<p>净值 %d 点：最低 %.4f，最高 %.4f，期末 %.4f</p>\n'
        % (len(equities), low, high, equities[-1])
    )
    return "".join(parts)
