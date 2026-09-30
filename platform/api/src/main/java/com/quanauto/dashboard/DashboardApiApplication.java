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
 * <p><b>本层现在有门禁了</b>（2026-09-30 订正：原文写的是「本层目前没有门禁、也不在 CI 里」，
 * <b>已过期</b>）—— 声明层 {@code platform-spec-parity} 与运行层 {@code platform-runtime}
 * 两条 tier-A 门禁都接在 {@code tools/run_all_gates.py} 的统一入口里，
 * 而 {@code ci.yml} 的最后一步就是那条命令 ⇒ 两条都真的在 CI 上跑。
 *
 * <p><b>但两条都既不启动 JVM、也不渲染页面</b> ⇒ 这一层仍然零覆盖的只有两件事：
 * ① <b>页面本身</b>（渲染 / 布局 / 视觉）；② <b>两侧展示文本是否一致</b>
 * —— 后者只有手动脚本 {@code platform/check_text_parity.py}，而它需要一个活着的 JVM
 * ⇒ 不能当门禁 ⇒ 不构成常驻证据。分工与更正见 {@code docs/智能量化交易平台.md}
 * 附录C §C.8（声明那层）与 §C.9（运行那层）。别把「本地跑绿过」当证据。
 */
@SpringBootApplication
public class DashboardApiApplication {

    public static void main(String[] args) {
        SpringApplication.run(DashboardApiApplication.class, args);
    }
}
