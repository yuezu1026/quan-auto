/**
 * 页面渲染测试 —— 这个仓库里**唯一**把页面真的挂起来的地方。
 *
 * 为什么非要有这一层：平台层另外四条门禁（`platform-spec-parity` / `platform-runtime` /
 * `platform-text-parity` / `platform-web-parity`）**都只读源码**，没有一条会挂载这个组件。
 * 于是「页面上到底出现了什么」在 2026-10-01 之前是**零覆盖**的 —— 一条读源码的判据
 * 挡不住「数字格式化漏到前端」「指标行没印服务端给的 text」「空目录时页面一片空白
 * 却不吱声」这三类**只有渲染出来才看得见**的缺陷。
 *
 * 输入取自**真报告**：`test/report-view.fixture.json` 是 `.rounds/i1/report-seed7-a.json`
 * 经过服务端那套投影（`ReportProjection`）+ `quanauto.dashboard.format_metric` 现算出来的，
 * 键名与 `ReportView.java` 的记录组件逐一对齐（`tools/verify_platform_web.py` 的
 * `PW-FIXTURE-KEYS` 盯着这件事，少一个/多一个都报）。
 *
 * ⚠️ 本文件**故意不写**任何基于 `getBoundingClientRect()` 的判据。jsdom 没有布局引擎，
 * 任何元素的 rect 恒为 `0 × 0`（见下面那条 witness 用例），而「矩形恒为 0 时没有元素
 * 溢出视口、没有元素互相重叠」是**恒真**的话 —— 那种绿是假绿，它证明不了任何视觉性质。
 * 布局 / 视觉仍然只有真浏览器能判，仍然零覆盖。**别往这个文件里加矩形断言。**
 */

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.jsx";
import reportView from "./report-view.fixture.json";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

/** `/api/reports` 的响应形状见 `ReportController.ReportList`。 */
const CATALOGUE = {
  reports: [
    {
      id: "report-seed7-a",
      symbol: "000001.SZ",
      windowStart: "2024-01-02T00:00:00",
      windowEnd: "2024-03-25T00:00:00",
      bars: 60,
      error: null,
    },
    {
      id: "report-seed8",
      symbol: "000001.SZ",
      windowStart: "2024-01-02T00:00:00",
      windowEnd: "2024-03-25T00:00:00",
      bars: 60,
      error: null,
    },
  ],
};

let container = null;
let root = null;

function jsonResponse(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

/** 按 URL 分派假 fetch。`/api/reports` 与 `/api/reports/<id>` 必须分开 —— 混成一条
 * 会让「列表读到了但报告读坏了」这类分叉根本走不到。 */
function stubFetch(handler) {
  const calls = [];
  const stub = vi.fn(async (url) => {
    calls.push(url);
    const reply = handler(url, calls.length);
    if (reply instanceof Error) throw reply;
    return reply;
  });
  vi.stubGlobal("fetch", stub);
  return calls;
}

/** 列表两份都能读；报告的正文一律用那份真夹具。 */
const readable = (url) =>
  url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(reportView);

async function renderInto(element) {
  if (root) {
    const previousRoot = root;
    const previousContainer = container;
    root = null;
    container = null;
    await act(async () => {
      previousRoot.unmount();
    });
    previousContainer.remove();
  }
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(element);
  });
  return container;
}

function mount(handler) {
  stubFetch(handler);
  return renderInto(<App />);
}

function metricCells(el) {
  return [...el.querySelectorAll("td.value")].map((cell) => cell.textContent);
}

function metaRows(el) {
  return [...el.querySelectorAll(".meta > div")].map((div) => ({
    label: div.querySelector("dt").textContent,
    value: div.querySelector("dd").textContent,
  }));
}

// ── 两条核心判据 ────────────────────────────────────────────────────────────
// 抽成函数是为了**下面那条触发测试**：拿一个故意做错的页面来证明它们真的会红。
// 只跑「真页面全绿」是证明不了判据有牙的 —— 判据写错成恒真时，报告比真通过还干净。

