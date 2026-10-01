package com.quanauto.dashboard;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * 显示规则的<b>逐条对拍</b>：把 {@code platform/text-parity-cases.json} 里的每一条喂进
 * {@link ReportProjection#formatMetric}，与夹具里的 {@code text} 逐字节比。
 *
 * <p>夹具的 {@code text} 是<b>Python 侧现算</b>出来的（{@code quanauto.dashboard.format_metric}
 * 才是权威），所以这个类做的事情就一句话：<b>Java 侧必须打印出与 Python 完全相同的字符串</b>。
 * 它是 {@code tools/verify_platform_text_parity.py} 的另一条腿：那一侧核夹具新不新鲜
 * （{@code PARITY-STALE-CASES}）、这一侧拿夹具当输入真的跑一遍。两侧都由
 * {@code platform-runtime} 门禁驱动（本类经真 {@code mvn test}）。
 *
 * <p>为什么非要有它：本轮实测（2026-10-01）拿 73929 组输入做差分，两侧在
 * <b>6887 组（9.3%）</b>上打印出不同的字符串 —— 其中「丢负号」4161 组、「平局落错边」2726 组，
 * 另有一类大整数计数（{@code 1e23}）分歧。而当时唯一的显示规则用例
 * {@link ReportProjectionTest#formatsMetricLikePythonSide} 只有 7 条断言，<b>全部通过</b>
 * —— 它一条都没碰到那些分歧点。7 条手挑的样本证明不了「两侧一致」，
 * 只有一份**按量纲 × 小数位 × 值**铺开的语料加一个自动比对能。
 *
 * <p>⚠️ 边界：本类只比 {@code formatMetric} 这一层。仍然零覆盖的是 <b>布局 / 视觉</b>
 * （jsdom 没有布局引擎，几何量恒为 0）—— <b>页面渲染</b>自 2026-10-01 晚起归运行层那条门禁的
 * 第二步 {@code npm test}（vitest + jsdom 真的把页面挂起来，见附录C §C.13）。
 * 分工见 {@code docs/智能量化交易平台.md} 附录C §C.8 / §C.9 / §C.11 / §C.12 / §C.13。
 */
class FormatParityTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String FIXTURE_REL = "platform/text-parity-cases.json";

    /** 夹具条数下限（镜像 Python 侧的 {@code MIN_CASES}）：防空转，见下面第一条断言。 */
    private static final int MIN_CASES = 60;

    /** 量纲 × 小数位组合数下限（镜像 Python 侧的 {@code PARITY-UNITDIGITS-COVERAGE}）。 */
    private static final int MIN_UNIT_DIGITS = 5;

    /**
     * 这批用例<b>必须在夹具里</b>（{@code unit, digits, value}）。它们各自钉住一类真实分歧
     * ——「丢减号」「平局落错边」「非可表示的大整数」，以及各组的不分歧对照。
     *
     * <p>为什么要写死这一份：只报「比了多少条、0 条不一致」的用例是<b>单向</b>的，
     * 有人把夹具删到只剩好走的样本（或者 {@code --write} 录到一半）它照样全绿。
     * 这份清单让「语料被削薄」当场变红。
     */
    private static final List<String> MUST_HAVE = List.of(
            // 平局落错边：最短往返表示把舍入推到另一侧
            "ratio|1|-29.95",
            "ratio|2|2.675",
            "money|2|182.745",
            "money|1|0.05",
            "ratio|4|0.00005",
            "percent|4|0.0000125",
            "days|2|1.005",
            // 丢减号：负的、舍入之后落到零上
            "money|2|-0.005",
            "money|2|-1e-09",
            "money|2|-5e-324",
            "percent|4|-1e-08",
            "ratio|0|-0.5",
            // 计数类大整数：`BigDecimal.valueOf` 给的不是精确值
            "count|0|1e+23",
            // 极大数：定点必须逐位展开，不许走科学计数
            "money|2|1.7976931348623157e+308");

    /**
     * 主用例：夹具里每一条都要与 Python 侧逐字节一致。
     *
     * <p>防空转守卫在最后那个 {@code assertEquals(MIN_CASES, ...)} 之前还有两条：
     * schema 对不对、{@code cases} 是不是数组。没有这两条的话，「夹具读成空表 ⇒ 循环 0 轮
     * ⇒ 0 条不一致 ⇒ 全绿」会把「语料没了」报成「两侧一致」。
     */
    @Test
    void formatsEveryFixtureCaseLikePythonSide() throws Exception {
        JsonNode fixture = MAPPER.readTree(fixturePath().toFile());
        assertEquals("quanauto.text-parity-cases/1", fixture.path("schema").asText(),
                "夹具 schema 不对：" + fixturePath());
        JsonNode cases = fixture.path("cases");
        assertTrue(cases.isArray(),
                "夹具的 cases 不是数组（读到 " + cases.getNodeType() + "）—— 后面会静默 0 轮");

        List<String> mismatches = new ArrayList<>();
        Set<String> unitDigits = new LinkedHashSet<>();
        int compared = 0;
        for (JsonNode item : cases) {
            String unit = item.path("unit").asText();
            int digits = item.path("digits").asInt();
            String raw = item.path("value").asText();
            String expected = item.path("text").asText();

            MetricSpec spec = new MetricSpec(item.path("key").asText(), "", "", unit, digits);
            String actual;
            try {
                actual = ReportProjection.formatMetric(spec, MAPPER.valueToTree(Double.parseDouble(raw)));
            } catch (RuntimeException exc) {
                actual = "THROWS:" + exc.getClass().getSimpleName();
            }
            compared++;
            unitDigits.add(unit + "|" + digits);
            if (!expected.equals(actual)) {
                mismatches.add(unit + " d=" + digits + " value=" + raw
                        + " python=" + expected + " java=" + actual);
            }
        }

        // 先报不一致，再报语料太薄 —— 反过来的话，一个被削空的夹具会先说「没比到几条」而
        // 掩盖「本来就该红」这件事……两者都要报，所以顺序无所谓，但两句话都在。
        assertTrue(mismatches.isEmpty(), "两侧显示文本不一致 " + mismatches.size() + " / " + compared
                + " 条（Python 侧才是权威，改 ReportProjection 而不是改夹具）：\n  "
                + String.join("\n  ", mismatches.subList(0, Math.min(8, mismatches.size()))));

        assertTrue(compared >= MIN_CASES,
                "夹具只有 " + compared + " 条用例（下限 " + MIN_CASES + "）—— 语料被削薄了，"
                        + "这个用例等于什么都没比。重新录：python tools/verify_platform_text_parity.py --write");
        assertTrue(unitDigits.size() >= MIN_UNIT_DIGITS,
                "夹具只覆盖 " + unitDigits.size() + " 种 量纲×小数位 组合（下限 "
                        + MIN_UNIT_DIGITS + "）：" + unitDigits);
    }

    /** 语料不许被削薄：上面那份 {@link #MUST_HAVE} 一条都不能少。 */
    @Test
    void fixtureStillHoldsTheHardCases() throws Exception {
        JsonNode cases = MAPPER.readTree(fixturePath().toFile()).path("cases");
        Set<String> present = new LinkedHashSet<>();
        for (JsonNode item : cases) {
            present.add(item.path("unit").asText() + "|" + item.path("digits").asInt()
                    + "|" + item.path("value").asText());
        }
        List<String> missing = new ArrayList<>();
        for (String want : MUST_HAVE) {
            if (!present.contains(want)) {
                missing.add(want);
            }
        }
        assertTrue(missing.isEmpty(),
                "夹具里少了这一批「曾经真的分歧」的用例：" + missing
                        + "。它们不在，这个对拍网就漏掉了本轮修掉的那些分歧点。");
    }

    /**
     * 夹具路径解析：与 {@link ReportCatalog#resolve} 同一条规则 —— 从工作目录往上找
     * {@code .git} 当仓库根，再拼 {@code platform/text-parity-cases.json}。
     *
     * <p>为什么不写相对工作目录的路径：{@code mvn test} 的工作目录取决于是从仓库根调还是从
     * {@code platform/api} 调，而 {@code platform-runtime} 门禁是在一次性沙箱里跑的
     * （沙箱根有一个空的 {@code .git/} 占位目录，为的正是让这条规则能落地）。
     * 找不到 {@code .git} 就退化成「从工作目录往上找这份夹具」，两条路都找不到才报错。
     */
    private static Path fixturePath() {
        Path start = Paths.get("").toAbsolutePath();
        for (Path cursor = start; cursor != null; cursor = cursor.getParent()) {
            if (Files.isDirectory(cursor.resolve(".git"))) {
                Path candidate = cursor.resolve(FIXTURE_REL);
                assertTrue(Files.isRegularFile(candidate),
                        "仓库根（" + cursor + "）下没有 " + FIXTURE_REL + " —— 对拍没有语料可读");
                return candidate;
            }
        }
        for (Path cursor = start; cursor != null; cursor = cursor.getParent()) {
            for (String rel : List.of(FIXTURE_REL, "text-parity-cases.json")) {
                Path candidate = cursor.resolve(rel);
                if (Files.isRegularFile(candidate)) {
                    return candidate;
                }
            }
        }
        throw new IllegalStateException(
                "从 " + start + " 往上都找不到 " + FIXTURE_REL + "（对拍没有语料可读）");
    }
}
