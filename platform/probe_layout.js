/* ─────────────────────────────────────────────────────────────────────────────
 * 平台层「布局 / 视觉」一次性探针（2026-10-01 首次真量）
 *
 * ⚠️ 它**不是门禁**，也**不能**变成门禁。理由只有一条，而且是实测过的：
 *    几何量只有在**真有布局引擎**的地方才有意义，而本仓库能常驻跑的那个 DOM
 *    环境（vitest + jsdom）**没有布局引擎** —— 在那里 getBoundingClientRect()
 *    恒返回 0×0、innerWidth 恒 1024 ⇒ 写「没溢出视口 / 没重叠」这类断言**恒真**，
 *    是本仓库最忌讳的**假绿**（见 platform/web/test/render.test.jsx 的最后一条用例，
 *    那条用例专门把这个事实钉住，好让后来的人别去 jsdom 里写几何断言）。
 *    要它就得**同时**有一个活着的 JVM（托管页面的后端）和一个真浏览器 ⇒
 *    **判据依赖环境就是判据的缺陷** ⇒ 本条只能是「一次性取证 + 可复跑的探针」，
 *    与 platform/check_text_parity.py 同一个档位（那个要活着的 JVM）。
 *
 * ⚠️ 它**自己也不作数**：门禁的效力来自「构造坏样本证明它会 FAIL」（变异测试），
 *    而本探针跑一次要一个活浏览器 ⇒ 本仓库**没有**给它做变异。所以它的结论只能读成
 *    「**这一次**、**这一档视口**、**这一份报告**下没量到问题」，不能读成「布局没问题」。
 *
 * 怎么跑（两条路，都要求后端已经起来并且在托管**当前**工作树构建出来的页面）：
 *   1) 先 `cd platform/web && npm run build`（产物写进 api/src/main/resources/static/），
 *      再起后端：`mvn -B -f platform/pom.xml -pl api spring-boot:run`
 *      ⚠️ 顺序反了就 served 旧包；且 mvn 用的是 JAVA_HOME（本机 21），不是 PATH 里的 java。
 *   2) 然后二选一：
 *      · 浏览器 DevTools Console：把这个文件整段粘进去，再敲 `probeLayout().then(console.log, console.error)`
 *      · Playwright（本仓库没有把它做成依赖，用编辑器里那个集成的浏览器即可）：
 *          await page.addScriptTag({ path: 'platform/probe_layout.js' });
 *          await page.evaluate(() => probeLayout());
 *
 * 检查项（一项一个探测器，互不 return，前一项红了后面的照样跑）：
 *   LP-EMPTY        提取为空（没有 main / 没有分组 / 没有指标格 / 没有折线）⇒ 直接报红。
 *                   **这是最重要的一条**：提取失配时后面每个探测器都在空转，
 *                   却会打印出「零问题」——报告看起来比真通过还干净。
 *   LP-OVERFLOW     有元素横向越出视口。
 *   LP-SCROLL       整页出现横向滚动条（scrollWidth > innerWidth）。
 *   LP-OVERLAP      同一父元素下两个可见子元素**相交**（非嵌套）—— 元素压在一起。
 *   LP-CLIP         某元素把内容截掉了（overflow 非 visible 且 scrollWidth > clientWidth）。
 *   LP-ZERO         应当看得见的东西几何为 0（svg / polyline / 分组 / 指标格 / 下拉框）。
 *   LP-API          取服务端下发的那份报告失败 / JSON 里没有 metrics 数组（读不懂 ⇒ 拒绝给结论）。
 *   LP-TEXT-COUNT   页面印的指标格数与服务端下发的条数不一致。
 *   LP-LABEL        行头（指标标签）不是服务端下发的那个串。
 *   LP-TEXT         指标格里的串不是服务端原样下发的那个串（前端偷偷又格式化了一次）。
 *   LP-CURVE-POINTS 折线点数与报告曲线点数不一致，或不足两点却画了线。
 *   LP-CURVE-COUNT  报告里 curve.length 与 counts.equityCurve 对不上。
 * ───────────────────────────────────────────────────────────────────────────── */

