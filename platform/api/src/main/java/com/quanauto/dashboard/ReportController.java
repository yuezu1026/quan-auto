package com.quanauto.dashboard;

import java.util.List;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 本层唯一的 HTTP 面：三个只读端点。
 *
 * <p>没有写端点、没有上传、没有「重新计算」—— 加一个都需要先改附录C 的契约，
 * 因为「能触发计算」的口子一开，「屏幕上每个数都来自同一份报告」这句话就不再成立。
 *
 * <p>注意端点返回的 {@link ReportView} 里，每个指标都带着服务端算好的 {@code text}。
 * 前端<b>不要</b>拿 {@code value} 自己格式化：显示规则只有一份实现（{@link ReportProjection#formatMetric}）。
 */
@RestController
@RequestMapping("/api")
public class ReportController {

    private final ReportCatalog catalog;

    public ReportController(ReportCatalog catalog) {
        this.catalog = catalog;
    }

    /** 存活探针。把解析出来的报告目录回显出来 —— 目录指错是这一层最常见的故障。 */
    @GetMapping("/health")
    public Map<String, Object> health() {
        return Map.of(
                "status", "UP",
                "reportsDir", catalog.directory().toString(),
                "reportCount", catalog.ids().size(),
                "metricCount", MetricSpec.METRIC_SPECS.size());
    }

    /**
     * 报告列表。读不出来的那份会带着 {@code error} 出现在列表里，而不是让整个列表打不开。
     *
     * <p>外层刻意包一层 {@code {"reports": [...]}} 而<b>不是</b>直接返回裸数组：
     * 一是与另一侧的 {@code {"error": ...}} 同一形状（客户端只需要一套拆包逻辑），
     * 二是以后要加 {@code total} / {@code dir} 这类元信息不必改客户端的根类型。
     *
     * <p>⚠️ 第一版这里返回的就是裸数组，而前端读的是 {@code body.reports} ⇒
     * 前端把它当成「目录里没有报告」。两侧各自都能跑，缝上直接错 ——
     * {@code platform/check_text_parity.py} 现在也把这条信封形状钉住了。
     */
    @GetMapping("/reports")
    public ReportList reports() {
        return new ReportList(catalog.summaries());
    }

    /** 列表端点的信封。 */
    public record ReportList(List<ReportCatalog.ReportSummary> reports) {
    }

    /** 一份报告的完整视图。 */
    @GetMapping("/reports/{id}")
    public ReportView report(@PathVariable String id) {
        return ReportProjection.read(id, catalog.load(id));
    }
}
