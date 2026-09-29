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
 */
public class DashboardException extends RuntimeException {

    public DashboardException(String message) {
        super(message);
    }
}
