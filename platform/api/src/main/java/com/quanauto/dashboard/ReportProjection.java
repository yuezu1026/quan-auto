package com.quanauto.dashboard;

import com.fasterxml.jackson.databind.JsonNode;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;

/**
 * 把一份报告 JSON 投影成 {@link ReportView}。<b>只读数，不算数。</b>
 *
 * <p>本类是本层唯一有业务逻辑的地方，因此约束也全部压在这里：
 *
 * <ol>
 *   <li><b>零统计。</b>不 import 任何统计/数值库，不含任何指标公式。每个指标的值只从报告的
 *       {@code deterministic.performance} 段取。本类里唯一的算术是
 *       <b>量纲换算</b>（{@code percent} 乘 100）与<b>定点小数</b>，两者都只影响显示，
 *       不影响取值。这是 Python 侧 {@code quanauto/dashboard.py} 的同一味道 ——
 *       那边由 {@code tools/verify_dashboard.py} 的 C6 静态检查盯着，这边没有，
 *       见 {@link MetricSpec#METRIC_SPECS} 的警告。</li>
 *   <li><b>取不到就抛，绝不退回 0。</b>{@code 0.0} 与「这个字段不存在」长得一样：
 *       退回 0 得到的是一张「所有指标都是 0」的看板，而它和「真的算出来全是 0」无法区分。</li>
 *   <li><b>纯函数。</b>同一个 payload 读两次得到相等的视图；不修改输入。</li>
 * </ol>
 *
 * <p><b>本层不证明什么</b>：它<b>不保证数算得对</b>。报告里写 -12% 它就显示 -12%；
 * 算错了是研究层 {@code performance.py} 的事。看板的职责是<b>忠实</b>，不是<b>好看</b>。
 */
public final class ReportProjection {

    private ReportProjection() {
    }

    /** 报告里 {@code equity_curve} 每个点必须有的三个字段。 */
    private static final List<String> CURVE_FIELDS = List.of("timestamp", "equity", "drawdown");

    public static ReportView read(String id, JsonNode payload) {
        JsonNode report = requireObject(payload, "报告");
        JsonNode deterministic = report.get("deterministic");
        if (deterministic == null || !deterministic.isObject()) {
            throw new DashboardException(
                    "报告里没有 deterministic 段 —— 看板只认 `engine.report_payload` 这种形状，"
                            + "读不到就停在这里（不去猜别的键名）");
        }

        JsonNode performance = deterministic.get("performance");
        if (performance == null || !performance.isObject() || performance.isEmpty()) {
            // 这一条是防空转守卫：{} 会让下面 14 个指标全部读不到，
            // 若不拦住，看板会安静地显示一张空表。
            throw new DashboardException(
                    "报告里的 performance 段是空的或不存在（" + brief(performance) + "）—— "
                            + "14 个指标一个都读不到，继续渲染只会得到一张空表");
        }

        List<String> missing = new ArrayList<>();
        for (MetricSpec spec : MetricSpec.METRIC_SPECS) {
            if (!performance.has(spec.key())) {
                missing.add(spec.key());
            }
        }
        if (!missing.isEmpty()) {
            throw new DashboardException(String.format(
                    "报告 performance 段缺 %d 个指标：%s —— 看板不替上游补默认值",
                    missing.size(), String.join(", ", missing)));
        }

        return new ReportView(
                id,
                textOr(report.get("schema")),
                textOr(deterministic.get("strategy_id")),
                textOr(deterministic.get("strategy_version")),
                textOr(deterministic.get("data_version")),
                textOr(deterministic.get("status")),
                seedOf(report),
                symbolOf(report),
                windowOf(report),
                new ReportView.CountsRead(
                        arraySize(deterministic.get("equity_curve")),
                        arraySize(deterministic.get("trades")),
                        arraySize(deterministic.get("orders"))),
                readMetrics(performance),
                readCurve(deterministic));
    }

    // ── 读数 ─────────────────────────────────────────────────────────────────

    private static List<ReportView.MetricRead> readMetrics(JsonNode performance) {
        List<ReportView.MetricRead> reads = new ArrayList<>(MetricSpec.METRIC_SPECS.size());
        for (MetricSpec spec : MetricSpec.METRIC_SPECS) {
            JsonNode raw = performance.get(spec.key());
            reads.add(new ReportView.MetricRead(
                    spec.key(),
                    spec.group(),
                    spec.label(),
                    spec.unit(),
                    spec.digits(),
                    requireNumber(spec.key(), raw),
                    formatMetric(spec, raw)));
        }
        return reads;
    }