/** 屏幕上那 14 个格子，逐字等于服务端算好的 text，个数不多不少。 */
function assertMetricsPrintedVerbatim(el) {
  const cells = metricCells(el);
  const want = reportView.metrics.map((metric) => metric.text);
  if (cells.length !== want.length) {
    throw new Error("指标行数 " + cells.length + " ≠ 服务端下发的 " + want.length + " 条");
  }
  cells.forEach((got, index) => {
    if (got !== want[index]) {
      throw new Error(
        "第 " + (index + 1) + " 行不是服务端给的 text：" + JSON.stringify(got) + " ≠ " + JSON.stringify(want[index]),
      );
    }
  });
}

/** 报告里的原值不许以任何形式（正文 / 属性）出现在页面上。 */
function assertNoRawValues(el) {
  const html = el.innerHTML;
  // 只挑**能认出来的**原值：`String(0)` 是 `"0"`，而页面上到处都是 `0`
  // （60 点、3 笔、2024 年…）—— 拿它当判据必然误报，那种断言等于没写。
  const distinctive = reportView.metrics.filter(
    (metric) => String(metric.value) !== metric.text && String(metric.value).includes("."),
  );
  if (distinctive.length === 0) {
    throw new Error("防空转：夹具里没有一个可辨认的原值，这条判据整段都在空转");
  }
  for (const metric of distinctive) {
    const raw = String(metric.value);
    if (html.includes(raw)) {
      throw new Error(metric.key + " 把报告原值 " + raw + " 印到页面上了");
    }
  }
}

afterEach(async () => {
  if (root) {
    const previousRoot = root;
    root = null;
    await act(async () => {
      previousRoot.unmount();
    });
  }
  if (container) {
    container.remove();
    container = null;
  }
  vi.unstubAllGlobals();
});