async function probeLayout() {
  const problems = [];
  const fail = (code, msg) => problems.push(code + " :: " + msg);
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    return {
      left: Math.round(r.left),
      top: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
      right: Math.round(r.right),
      bottom: Math.round(r.bottom),
    };
  };
  const nameOf = (el) =>
    el.tagName.toLowerCase() +
    (typeof el.className === "string" && el.className.trim()
      ? "." + el.className.trim().split(/\s+/).join(".")
      : "");

  const vw = window.innerWidth;
  const de = document.documentElement;
  const report = {
    viewport: { width: vw, height: window.innerHeight },
    doc: { scrollWidth: de.scrollWidth, scrollHeight: de.scrollHeight },
    extracted: null,
    outside: [],
    overlaps: [],
    clipped: [],
    zeroSized: [],
    server: null,
    curve: null,
    problems,
  };

  const main = document.querySelector("main");
  const groups = main ? [...main.querySelectorAll("section.group")] : [];
  const cells = main ? [...main.querySelectorAll("table td")] : [];
  const headers = main ? [...main.querySelectorAll("table th")] : [];
  const svg = main ? main.querySelector("svg") : null;
  const polyline = main ? main.querySelector("svg polyline") : null;
  const selects = main ? [...main.querySelectorAll("select")] : [];
  report.extracted = {
    groups: groups.length,
    metricCells: cells.length,
    rowHeaders: headers.length,
    svg: !!svg,
    polyline: !!polyline,
    selects: selects.length,
  };
  if (!main) {
    fail(
      "LP-EMPTY",
      "页面里没有 <main> —— 提取失败时后面每个探测器都在空转，拒绝给结论",
    );
    return report;
  }
  if (
    groups.length === 0 ||
    cells.length === 0 ||
    headers.length === 0 ||
    !polyline
  ) {
    fail(
      "LP-EMPTY",
      "提取到 groups=" +
        groups.length +
        " metricCells=" +
        cells.length +
        " rowHeaders=" +
        headers.length +
        " polyline=" +
        !!polyline +
        " —— 报告没加载完或页面结构变了；此时后面的探测器全在空转，所以这里直接报红",
    );
  }

  // ② 横向越界 + 整页横向滚动
  for (const el of main.querySelectorAll("*")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > vw + 1 || r.left < -1)
      report.outside.push(nameOf(el) + " " + JSON.stringify(rectOf(el)));
  }
  if (report.outside.length) {
    fail(
      "LP-OVERFLOW",
      report.outside.length +
        " 个元素横向越出视口（宽 " +
        vw +
        "）：" +
        report.outside.slice(0, 5).join(" | "),
    );
  }
  if (de.scrollWidth > vw + 1) {
    fail(
      "LP-SCROLL",
      "整页横向可滚动：scrollWidth=" + de.scrollWidth + " > innerWidth=" + vw,
    );
  }

  // ③ 兄弟重叠（同父、都可见、相交面积 > 1px²；祖先/后代不算）
  const parents = [
    main,
    ...main.querySelectorAll(
      "div.groups, section.group, table, tbody, dl.meta",
    ),
  ];
  for (const p of parents) {
    const kids = [...p.children].filter((c) => {
      const r = c.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    for (let i = 0; i < kids.length; i++) {
      for (let j = i + 1; j < kids.length; j++) {
        const a = kids[i].getBoundingClientRect();
        const b = kids[j].getBoundingClientRect();
        const ix = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const iy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ix > 1 && iy > 1) {
          report.overlaps.push(
            nameOf(p) +
              " 下 " +
              nameOf(kids[i]) +
              " ∩ " +
              nameOf(kids[j]) +
              " 相交 " +
              Math.round(ix) +
              "×" +
              Math.round(iy) +
              "px",
          );
        }
      }
    }
  }
  if (report.overlaps.length) {
    fail(
      "LP-OVERLAP",
      report.overlaps.length +
        " 处兄弟重叠：" +
        report.overlaps.slice(0, 5).join(" | "),
    );
  }

  // ④ 文本被截掉
  for (const el of main.querySelectorAll("*")) {
    const cs = getComputedStyle(el);
    if (cs.overflowX === "visible" && cs.overflowY === "visible") continue;
    if (el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1) {
      report.clipped.push(
        nameOf(el) +
          " clientWidth=" +
          el.clientWidth +
          " scrollWidth=" +
          el.scrollWidth +
          " 文本=" +
          JSON.stringify((el.textContent || "").trim().slice(0, 30)),
      );
    }
  }
  if (report.clipped.length) {
    fail(
      "LP-CLIP",
      report.clipped.length +
        " 个元素把内容截掉了：" +
        report.clipped.slice(0, 5).join(" | "),
    );
  }

  // ⑤ 应当看得见的东西不能是 0×0
  for (const el of [svg, polyline, ...groups, ...cells, ...selects]) {
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1)
      report.zeroSized.push(nameOf(el) + " " + JSON.stringify(rectOf(el)));
  }
  if (report.zeroSized.length) {
    fail(
      "LP-ZERO",
      report.zeroSized.length +
        " 个应当可见的元素几何为 0：" +
        report.zeroSized.slice(0, 5).join(" | "),
    );
  }

  // ⑥ 与「服务端下发的那份」逐字对拍（页面只许原样呈现）
  const id = selects.length ? selects[0].value : null;
  let view = null;
  try {
    if (id)
      view = await (
        await fetch("/api/reports/" + encodeURIComponent(id))
      ).json();
  } catch (e) {
    fail("LP-API", "取服务端下发的那份报告失败（" + id + "）：" + e);
  }
  if (view && !Array.isArray(view.metrics)) {
    fail(
      "LP-API",
      "服务端下发的 JSON 里没有 metrics 数组 —— 探针读不懂就拒绝给结论",
    );
    view = null;
  }
  if (view) {
    const metrics = view.metrics;
    const curve = Array.isArray(view.curve) ? view.curve : [];
    report.server = {
      id,
      metrics: metrics.length,
      curve: curve.length,
      counts: view.counts,
    };

    const printed = cells.map((td) => td.textContent.trim());
    const expectedText = metrics.map((m) => m.text);
    if (printed.length !== expectedText.length) {
      fail(
        "LP-TEXT-COUNT",
        "页面印了 " +
          printed.length +
          " 格，服务端下发 " +
          expectedText.length +
          " 条",
      );
    } else if (printed.join("\u0001") !== expectedText.join("\u0001")) {
      const bad = printed
        .map((t, i) =>
          t === expectedText[i]
            ? null
            : i +
              "：页面印 " +
              JSON.stringify(t) +
              "，服务端下发 " +
              JSON.stringify(expectedText[i]),
        )
        .filter(Boolean);
      fail(
        "LP-TEXT",
        bad.length + " 格不是原样呈现：" + bad.slice(0, 5).join(" | "),
      );
    }

    const printedLabels = headers.map((th) => th.textContent.trim());
    const expectedLabels = metrics.map((m) => m.label);
    if (
      printedLabels.length === expectedLabels.length &&
      printedLabels.join("\u0001") !== expectedLabels.join("\u0001")
    ) {
      const bad = printedLabels
        .map((t, i) =>
          t === expectedLabels[i]
            ? null
            : i +
              "：页面印 " +
              JSON.stringify(t) +
              "，服务端下发 " +
              JSON.stringify(expectedLabels[i]),
        )
        .filter(Boolean);
      fail(
        "LP-LABEL",
        bad.length + " 个指标标签不是原样呈现：" + bad.slice(0, 5).join(" | "),
      );
    }

    const points =
      polyline && polyline.getAttribute("points")
        ? polyline.getAttribute("points").trim().split(/\s+/).filter(Boolean)
            .length
        : 0;
    report.curve = { points, curvePoints: curve.length, counts: view.counts };
    if (curve.length < 2 && points !== 0) {
      fail(
        "LP-CURVE-POINTS",
        "报告只有 " +
          curve.length +
          " 个点（不够两点）却画了 " +
          points +
          " 个点的折线",
      );
    }
    if (curve.length >= 2 && points !== curve.length) {
      fail(
        "LP-CURVE-POINTS",
        "折线上 " + points + " 个点，报告里 " + curve.length + " 个点",
      );
    }
    if (
      view.counts &&
      typeof view.counts.equityCurve === "number" &&
      curve.length !== view.counts.equityCurve
    ) {
      fail(
        "LP-CURVE-COUNT",
        "curve.length=" +
          curve.length +
          "，counts.equityCurve=" +
          view.counts.equityCurve,
      );
    }
  }

  return report;
}
