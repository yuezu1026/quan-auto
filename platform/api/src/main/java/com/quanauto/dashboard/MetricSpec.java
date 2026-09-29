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
     * <p>⚠️ <b>这是一份手抄的副本，而目前没有任何门禁盯着它。</b>
     * 权威定义在 {@code quanauto/dashboard.py} 的 {@code METRIC_SPECS}（那里的
     * {@code tools/verify_dashboard.py} C2 与 {@code tests/test_dashboard.py} 双向对齐
     * {@code models.PerformanceMetrics}）。本列表逐字照抄，并由
     * {@code docs/智能量化交易平台.md} 附录C 的表格逐行登记。
     *
     * <p>风险是明确的：这条清单一旦漂移，两边就会显示不同的指标名/量纲，
     * 而<b>没有任何检查会报</b>（Java 与前端两侧目前零门禁覆盖，见附录C §C.5）。
     * 最省事的第一条补牙办法就是写一条纯 Python 静态检查器，解析本文件与
     * {@code dashboard.py} 做双向比对 —— 记为待办，不假装它已经存在。
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
