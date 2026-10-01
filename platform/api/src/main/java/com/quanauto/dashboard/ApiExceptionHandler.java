package com.quanauto.dashboard;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

/**
 * 把异常折成 {@code {"error": "..."}}，并且<b>只回这一句</b>。
 *
 * <p>不回堆栈、不回 problem detail、不回内部路径 —— 这一层的错误信息天然会带着
 * 本机文件路径（{@code ReportCatalog} 的异常里就有），直接吐给浏览器是没必要的暴露。
 * 想看细节的人去服务端日志里看（那里 {@code log.warn} 了同一个 cause）。
 *
 * <p>⚠️ 上面那句「不回内部路径」第一版**没兑现**（2026-09-30 修）：当时下发的是
 * {@link DashboardException#getMessage()}，而 {@code ReportCatalog} 的详细消息里拼着本机绝对目录
 * ⇒ 真的吐出了 {@code {"error":"没有这份报告：nope（目录 D:\…\.rounds\i1）"}}。
 * 现在下发的是 {@link DashboardException#clientMessage()}（详细消息照旧进日志），
 * 由 {@link ApiExceptionHandlerTest} 钉住；那条用例还带一条**前提断言**
 * （详细消息里必须真的带目录），免得它在前提消失后退化成一句永远为真的空转。
 *
 * <p>状态码分工：
 * <ul>
 *   <li>{@code 404} —— 这个名字没有报告（用户敲错地址）</li>
 *   <li>{@code 422} —— 报告在，但读不懂/字段缺（是研究层的产物有问题），
 *       与研究层 Python 侧「取不到就抛、绝不退回 0」同一个立场：<b>宁可报错，不给一个假的 0</b>。</li>
 * </ul>
 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(ReportNotFoundException.class)
    public ResponseEntity<Map<String, String>> handleNotFound(ReportNotFoundException exc) {
        log.warn("报告不存在：{}", exc.getMessage());
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of("error", exc.clientMessage()));
    }

    @ExceptionHandler(DashboardException.class)
    public ResponseEntity<Map<String, String>> handleBadReport(DashboardException exc) {
        log.warn("报告读不懂：{}", exc.getMessage());
        return ResponseEntity.status(HttpStatus.UNPROCESSABLE_ENTITY)
                .body(Map.of("error", exc.clientMessage()));
    }

    /**
     * 静态资源没命中时 Spring 会抛这个（例如还没 {@code npm run build} 就访问 {@code /}）。
     * 不特判它的话，浏览器会收到 500 + 一大段堆栈 —— 而真实原因只是「前端没构建」。
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<Map<String, String>> handleNoResource(NoResourceFoundException exc) {
        log.warn("静态资源没找到：{}", exc.getMessage());
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(Map.of("error", "页面不存在（前端还没构建？先在 platform/web 跑 npm run build）"));
    }

    /** 兜底：任何没预料到的异常也只回一句话，但日志里留全栈，别把线上问题变成不可查。 */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> handleUnexpected(Exception exc) {
        log.error("未预料的异常", exc);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("error", "服务端异常，详见服务端日志"));
    }
}
