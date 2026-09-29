package com.quanauto.dashboard;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 报告目录：本层<b>唯一</b>的文件系统接触面。
 *
 * <p>报告不是本层产出的，而是研究层 {@code python -m quanauto.cli backtest --out PATH} 写出来的
 * JSON（{@code schema = "quanauto.backtest-report/1"}）。本层只做一件事：按名字把它们找出来。
 * 仓库里已入库的现成样本是 {@code .rounds/i1/report-seed7-a.json} 这三个。
 *
 * <p><b>为什么不用数据库</b>：附录B.3 要求「先把『{@code BacktestResult} 如何落库、由谁读出』
 * 写成契约」。本轮选择的是<b>最省的那一条读法</b>：报告本来就以文件形式存在（研究层已经这么写了），
 * 本层直接读同一份文件。于是「落库」这件事在本切片里<b>不存在</b> —— 也就不需要新表、
 * 不需要碰 {@code db/*.sql}、不需要跑容器 PostgreSQL。这条选择连同它的代价
 * （文件目录不是并发安全的存储、没有版本索引）写在附录C §C.2。
 */
@Component
public class ReportCatalog {

    private static final Logger log = LoggerFactory.getLogger(ReportCatalog.class);

    /**
     * 报告 id 的白名单：只允许这些字符，且必须以字母或数字开头。
     *
     * <p>这一条是<b>安全边界</b>，不是风格偏好：id 直接来自 URL 路径段，若允许 {@code /} 或 {@code ..}
     * 就能读走目录外的任意文件。正则 + 下面的 {@code startsWith} 双重把关。
     */
    private static final Pattern SAFE_ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]*");

    private final Path directory;
    private final ObjectMapper mapper = new ObjectMapper();

    public ReportCatalog(@Value("${quanauto.reports.dir:.rounds/i1}") String configured) {
        this.directory = resolve(configured);
        log.info("报告目录：{}（{}）", directory, Files.isDirectory(directory) ? "存在" : "不存在");
    }

    /**
     * 相对路径解析成<b>仓库根</b>下的路径：从工作目录往上找 {@code .git}。
     *
     * <p>为什么要这么绕：{@code mvn spring-boot:run} 的进程工作目录取决于怎么调用
     * （从仓库根调、还是从 {@code platform/api} 调），把默认值绑在工作目录上会让
     * 「同一份配置在两个目录下指向两个地方」。绑到仓库根就与调用位置无关了。
     * 找不到 {@code .git}（例如被打成 jar 单独跑）就退回工作目录，并且启动日志会打印实际值。
     */
    private static Path resolve(String configured) {
        Path path = Paths.get(configured);
        if (path.isAbsolute()) {
            return path.normalize();
        }
        Path cursor = Paths.get("").toAbsolutePath();
        while (cursor != null) {
            if (Files.isDirectory(cursor.resolve(".git"))) {
                return cursor.resolve(path).normalize();
            }
            cursor = cursor.getParent();
        }
        return path.toAbsolutePath().normalize();
    }

    public Path directory() {
        return directory;
    }

    /** 报告 id（= 文件名去掉 {@code .json}），按名字排序。目录不存在时返回空表而不是报错。 */
    public List<String> ids() {
        if (!Files.isDirectory(directory)) {
            return List.of();
        }
        try (Stream<Path> stream = Files.list(directory)) {
            return stream
                    .filter(Files::isRegularFile)
                    .map(p -> p.getFileName().toString())
                    .filter(name -> name.endsWith(".json"))
                    .map(name -> name.substring(0, name.length() - ".json".length()))
                    .sorted(Comparator.naturalOrder())
                    .toList();
        } catch (IOException exc) {
            throw new DashboardException("报告目录读取失败：" + directory + "（" + exc.getMessage() + "）");
        }
    }

    /** 把 id 解析成文件路径。非法 id 与越界路径都在这里挡掉。 */
    public Path resolveReport(String id) {
        if (id == null || !SAFE_ID.matcher(id).matches()) {
            throw new DashboardException("报告 id 非法：" + id + "（只允许字母/数字/点/下划线/连字符）");
        }
        Path path = directory.resolve(id + ".json").normalize();
        if (!path.startsWith(directory)) {
            throw new DashboardException("报告路径越出报告目录：" + id);
        }
        if (!Files.isRegularFile(path)) {
            throw new ReportNotFoundException("没有这份报告：" + id + "（目录 " + directory + "）");
        }
        return path;
    }

    public JsonNode load(String id) {
        Path path = resolveReport(id);
        try {
            return mapper.readTree(path.toFile());
        } catch (IOException exc) {
            throw new DashboardException("报告读取失败：" + id + "（" + exc.getMessage() + "）");
        }
    }

    /**
     * 目录列表用的轻量摘要：只取报告自己写的 {@code summary} 段，<b>不做投影</b>。
     * 因此一个格式不对的文件不会让整个列表打不开 —— 它会在 {@code error} 里说明自己为什么不行。
     */
    public List<ReportSummary> summaries() {
        List<ReportSummary> out = new ArrayList<>();
        for (String id : ids()) {
            try {
                JsonNode payload = load(id);
                JsonNode summary = payload.get("summary");
                String symbol = null;
                String start = null;
                String end = null;
                Integer bars = null;
                if (summary != null && summary.isObject()) {
                    JsonNode sym = summary.get("symbol");
                    symbol = sym == null || sym.isNull() ? null : sym.asText();
                    JsonNode window = summary.get("window");
                    if (window != null && window.isArray() && window.size() == 2) {
                        start = window.get(0).asText();
                        end = window.get(1).asText();
                    }
                    JsonNode barsNode = summary.get("bars");
                    bars = barsNode != null && barsNode.isIntegralNumber() ? barsNode.intValue() : null;
                }
                out.add(new ReportSummary(id, symbol, start, end, bars, null));
            } catch (DashboardException exc) {
                out.add(new ReportSummary(id, null, null, null, null, exc.getMessage()));
            }
        }
        return out;
    }

    /**
     * 列表项。
     *
     * @param error 这份报告读不出来的原因；能读出来时为 {@code null}
     */
    public record ReportSummary(
            String id,
            String symbol,
            String windowStart,
            String windowEnd,
            Integer bars,
            String error) {
    }
}