    private static List<ReportView.CurvePointRead> readCurve(JsonNode deterministic) {
        JsonNode raw = deterministic.get("equity_curve");
        if (raw == null || !raw.isArray() || raw.size() < 2) {
            String actual = raw == null
                    ? "缺失"
                    : (raw.isArray() ? raw.size() + " 个点" : brief(raw));
            throw new DashboardException(
                    "报告的 equity_curve 不是长度 >= 2 的数组（" + actual + "）—— "
                            + "一个点画不出曲线，0 个点画出来的「曲线」与「没有数据」无法区分");
        }
        List<ReportView.CurvePointRead> points = new ArrayList<>(raw.size());
        for (int index = 0; index < raw.size(); index++) {
            JsonNode item = raw.get(index);
            if (item == null || !item.isObject()) {
                throw new DashboardException("equity_curve 第 " + index + " 个点不是对象");
            }
            for (String key : CURVE_FIELDS) {
                JsonNode value = item.get(key);
                if (value == null || value.isNull()) {
                    throw new DashboardException(
                            "equity_curve 第 " + index + " 个点缺 " + key + " —— 缺字段时退回 0 "
                                    + "会让曲线上一段「贴地」，与真的亏到底长得一样");
                }
            }
            points.add(new ReportView.CurvePointRead(
                    item.get("timestamp").asText(),
                    requireNumber("equity", item.get("equity")),
                    requireNumber("drawdown", item.get("drawdown"))));
        }
        return points;
    }

    // ── 显示层 ───────────────────────────────────────────────────────────────

    /**
     * 把报告里的原值变成显示文本。<b>只做量纲换算与定点小数</b>，不做任何再加工。
     *
     * <p>与 Python 侧 {@code dashboard.format_metric} 同口径：{@code percent} 乘 100 加 {@code %}
     * ；{@code count} 必须是整数（否则抛，因为格式化会静默截断）；其余按 {@code digits} 定点。
     */
    static String formatMetric(MetricSpec spec, JsonNode raw) {
        double number = requireNumber(spec.key(), raw);
        if (MetricSpec.UNIT_PERCENT.equals(spec.unit())) {
            return fixed(number * 100.0, spec.digits()) + "%";
        }
        if (MetricSpec.UNIT_COUNT.equals(spec.unit())) {
            if (number != Math.rint(number)) {
                throw new DashboardException(String.format(
                        "%s 是计数类指标，报告里却是 %s —— 显示时会被静默截断成整数，"
                                + "报告与看板从此对不上", spec.key(), brief(raw)));
            }
            // 用 BigDecimal 而不是 (long)：计数类指标将来可能是个大数，
            // (long) 溢出会静默给出一个错的整数，而那是看板最不该犯的错。
            // 这里用 `new BigDecimal(double)`（**精确二进制值**）而不是 `BigDecimal.valueOf`：
            // 后者走最短往返表示，对 >= 2^53 的非可表示整数**不等于**这个 double 本身
            // （`1e23` 的精确值是 99999999999999991611392，Python 的 `"%d" % int(1e23)`
            // 打印的就是它），于是两侧会在这种值上打印出不同的整数。
            return new BigDecimal(number).toBigInteger().toString();
        }
        return fixed(number, spec.digits());
    }

    /**
     * 定点小数。四舍五入的<b>平局规则用 HALF_EVEN</b>，与 Python 的 {@code "%.4f"} 一致
     * （{@code String.format} 的默认是 HALF_UP，会与 Python 在平局处分道扬镳）。
     *
     * <p>⚠️ 这里是「显示规则在两侧各有一份实现」的<b>那一侧</b>（另一侧是 Python 的
     * {@code dashboard.format_metric}）。2026-10-01 实测过分歧面：73929 组样本里旧版
     * 打印出 <b>6887 组（9.3%）</b>与 Python 不同的字符串，两类成因各修掉一处 ——
     *
     * <ol>
     *   <li><b>丢负号</b>（4161 组）：{@code BigDecimal} 的零<b>没有符号</b>，
     *       {@code -0.005} 在 {@code digits=2} 上舍入成零之后减号也没了（打印 {@code 0.00}），
     *       而 Python 打印 {@code -0.01} / {@code -0.0000}。旧版只补了「原值恰好是
     *       {@code -0.0}」那一种，补不到「负的、但舍入后落到零上」那一大片。</li>
     *   <li><b>平局落错边</b>（2726 组）：{@code BigDecimal.valueOf(double)} 走的是
     *       {@code Double.toString} 的<b>最短往返表示</b>，它不是这个 double 的精确值。
     *       当那个最短表示恰好落在十进制平局点上（{@code -29.95}、{@code 2.675}、
     *       {@code 182.745}）时，两侧就分道扬镳：Python 按精确值得到 {@code -29.9}，
     *       这里按那个十进制串做 HALF_EVEN 得到 {@code -30.0}。</li>
     * </ol>
     *
     * <p>常驻判据：{@code platform/text-parity-cases.json} 是 Python 侧现算出来的夹具，
     * 由 {@code tools/verify_platform_text_parity.py} 核它新不新鲜，并由
     * {@code FormatParityTest} 逐条喂进本方法对拍（{@code platform-runtime} 门禁真跑
     * {@code mvn test}）。改本方法会让夹具变红 —— 那是判据在工作，正确的反应是重新录一遍
     * 夹具并**让两侧都跑一遍**，不是放宽判据。
     */
    static String fixed(double value, int digits) {
        // 用 `new BigDecimal(double)`（**精确二进制值**）而不是 `BigDecimal.valueOf`：后者
        // 的最短往返表示会在十进制平局点上把舍入推到另一侧（见上面的 ②）。
        BigDecimal scaled = new BigDecimal(value).setScale(digits, RoundingMode.HALF_EVEN);
        String text = scaled.toPlainString();
        // 补号按**原值的符号**判，而不是按「原值是否恰好是 -0.0」：要盖住的是
        // 「负的、但舍入后落到零上」那一类（`-0.005` @2 ⇒ `-0.01`；`-1e-9` @2 ⇒ `-0.00`）。
        if (text.charAt(0) != '-' && scaled.signum() == 0 && isNegative(value)) {
            return "-" + text;
        }
        return text;
    }

