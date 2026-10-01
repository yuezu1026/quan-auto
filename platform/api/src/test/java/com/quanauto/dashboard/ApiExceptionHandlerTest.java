package com.quanauto.dashboard;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.servlet.resource.NoResourceFoundException;

/**
 * 错误体的用例。
 *
 * <p>它盯的是一句**承诺**：{@link ApiExceptionHandler} 的类注释写「只回这一句、不回内部路径」。
 * 第一版**没兑现** —— 下发的是 {@code exc.getMessage()}，而 {@link ReportCatalog} 的详细消息里
 * 拼着本机绝对报告目录，于是浏览器真能收到
 * {@code {"error":"没有这份报告：nope（目录 D:\…\.rounds\i1）"}}。
 * 现在下发的是 {@link DashboardException#clientMessage()}（详细消息照旧进日志）。
 *
 * <p><b>为什么每条断言都配一条前提断言</b>：像
 * {@code assertFalse(body.contains(dir))} 这种句子，只要那个 {@code dir} 没进过任何消息，
 * 它就**恒真** —— 用例照样是绿的，却什么都没测（本仓库反复踩过的「提取为空 ⇒ 假绿」同族）。
 * 所以每条这样的断言前面都先断言**详细消息里确实带着那个目录**；
 * 前提一旦消失就判红，因为那说明这条用例该重写了，而不是说明它通过了。
 *
 * <p>覆盖到的是 {@link ReportCatalog} 里**能由 URL 走到**的两条带路径消息
 * （{@code resolveReport} 的「没有这份报告」与 {@code load} 的「报告读取失败」）。
 * 第三条（{@code ids()} 的「报告目录读取失败」）**没有**覆盖：在 Windows 上把
 * {@code Files.list} 弄失败要动权限，写出来的用例会在别的机器上变成噪声 ——
 * 这里如实记一笔，不假装它被测过。
 */
class ApiExceptionHandlerTest {

    /** 处理器是无状态的，直接 new 就行 —— 不需要起 Spring 上下文。 */
    private final ApiExceptionHandler handler = new ApiExceptionHandler();

    private static String body(ResponseEntity<Map<String, String>> response) {
        Map<String, String> payload = response.getBody();
        assertNotNull(payload, "响应体不该是空的");
        String error = payload.get("error");
        assertNotNull(error, "响应体里必须有 error 字段");
        return error;
    }

    /** 404：只回「没有这份报告：<id>」，不带那个本机目录。 */
    @Test
    void notFoundBodyHasNoLocalPath() throws Exception {
        Path dir = Files.createTempDirectory("qa-not-found");
        ReportCatalog catalog = new ReportCatalog(dir.toString());

        ReportNotFoundException exc = assertThrows(
                ReportNotFoundException.class, () -> catalog.resolveReport("nope"));

        // 前提：详细消息里**真的**带着那个目录。前提没了 ⇒ 本用例在空转 ⇒ 判红（fail-closed）。
        assertTrue(
                exc.getMessage().contains(dir.toString()),
                "前提失效：详细消息里没有本机目录，这条用例就变成了永远为真的空转 —— 请重写它");

        ResponseEntity<Map<String, String>> response = handler.handleNotFound(exc);
        assertEquals(HttpStatus.NOT_FOUND, response.getStatusCode());
        assertEquals("没有这份报告：nope", body(response));
        assertFalse(body(response).contains(dir.toString()), "把本机报告目录吐给浏览器了");
        assertFalse(body(response).contains("\\"), "错误体里出现了路径分隔符");
    }

