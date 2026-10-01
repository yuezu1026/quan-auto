package com.quanauto.dashboard;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * 投影层的用例。<b>用仓库里真实的报告文件当样本</b>（{@code .rounds/i1/report-seed7-a.json}）——
 * 自己造一个「好看的」样本只能证明它读得动我自己写的东西。
 *
 * <p>⚠️ 订正（2026-09-30）：原来这里写「这一层<b>不在任何门禁里，也不在 CI 里</b>」，<b>已过期</b>
 * —— 现在 {@code platform-runtime} 门禁会在一次性沙箱里真跑 {@code mvn test}（也就是这个类），
 * 而它接在 {@code tools/run_all_gates.py} 的统一入口里、{@code ci.yml} 会跑那条命令
 * ⇒ 下面这些用例真的有人跑，把它们弄红会在 CI 上被拦住。
 *
 * <p>仍然零覆盖的是：<b>页面本身</b>（渲染 / 布局 / 视觉）。<b>两侧展示文本是否一致</b>
 * 自 2026-10-01 起<b>已经不在这里的账上</b> —— 它由 {@code platform-text-parity} 门禁盯
 * （{@code platform/text-parity-cases.json} + 本包下的 {@link FormatParityTest}，
 * 后者跑在本类同一个 {@code mvn test} 里）。下面这几条<b>不是</b>两侧一致性的判据：
 * 它们比的是「这一侧读得动真报告」，样本是手挑的。
 * 仍然只有手动脚本 {@code platform/check_text_parity.py} 的是「<b>活着的服务端</b>下发了什么」
 * （需要一个活着的 JVM ⇒ 不能当门禁）。分工见 {@code docs/智能量化交易平台.md} 附录C §C.8 / §C.9。
 */
class ReportProjectionTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** 真实报告：14 个指标一个不少，且每个都带着服务端算好的显示文本。 */
    @Test
    void readsRealReportFromRounds() {
        ReportCatalog catalog = new ReportCatalog(".rounds/i1");
        ReportView view = ReportProjection.read("report-seed7-a", catalog.load("report-seed7-a"));

        assertEquals("quanauto.backtest-report/1", view.schema());
        assertEquals("000001.SZ", view.symbol());
        assertEquals(List.of("2024-01-02T00:00:00", "2024-03-25T00:00:00"), view.window());
        assertEquals(MetricSpec.METRIC_SPECS.size(), view.metrics().size());
        assertEquals(60, view.counts().equityCurve());
        assertEquals(3, view.counts().trades());
        assertEquals(3, view.counts().orders());

        // 顺序必须与 dashboard.py 的 METRIC_SPECS 一致：前端按顺序渲染，顺序变了表格就变了。
        assertEquals("total_return", view.metrics().get(0).key());
        assertEquals("total_commission", view.metrics().get(view.metrics().size() - 1).key());

        // 抽一个具体值到显示文本，钉住「量纲换算 + 定点」这一整条链。
        // 注意小数位是 **4**（与 dashboard.py 的 METRIC_SPECS 一致）—— 第一版这里写 2 被我写成绿的。
        ReportView.MetricRead totalReturn = view.metrics().get(0);
        assertEquals(-0.032043452542899906, totalReturn.value(), 1e-15);
        assertEquals("percent", totalReturn.unit());
        assertEquals(4, totalReturn.digits());
        assertEquals("-3.2043%", totalReturn.text());

        for (ReportView.MetricRead metric : view.metrics()) {
            assertNotNull(metric.label(), metric.key() + " 没有标签，前端会显示成空单元格");
            assertTrue(!metric.text().isBlank(), metric.key() + " 的显示文本是空的");
        }
        assertEquals(60, view.curve().size());
        assertNotNull(view.curve().get(0).timestamp());
    }

    /** 防空转守卫：{@code performance} 是空对象时必须抛，否则会安静地渲染成一张空表。 */
    @Test
    void refusesEmptyPerformanceSection() throws Exception {
        JsonNode payload = MAPPER.readTree("""
                {"schema":"quanauto.backtest-report/1",
                 "deterministic":{"performance":{},
                                  "equity_curve":[{"timestamp":"a","equity":1,"drawdown":0},
                                                  {"timestamp":"b","equity":2,"drawdown":0}]}}
                """);
        DashboardException exc = assertThrows(DashboardException.class,
                () -> ReportProjection.read("empty", payload));
        assertTrue(exc.getMessage().contains("performance"), exc.getMessage());
    }

    /** 缺字段：报错信息里要点名缺了哪几个（否则拿到报错也不知道去补什么）。 */
    @Test
    void refusesMissingMetrics() throws Exception {
        JsonNode payload = MAPPER.readTree("""
                {"deterministic":{"performance":{"total_return":0.01},
                                  "equity_curve":[{"timestamp":"a","equity":1,"drawdown":0},
                                                  {"timestamp":"b","equity":2,"drawdown":0}]}}
                """);
        DashboardException exc = assertThrows(DashboardException.class,
                () -> ReportProjection.read("partial", payload));
        assertTrue(exc.getMessage().contains("annual_return"), exc.getMessage());
        assertTrue(exc.getMessage().contains("13"), exc.getMessage());
    }

    /** 曲线点缺字段：不能退回 0（会画出一段「贴地」，与真的亏到底长得一样）。 */
    @Test
    void refusesCurvePointWithoutEquity() throws Exception {
        String metrics = fullMetricsJson();
        JsonNode payload = MAPPER.readTree("""
                {"deterministic":{"performance":%s,
                                  "equity_curve":[{"timestamp":"a","drawdown":0},
                                                  {"timestamp":"b","equity":2,"drawdown":0}]}}
                """.formatted(metrics));
        DashboardException exc = assertThrows(DashboardException.class,
                () -> ReportProjection.read("badcurve", payload));
        assertTrue(exc.getMessage().contains("equity"), exc.getMessage());
    }

    /** 只有一个点的曲线不接受：一个点画不出曲线，也没法让「画出来是空的」与「没有数据」区分开。 */
    @Test
    void refusesTooShortCurve() throws Exception {
        JsonNode payload = MAPPER.readTree("""
                {"deterministic":{"performance":%s,
                                  "equity_curve":[{"timestamp":"a","equity":1,"drawdown":0}]}}
                """.formatted(fullMetricsJson()));
        assertThrows(DashboardException.class, () -> ReportProjection.read("short", payload));
    }

    /** 非数字当指标值必须被挡下。 */
    @Test
    void refusesNonNumericMetric() throws Exception {
        String metrics = mustReplace(fullMetricsJson(), "\"total_return\":0.0",
                "\"total_return\":\"NaN\"");
        JsonNode payload = MAPPER.readTree("""
                {"deterministic":{"performance":%s,
                                  "equity_curve":[{"timestamp":"a","equity":1,"drawdown":0},
                                                  {"timestamp":"b","equity":2,"drawdown":0}]}}
                """.formatted(metrics));
        DashboardException exc = assertThrows(DashboardException.class,
                () -> ReportProjection.read("nan", payload));
        assertTrue(exc.getMessage().contains("total_return"), exc.getMessage());
    }

    /** 显示规则：量纲换算、定点、负零、计数取整。
     *
     * <p>这里用的是**临时造的 spec**（不是 {@link MetricSpec#METRIC_SPECS} 里那 14 条）——
     * 目的是探格式器本身，所以刻意挑了几种 digits/unit 的组合。</p> */
    @Test
    void formatsMetricLikePythonSide() {
        assertEquals("-3.20%", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_RETURN, "l", MetricSpec.UNIT_PERCENT, 2),
                MAPPER.valueToTree(-0.032043452542899906)));
        assertEquals("-0.00%", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_RETURN, "l", MetricSpec.UNIT_PERCENT, 2),
                MAPPER.valueToTree(-0.0)));
        assertEquals("0.00%", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_RETURN, "l", MetricSpec.UNIT_PERCENT, 2),
                MAPPER.valueToTree(0.0)));
        assertEquals("1.5102", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_RISK, "l", MetricSpec.UNIT_RATIO, 4),
                MAPPER.valueToTree(1.5102148302585512)));
        assertEquals("1", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_TRADE, "l", MetricSpec.UNIT_COUNT, 0),
                MAPPER.valueToTree(1.0)));
        assertEquals("182.75", ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_COST, "l", MetricSpec.UNIT_MONEY, 2),
                MAPPER.valueToTree(182.7465397)));
        // 计数类指标给了小数：宁可报错，也不静默截断
        assertThrows(DashboardException.class, () -> ReportProjection.formatMetric(
                new MetricSpec("k", MetricSpec.GROUP_TRADE, "l", MetricSpec.UNIT_COUNT, 0),
                MAPPER.valueToTree(1.5)));
    }

    /** 目录层：非法 id 与不存在的 id 必须分成两种（前者是攻击面，后者是敲错地址）。 */
    @Test
    void catalogSeparatesIllegalFromMissing() {
        ReportCatalog catalog = new ReportCatalog(".rounds/i1");
        assertTrue(catalog.ids().contains("report-seed7-a"), String.valueOf(catalog.ids()));

        DashboardException illegal = assertThrows(DashboardException.class,
                () -> catalog.resolveReport("../../db/risk_control"));
        assertTrue(!(illegal instanceof ReportNotFoundException), "路径穿越必须是非法 id，不是 404");

        assertThrows(ReportNotFoundException.class, () -> catalog.resolveReport("nope-does-not-exist"));
    }

    /**
     * 变异必须自assert：{@code String.replace} 命中 0 次是静默 no-op，
     * 于是「坏样本」与原文件逐字节相同，用例会去测一个**没被改过的**输入。
     * （仓库历史里踩过两次：一次报「期望抛异常但什么都没抛」，一次报「全绿」。）
     */
    private static String mustReplace(String source, String from, String to) {
        if (!source.contains(from)) {
            throw new IllegalStateException("变异没命中：" + from + "（样本 = " + source + "）");
        }
        String mutated = source.replace(from, to);
        if (mutated.equals(source)) {
            throw new IllegalStateException("变异等于原串：" + from);
        }
        return mutated;
    }

    /** 14 个指标 + 一组能过校验的最小曲线，供上面几个负样本复用。 */
    private static String fullMetricsJson() {
        StringBuilder builder = new StringBuilder("{");
        for (int index = 0; index < MetricSpec.METRIC_SPECS.size(); index++) {
            if (index > 0) {
                builder.append(',');
            }
            builder.append('"').append(MetricSpec.METRIC_SPECS.get(index).key()).append("\":0.0");
        }
        return builder.append('}').toString();
    }
}
