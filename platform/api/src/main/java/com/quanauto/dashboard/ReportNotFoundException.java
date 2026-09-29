package com.quanauto.dashboard;

/**
 * 「这个名字的报告不存在」。
 *
 * <p>刻意与「报告存在但读不懂」分开：前者是 404（用户把地址敲错了），后者是 422
 * （报告本身有问题，得去研究层查）。把两件事合成一个错误码，界面就只能说
 * 「出错了」，而分不清「换个报告试试」和「这份报告坏了」。
 */
public class ReportNotFoundException extends DashboardException {

    public ReportNotFoundException(String message) {
        super(message);
    }
}