    /** 422：读不出来的那份只回「报告读取失败：<id>」，不带底层 IOException 里的路径。 */
    @Test
    void unreadableReportBodyHasNoLocalPath() throws Exception {
        Path dir = Files.createTempDirectory("qa-bad-json");
        Files.writeString(dir.resolve("bad.json"), "{", StandardCharsets.UTF_8);
        ReportCatalog catalog = new ReportCatalog(dir.toString());

        DashboardException exc = assertThrows(DashboardException.class, () -> catalog.load("bad"));

        // 前提：两条消息**确实是分开的** —— 否则下面「下发的那句更短」全是废话。
        assertNotEquals(exc.getMessage(), exc.clientMessage(), "前提失效：这条异常没有分开的两条消息");

        ResponseEntity<Map<String, String>> response = handler.handleBadReport(exc);
        assertEquals(HttpStatus.UNPROCESSABLE_ENTITY, response.getStatusCode());
        assertEquals("报告读取失败：bad", body(response));
        assertFalse(body(response).contains(dir.toString()), "把本机报告目录吐给浏览器了");
    }

    /** `/api/reports` 列表里那一格 {@code error} 也是下发给浏览器的，同样不许带路径。 */
    @Test
    void summaryListErrorIsAlsoClientFacing() throws Exception {
        Path dir = Files.createTempDirectory("qa-summary");
        Files.writeString(dir.resolve("bad.json"), "{", StandardCharsets.UTF_8);
        ReportCatalog catalog = new ReportCatalog(dir.toString());

        List<ReportCatalog.ReportSummary> summaries = catalog.summaries();

        assertEquals(1, summaries.size());
        assertEquals("报告读取失败：bad", summaries.get(0).error());
        assertFalse(summaries.get(0).error().contains(dir.toString()));
    }

    /**
     * 处理器与异常的契约：**下发 {@code clientMessage()}，而不是 {@code getMessage()}**。
     *
     * <p>这一条不碰文件系统 —— 它把「详细消息带路径」直接造出来，因此哪怕
     * {@link ReportCatalog} 以后不再拼路径，这条钉子也照样有效。
     */
    @Test
    void handlerEmitsClientMessageNotDetail() {
        DashboardException detailed = new DashboardException(
                "细节：D:\\secret\\x，这句不该出去", "读不懂这份报告");
        assertEquals("读不懂这份报告", body(handler.handleBadReport(detailed)));

        ReportNotFoundException notFound = new ReportNotFoundException(
                "没有这份报告：a（目录 D:\\secret）", "没有这份报告：a");
        assertEquals("没有这份报告：a", body(handler.handleNotFound(notFound)));
    }

    /**
     * 默认值这条同样要钉住：{@link ReportProjection} 的 422 文案是页面上**唯一有用的**
     * 那句话（「报告里没有 deterministic 段 ……」），它无路径、必须原样下发。
     * 若有人把 clientMessage 的默认值改成一句笼统的「出错了」，这条会红。
     */
    @Test
    void projectionMessageReachesTheBrowserUnchanged() {
        DashboardException exc = new DashboardException("报告里没有 deterministic 段 —— 看板只认 X");
        assertEquals(exc.getMessage(), exc.clientMessage());
        assertTrue(body(handler.handleBadReport(exc)).contains("deterministic"));
    }

    /** 前端没构建时访问 {@code /} 要落成 404，而不是 500 + 一堆堆栈。 */
    @Test
    void missingStaticResourceIsNotFoundNotServerError() {
        ResponseEntity<Map<String, String>> response =
                handler.handleNoResource(new NoResourceFoundException(HttpMethod.GET, "index.html"));

        assertEquals(HttpStatus.NOT_FOUND, response.getStatusCode());
        assertTrue(body(response).contains("npm run build"), "提示里该告诉人前端要构建");
    }

    /** 兜底：任何没预料到的异常也只回一句固定的话，不把 cause 的文本带出去。 */
    @Test
    void unexpectedIsGeneric500() {
        ResponseEntity<Map<String, String>> response =
                handler.handleUnexpected(new IllegalStateException("boom at D:\\secret"));

        assertEquals(HttpStatus.INTERNAL_SERVER_ERROR, response.getStatusCode());
        assertEquals("服务端异常，详见服务端日志", body(response));
        assertFalse(body(response).contains("boom"));
    }
}