describe("回测绩效看板页面（真渲染）", () => {
  it("14 条指标逐字印服务端给的 text，且报告里的原值一个都不许露出来", async () => {
    const el = await mount(readable);
    expect(() => assertMetricsPrintedVerbatim(el)).not.toThrow();
    expect(() => assertNoRawValues(el)).not.toThrow();
    // 正面对照，免得两条判据因为选择器写错而空转（提取为空时会一起变绿）。
    expect(metricCells(el)).toEqual(reportView.metrics.map((metric) => metric.text));
  });

  it("触发测试：在前端重算/多印数字的实现必须被上面那两条判据判红", async () => {
    // 做错法一：把报告原值直接印出来（既没换算量纲，也没定点）。
    const rawPage = await renderInto(
      <table>
        <tbody>
          {reportView.metrics.map((metric) => (
            <tr key={metric.key}>
              <th>{metric.label}</th>
              <td className="value">{String(metric.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>,
    );
    expect(() => assertNoRawValues(rawPage)).toThrow(/原值/);
    expect(() => assertMetricsPrintedVerbatim(rawPage)).toThrow(/text/);

    // 做错法二：在前端自己格式化（.toFixed 一刀切，不看量纲）——
    // 这正是被 platform-web-parity 的 PW-FORMAT-LEAK 盯着的那个动作，
    // 而它印出来的字符串在 percent 类上**恰好**蒙对，所以必须有第二条判据兜着。
    const selfFormatted = await renderInto(
      <table>
        <tbody>
          {reportView.metrics.map((metric) => (
            <tr key={metric.key}>
              <th>{metric.label}</th>
              <td className="value">{metric.value.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>,
    );
    expect(() => assertMetricsPrintedVerbatim(selfFormatted)).toThrow(/text/);

    // 干净样本：一个原样印 text 的最小实现不该被这两条判据判红（防误报）。
    const correct = await renderInto(
      <table>
        <tbody>
          {reportView.metrics.map((metric) => (
            <tr key={metric.key}>
              <th>{metric.label}</th>
              <td className="value">{metric.text}</td>
            </tr>
          ))}
        </tbody>
      </table>,
    );
    expect(() => assertMetricsPrintedVerbatim(correct)).not.toThrow();
    expect(() => assertNoRawValues(correct)).not.toThrow();
  });

  it("分组顺序跟着服务端下发的那份数据走（前端没有手抄的分组清单）", async () => {
    const shuffled = { ...reportView, metrics: [...reportView.metrics].reverse() };
    const el = await mount((url) =>
      url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(shuffled),
    );

    const expected = [];
    for (const metric of shuffled.metrics) {
      if (!expected.includes(metric.group)) expected.push(metric.group);
    }
    expect([...el.querySelectorAll(".group h3")].map((h) => h.textContent)).toEqual(expected);

    // 每一组里的行 = 这一组的指标，行序不变，且行的 title 就是指标键。
    for (const section of el.querySelectorAll(".group")) {
      const group = section.querySelector("h3").textContent;
      expect([...section.querySelectorAll("th")].map((th) => th.getAttribute("title"))).toEqual(
        shuffled.metrics.filter((metric) => metric.group === group).map((metric) => metric.key),
      );
    }
  });

  it("报告目录为空时说「先跑一遍回测」，而不是渲染一张看着像成功的空表", async () => {
    const el = await mount(() => jsonResponse({ reports: [] }));

    expect(el.querySelector(".error").textContent).toContain("报告目录里没有 .json 报告");
    expect(el.querySelector("select").disabled).toBe(true);
    expect(metricCells(el)).toEqual([]);
    expect(el.querySelector(".meta")).toBeNull();
  });

  it("列表里读不出来的那份标出来，并自动选中第一份能读的", async () => {
    const broken = {
      id: "report-broken",
      symbol: null,
      windowStart: null,
      windowEnd: null,
      bars: null,
      error: "报告里没有 deterministic 段",
    };
    const el = await mount((url) =>
      url === "/api/reports"
        ? jsonResponse({ reports: [broken, CATALOGUE.reports[1]] })
        : jsonResponse(reportView),
    );

    expect([...el.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "report-broken（读不出来）",
      "report-seed8",
    ]);
    expect(el.querySelector("select").value).toBe("report-seed8");
    expect(el.querySelector(".error")).toBeNull();
    expect(metricCells(el)).toEqual(reportView.metrics.map((metric) => metric.text));
  });

  it("报告本身读不出来时原样显示后端那句话，且把上一份的表格收回去", async () => {
    const message =
      "报告 performance 段缺 3 个指标：sharpe_ratio, sortino_ratio, calmar_ratio —— 看板不替上游补默认值";
    const el = await mount((url) =>
      url === "/api/reports"
        ? jsonResponse(CATALOGUE)
        : url.endsWith("report-seed8")
          ? jsonResponse({ error: message }, 422)
          : jsonResponse(reportView),
    );

    // 先让页面正常渲染一份，再切到坏报告 —— 这样才真的走到 setView(null) 那一支
    // （从没渲染过就报错的话，view 本来就 null，看不出「收回去」有没有做）。
    expect(metricCells(el)).toHaveLength(reportView.metrics.length);
    const select = el.querySelector("select");
    await act(async () => {
      select.value = "report-seed8";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(el.querySelector(".error").textContent).toBe(message);
    expect(el.querySelector(".meta")).toBeNull();
    expect(metricCells(el)).toEqual([]);
  });

  it("后端没起（fetch 直接抛）时说清是连不上，不说报告有问题", async () => {
    const el = await mount(() => new TypeError("Failed to fetch"));
    expect(el.querySelector(".error").textContent).toContain("连不上后端");
    expect(el.querySelector(".error").textContent).toContain("/api/reports");
  });

  it("挂载中就说「读取中…」，别让页面看着像卡死；读完就不再说", async () => {
    let release;
    const gate = new Promise((resolve) => {
      release = () => resolve(jsonResponse(CATALOGUE));
    });
    let first = true;
    const el = await mount(() => {
      if (first) {
        first = false;
        return gate;
      }
      return jsonResponse(reportView);
    });

    expect(el.textContent).toContain("读取中…");
    await act(async () => {
      release();
    });
    expect(el.textContent).not.toContain("读取中…");
    expect(metricCells(el)).toEqual(reportView.metrics.map((metric) => metric.text));
  });

  it("净值曲线逐点照抄；全平时画一条中线（既不是 NaN 也不是贴地）", async () => {
    const el = await mount(readable);
    const points = el
      .querySelector("svg polyline")
      .getAttribute("points")
      .trim()
      .split(/\s+/);
    expect(points).toHaveLength(reportView.curve.length);
    for (const point of points) {
      expect(point).toMatch(/^-?\d+\.\d\d,-?\d+\.\d\d$/); // 每个坐标两位小数
    }
    // 曲线上的点与报告里的点一一对应：equity 单调不减的那一段，y 必须单调不增
    const ys = points.map((point) => Number(point.split(",")[1]));
    const equities = reportView.curve.map((item) => item.equity);
    const firstRise = equities.findIndex((equity, index) => index > 0 && equity > equities[index - 1]);
    if (firstRise > 0) {
      expect(equities[firstRise]).toBeGreaterThan(equities[firstRise - 1]);
      expect(ys[firstRise]).toBeLessThanOrEqual(ys[firstRise - 1]);
    }

    // 全平：span === 0 ⇒ ratio 取 0.5（横中线），不是 0 也不是 NaN
    const flat = {
      ...reportView,
      curve: reportView.curve.map((item) => ({ ...item, drawdown: 0, equity: 100000 })),
    };
    const flatEl = await mount((url) =>
      url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(flat),
    );
    const flatYs = flatEl
      .querySelector("svg polyline")
      .getAttribute("points")
      .trim()
      .split(/\s+/)
      .map((point) => Number(point.split(",")[1]));
    expect(new Set(flatYs).size).toBe(1);
    expect(Number.isFinite(flatYs[0])).toBe(true);
  });

  it("曲线不足两个点时 points 是空串（画不出线，也不许画出乱线）", async () => {
    const single = { ...reportView, curve: [reportView.curve[0]] };
    const el = await mount((url) =>
      url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(single),
    );
    expect(el.querySelector("svg polyline").getAttribute("points")).toBe("");
    expect(el.querySelector(".curve-foot").textContent).toContain("1 个点");
  });

  it("报告里缺席的字段显示「—」，不是 0、不是空串、不是 undefined", async () => {
    const sparse = {
      ...reportView,
      strategyVersion: null,
      dataVersion: null,
      status: null,
      symbol: null,
      window: null,
      counts: { equityCurve: null, trades: null, orders: null },
    };
    const el = await mount((url) =>
      url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(sparse),
    );

    expect(metaRows(el)).toEqual([
      { label: "报告", value: "report-seed7-a" },
      { label: "标的", value: "—" },
      { label: "窗口", value: "—" },
      { label: "状态", value: "—" },
      { label: "策略", value: "ma-cross / —" },
      { label: "数据版本", value: "—" },
      { label: "结构", value: "曲线 — 点 · 成交 — 笔 · 委托 — 条" },
    ]);
    expect(el.textContent).not.toContain("undefined");
    expect(el.textContent).not.toContain("NaN");
  });

  it("窗口不是两个端点时，页脚也不拼出半个区间", async () => {
    const half = { ...reportView, window: ["2024-01-02T00:00:00"] };
    const el = await mount((url) =>
      url === "/api/reports" ? jsonResponse(CATALOGUE) : jsonResponse(half),
    );
    expect(el.querySelector(".curve-foot").textContent).not.toContain("→");
    expect(el.querySelector(".curve-foot").textContent).toContain("个点");
  });

  it("见证：jsdom 没有布局引擎 —— 所以这个文件里不许出现矩形判据", async () => {
    const el = await mount(readable);
    const rect = el.querySelector(".toolbar").getBoundingClientRect();

    // 视口存在（1024 宽）而几何恒为 0：这就是「拿 rect 判溢出/重叠」在本环境里
    // 只会得到恒真结论的原因。写下这条，是为了让下一个想加视觉断言的人先看见它。
    expect(window.innerWidth).toBe(1024);
    expect(rect.width).toBe(0);
    expect(rect.height).toBe(0);
    expect(el.querySelector(".toolbar").offsetHeight).toBe(0);
  });
});
