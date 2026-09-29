package com.quanauto.dashboard;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 回测绩效看板（平台层切片 P0）：读取侧。
 *
 * <p>这一层存在的理由只有一条：<b>让人觉得能点</b>。它把一个已经落盘的回测报告摆到浏览器里，
 * 除此之外什么都不做 —— 不算指标、不落库、不调度、不接行情。
 *
 * <p><b>它与 I4 的关系（重要）</b>：研究层已经有一个看板（{@code quanauto/dashboard.py}），
 * 它是纯读数组件，只有 {@code text} 与单文件 {@code html} 两种展示形态，本轮<b>一个字都没改</b>。
 * 这里的两边读的是<b>同一份报告 JSON</b>（{@code schema = "quanauto.backtest-report/1"}），
 * 因此不存在「谁是权威」的问题：权威是报告本身，两边都只是它的读者。
 * 「不能各算一套指标」这个担忧由「本层零统计」这条结构约束来消解，
 * 详见 {@link ReportProjection} 的类注释。
 *
 * <p><b>本层目前没有门禁、也不在 CI 里</b>（`docs/开工前缺口清单.md` 与
 * `docs/智能量化交易平台.md` 附录C 都已如实登记）。别把「本地跑绿过」当证据。
 */
@SpringBootApplication
public class DashboardApiApplication {

    public static void main(String[] args) {
        SpringApplication.run(DashboardApiApplication.class, args);
    }
}
