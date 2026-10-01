package com.quanauto.dashboard;

/**
 * 读报告读不下去时抛这个。
 *
 * <p>刻意与「HTTP 出错」分开：本类只表达「这份报告我看不懂 / 它缺东西」，
 * 由 {@link ApiExceptionHandler} 决定把它翻译成哪个状态码。
 *
 * <p>行为口径与 Python 侧 {@code quanauto.errors.DashboardError} 一致：
 * <b>取不到就抛，绝不退回 0</b> —— {@code 0.0} 与「这个字段不存在」长得一样，
 * 退回 0 会得到一张「所有指标都是 0」的看板，而它和「真的算出来全是 0」无法区分。
 *
 * <p><b>两条消息，两个去处</b>（2026-09-30 补）：{@link #getMessage()} 是给人查日志的，
 * 可以说出本机绝对路径；{@link #clientMessage()} 是会<b>下发给浏览器</b>的那一句，必须不含内部路径。
 * 两者的**默认值相同** —— 只有真的会拼路径的那几条才需要显式给第二条，也就是
 * {@link ReportCatalog} 里包了 {@code directory} 或底层 {@code IOException} 的那些消息，
 * 以及 {@link ReportNotFoundException} 的「目录 …」那一截。
 *
 * <p>为什么非要拆：{@link ApiExceptionHandler} 的类注释承诺「不回内部路径」，
 * 而它当时的实现是 {@code body(Map.of("error", exc.getMessage()))} ⇒ 只要消息里带路径，
 * 承诺就不成立（实测吐出了
 * {@code {"error":"没有这份报告：nope（目录 D:\\…\\.rounds\\i1）"}}）。
 * 拆成两条是在**实现**里兑现那句话，而不是把承诺删掉。
 */
public class DashboardException extends RuntimeException {

    private final String clientMessage;

    public DashboardException(String message) {
        this(message, message);
    }

    /**
     * @param message       详细消息，进日志（<b>可以</b>带本机路径）
     * @param clientMessage 下发消息，进 HTTP 响应体（<b>不许</b>带内部路径）
     */
    public DashboardException(String message, String clientMessage) {
        super(message);
        this.clientMessage = clientMessage;
    }

    /** 可以安全交给浏览器的那一句。默认为 {@link #getMessage()}。 */
    public String clientMessage() {
        return clientMessage;
    }
}
