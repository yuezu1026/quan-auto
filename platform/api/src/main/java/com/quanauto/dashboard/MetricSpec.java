package com.quanauto.dashboard;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一个指标<b>怎么取、怎么显示</b>。注意它<b>没有「怎么算」的字段</b> —— 这是刻意的。
 *
 * <p>与 Python 侧 {@code quanauto.dashboard.MetricSpec} 同名同义同序。
 *
 * @param key    报告 {@code deterministic.performance} 段里的字段名
 * @param group  界面分组（收益 / 风险 / 成交 / 成本）
 * @param label  中文名
 * @param unit   量纲，只影响<b>显示</b>
 * @param digits 定点小数位
 */
public record MetricSpec(String key, String group, String label, String unit, int digits) {

    public static final String UNIT_PERCENT = "percent";
    public static final String UNIT_RATIO = "ratio";
    public static final String UNIT_COUNT = "count";
    public static final String UNIT_MONEY = "money";
    public static final String UNIT_DAYS = "days";

    public static final String GROUP_RETURN = "收益";
    public static final String GROUP_RISK = "风险";
    public static final String GROUP_TRADE = "成交";
    public static final String GROUP_COST = "成本";

    /**
     * 14 条，顺序即界面顺序。
     *
     * <p><b>权威定义在 {@code quanauto/dashboard.py} 的 {@code METRIC_SPECS}</b>（那里的
     * {@code tools/verify_dashboard.py} C2 与 {@code tests/test_dashboard.py} 双向对齐
     * {@code models.PerformanceMetrics}）。本列表逐字照抄，并由
     * {@code docs/智能量化交易平台.md} 附录C 的表格逐行登记。
     *
     * <p>⚠️ 抄得再像也只是一份<b>副本</b>：它一旦漂移，两边就会显示不同的指标名/量纲。
     * 2026-09-30 起这条风险有门禁盯着 —— {@code platform-spec-parity}
     * （{@code tools/verify_platform_specs.py}，接进 {@code tools/run_all_gates.py} 的统一入口，
     * 因此也在 CI 上跑）把本文件与 {@code dashboard.py} <b>双向</b>比对：
     * 量纲/分组常量取值、14 行的键-分组-标签-量纲-小数位、<b>行序</b>、
     * 以及 {@code STRUCTURE_COUNTS} 的取值与顺序。
     * 也就是说：<b>本文件与 Python 侧不一致时，是这里要改</b>，不是改 {@code dashboard.py}。
     *
     * <p>⚠️ 门禁只覆盖到「这张<b>声明表</b>两侧一致」为止。它不编译、不启动 JVM、不渲染页面
     * ⇒ 本门禁管不到 {@code mvn test}、{@code npm run build} 与页面本身。
     * 订正（2026-10-01）：原文写的是「{@code mvn test}、{@code npm run build} 与页面本身
     * <b>依然没有门禁盯着</b>」，那半句<b>自 2026-09-30 起已不成立</b> ——
     * 运行层有 {@code platform-runtime}（一次性沙箱里真跑 {@code npm ci} →
     * {@code npm run build} → {@code mvn test}，见附录C §C.9），
     * 显示规则层有 {@code platform-text-parity}（见 §C.11）
     * ⇒ 如今只剩 <b>页面本身</b>（渲染 / 布局 / 视觉）仍零覆盖。
     */
    public static final List<MetricSpec> METRIC_SPECS = List.of(
            new MetricSpec("total_return", GROUP_RETURN, "累计收益", UNIT_PERCENT, 4),
            new MetricSpec("annual_return", GROUP_RETURN, "年化收益", UNIT_PERCENT, 4),
            new MetricSpec("final_equity", GROUP_RETURN, "期末净值", UNIT_MONEY, 2),
            new MetricSpec("max_drawdown", GROUP_RISK, "最大回撤", UNIT_PERCENT, 4),
            new MetricSpec("sharpe_ratio", GROUP_RISK, "夏普比率", UNIT_RATIO, 4),
            new MetricSpec("sortino_ratio", GROUP_RISK, "索提诺比率", UNIT_RATIO, 4),
            new MetricSpec("calmar_ratio", GROUP_RISK, "卡玛比率", UNIT_RATIO, 4),
            new MetricSpec("win_rate", GROUP_TRADE, "胜率", UNIT_PERCENT, 4),
            new MetricSpec("profit_factor", GROUP_TRADE, "盈亏比（总额）", UNIT_RATIO, 4),
            new MetricSpec("profit_loss_ratio", GROUP_TRADE, "盈亏比（均额）", UNIT_RATIO, 4),
            new MetricSpec("max_consecutive_losses", GROUP_TRADE, "最大连续亏损", UNIT_COUNT, 0),
            new MetricSpec("avg_hold_period", GROUP_TRADE, "平均持仓天数", UNIT_DAYS, 2),
            new MetricSpec("total_trades", GROUP_TRADE, "成交笔数", UNIT_COUNT, 0),
            new MetricSpec("total_commission", GROUP_COST, "累计佣金", UNIT_MONEY, 2));

    /**
     * 报告结构里的三个计数。它们是<b>结构计数</b>，不是绩效指标：不进 {@link #METRIC_SPECS}，
     * 也不参与「字段清单双向对齐」，但同样来自报告、同样不重算。
     */
    public static final List<String> STRUCTURE_COUNTS = List.of("equity_curve", "trades", "orders");

    /** 按 key 建索引；用 {@link LinkedHashMap} 保住声明顺序，便于报错信息稳定。 */
    public static Map<String, MetricSpec> byKey() {
        Map<String, MetricSpec> index = new LinkedHashMap<>();
        for (MetricSpec spec : METRIC_SPECS) {
            index.put(spec.key(), spec);
        }
        return index;
    }
}
