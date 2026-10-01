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
 * <b>已过期</b>）—— 声明层 {@code platform-spec-parity}、运行层 {@code platform-runtime}
 * 与显示规则层 {@code platform-text-parity}（2026-10-01 追加）
 * 三条 tier-A 门禁都接在 {@code tools/run_all_gates.py} 的统一入口里，
 * 而 {@code ci.yml} 的最后一步就是那条命令 ⇒ 三条都真的在 CI 上跑。
 *
 * <p><b>但三条都</b>不起服务（跑完就退出，没有一个活着的 JVM 在监听端口）、
 * <b>也都不渲染页面</b> ⇒ 这一层仍然零覆盖的
 * 只剩 <b>页面本身</b>（渲染 / 布局 / 视觉）。
 * ⚠️ 订正（2026-10-01）：原来这里还写着「② 两侧展示文本是否一致」<b>已过期</b> ——
 * 「两侧按同一条显示规则打印出同一个串」现在由 {@code platform-text-parity} 盯着
 * （夹具 {@code platform/text-parity-cases.json} + {@link FormatParityTest}）。
 * 仍然只有手动脚本 {@code platform/check_text_parity.py} 的是<b>另一件事</b>：
 * 一个<b>活着的服务端</b>到底下发了什么（它需要一个活着的 JVM ⇒ 不能当门禁 ⇒ 不构成常驻证据）。
 * 分工与更正见 {@code docs/智能量化交易平台.md} 附录C §C.8（声明那层）、
 * §C.9（运行那层）与 §C.5 第 3 条的 2026-10-01 订正。别把「本地跑绿过」当证据。
 */
@SpringBootApplication
public class DashboardApiApplication {

    public static void main(String[] args) {
        SpringApplication.run(DashboardApiApplication.class, args);
    }
}