    /**
     * 原值是不是负的（<b>含负零</b>）：Python 的 {@code "%.1f" % -0.0} 打印 {@code -0.0}，
     * 而 {@code BigDecimal} 那边负零只是零 ⇒ 光看 {@code setScale} 的结果分不出来。
     */
    private static boolean isNegative(double value) {
        return value < 0.0 || (value == 0.0 && Double.doubleToRawLongBits(value) != 0L);
    }

    /** 只接受有限实数。布尔不算数（JSON 的 {@code true} 不是数字节点，天然被挡在外面）。 */
    static double requireNumber(String key, JsonNode raw) {
        if (raw == null || raw.isNull() || !raw.isNumber()) {
            throw new DashboardException(String.format(
                    "%s 不是数字（%s）—— 看板只显示报告里的数，取不到就报错，不退回 0",
                    key, brief(raw)));
        }
        double number = raw.doubleValue();
        if (!Double.isFinite(number)) {
            throw new DashboardException(String.format(
                    "%s 不是有限实数（%s）—— 报告里有 NaN/Inf 说明上游算出了非数，"
                            + "看板不该把它当正常值显示", key, brief(raw)));
        }
        return number;
    }

    // ── 小工具 ───────────────────────────────────────────────────────────────

    private static JsonNode requireObject(JsonNode value, String what) {
        if (value == null || !value.isObject()) {
            throw new DashboardException(
                    what + "不是对象（" + (value == null ? "null" : brief(value)) + "），读不到任何字段");
        }
        return value;
    }

    /** 取文本；缺失或 {@code null} 返回空串（与 Python 侧 {@code str(deterministic.get(k, ""))} 一致）。 */
    private static String textOr(JsonNode value) {
        return value == null || value.isNull() ? "" : value.asText();
    }

    private static Integer seedOf(JsonNode report) {
        JsonNode inputs = report.get("inputs");
        if (inputs == null || !inputs.isObject()) {
            return null;
        }
        JsonNode seed = inputs.get("seed");
        // isIntegralNumber() 对浮点/布尔/文本都是 false —— 与 Python 侧
        // 「不是 int（且不是 bool）就当取不到」同口径。
        return seed != null && seed.isIntegralNumber() ? seed.intValue() : null;
    }

    private static String symbolOf(JsonNode report) {
        JsonNode summary = report.get("summary");
        if (summary == null || !summary.isObject()) {
            return null;
        }
        JsonNode symbol = summary.get("symbol");
        return symbol == null || symbol.isNull() ? null : symbol.asText();
    }

    private static List<String> windowOf(JsonNode report) {
        JsonNode summary = report.get("summary");
        if (summary == null || !summary.isObject()) {
            return null;
        }
        JsonNode window = summary.get("window");
        if (window == null || !window.isArray() || window.size() != 2) {
            return null;
        }
        return List.of(window.get(0).asText(), window.get(1).asText());
    }

    /** 结构计数：是数组就取长度，否则 {@code null}（**不是 0**）。 */
    private static Integer arraySize(JsonNode value) {
        return value != null && value.isArray() ? value.size() : null;
    }

    /** 报错信息里引用原值时的截断包装：不能让一个 200 字符的坏值把整条信息冲散。 */
    private static String brief(JsonNode value) {
        if (value == null || value.isNull()) {
            return "缺失";
        }
        String text = value.toString();
        return text.length() <= 120 ? text : text.substring(0, 117) + "...";
    }
}
