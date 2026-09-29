package com.quanauto.dashboard;

import java.util.List;

/**
 * 看板要显示的全部内容。**没有派生指标字段** —— 只有报告里已有的东西。
 *
 * <p>字段清单被 {@code docs/智能量化交易平台.md} 附录C §C.3 逐条登记；
 * 前端只认这一份形状，不自己拼字段。
 *
 * @param id              报告 id（= 文件名去掉 {@code .json}）
 * @param schema          报告自称的 schema，原样透传（本层不校验版本号，只把它显示出来）
 * @param strategyId      策略 id（{@code deterministic.strategy_id}）
 * @param strategyVersion 策略版本
 * @param dataVersion     数据版本（决定这份回测用的是哪一版行情）
 * @param status          回测状态
 * @param seed            随机种子；取不到就是 {@code null}（**不是 0**）
 * @param symbol          标的；取不到就是 {@code null}
 * @param window          回测窗口 {@code [start, end]}；取不到就是 {@code null}
 * @param counts          三个结构计数，取不到的那一个为 {@code null}（显示成 {@code -}）
 * @param metrics         14 条指标读数，顺序与 {@link MetricSpec#METRIC_SPECS} 一致
 * @param curve           净值曲线逐点，与报告同序、不插值不抽样
 */
public record ReportView(
        String id,
        String schema,
        String strategyId,
        String strategyVersion,
        String dataVersion,
        String status,
        Integer seed,
        String symbol,
        List<String> window,
        CountsRead counts,
        List<MetricRead> metrics,
        List<CurvePointRead> curve) {

    /**
     * 一个指标的一次读数。
     *
     * <p><b>为什么不给前端 {@code value} 让前端自己格式化成 {@code text}</b>：
     * 那样会把「显示规则」实现两遍（Java 一遍、JS 一遍），而两份实现漂移是无声的。
     * 本层把 {@code text} 算好一起发出去，JS 只负责把它放进 DOM —— <b>前端零数字格式化</b>。
     *
     * @param value 报告里的原值（原样，不做量纲换算）
     * @param text  显示文本（唯一由本层产生的新东西 —— 量纲换算 + 定点小数）
     */
    public record MetricRead(
            String key,
            String group,
            String label,
            String unit,
            int digits,
            double value,
            String text) {
    }

    /** 净值曲线上的一个点 —— 逐字段照抄报告的 {@code equity_curve[i]}，不做插值/平滑。 */
    public record CurvePointRead(String timestamp, double equity, double drawdown) {
    }

    /** 三个结构计数。取不到的那一个为 {@code null} —— 绝不退回 0。 */
    public record CountsRead(Integer equityCurve, Integer trades, Integer orders) {
    }
}
