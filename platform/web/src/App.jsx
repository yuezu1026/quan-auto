import { useEffect, useMemo, useState } from "react";
import { listReports, loadReport } from "./api.js";

// 曲线的画布尺寸。SVG 用 viewBox + preserveAspectRatio="none" 拉伸，
// 因此这两个数是「坐标空间」而不是像素，屏幕宽度变化不需要重算。
const CURVE_WIDTH = 720;
const CURVE_HEIGHT = 180;
const CURVE_PAD = 8;

/**
 * 把净值曲线映射成 SVG 的 points 字符串。
 *
 * 这是本文件里**唯一的算术**，而且它的产物只进 SVG 的几何属性，不进屏幕上的任何文字。
 * 净值全平（span === 0）时画一条水平中线，而不是除以 0。
 */
function curvePoints(curve) {
  if (!curve || curve.length < 2) {
    return "";
  }
  const values = curve.map((point) => point.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const innerWidth = CURVE_WIDTH - CURVE_PAD * 2;
  const innerHeight = CURVE_HEIGHT - CURVE_PAD * 2;
  return curve
    .map((point, index) => {
      const x = CURVE_PAD + (index * innerWidth) / (curve.length - 1);
      const ratio = span === 0 ? 0.5 : (point.equity - min) / span;
      const y = CURVE_PAD + (1 - ratio) * innerHeight;
      return x.toFixed(2) + "," + y.toFixed(2);
    })
    .join(" ");
}

function Meta({ view }) {
  const window = view.window && view.window.length === 2 ? view.window : null;
  return (
    <dl className="meta">
      <div>
        <dt>报告</dt>
        <dd>{view.id}</dd>
      </div>
      <div>
        <dt>标的</dt>
        <dd>{view.symbol || "—"}</dd>
      </div>
      <div>
        <dt>窗口</dt>
        <dd>{window ? window[0] + " → " + window[1] : "—"}</dd>
      </div>
      <div>
        <dt>状态</dt>
        <dd>{view.status || "—"}</dd>
      </div>
      <div>
        <dt>策略</dt>
        <dd>
          {(view.strategyId || "—") + " / " + (view.strategyVersion || "—")}
        </dd>
      </div>
      <div>
        <dt>数据版本</dt>
        <dd>{view.dataVersion || "—"}</dd>
      </div>
      <div>
        <dt>结构</dt>
        <dd>
          曲线 {view.counts.equityCurve ?? "—"} 点 · 成交{" "}
          {view.counts.trades ?? "—"} 笔 · 委托 {view.counts.orders ?? "—"} 条
        </dd>
      </div>
    </dl>
  );
}

function Metrics({ view }) {
  // 分组顺序**从服务端给的数据里推导**，不在前端另抄一份分组清单 ——
  // 抄一份就等于多一个会漂的地方（而这一层没有门禁）。
  const groups = useMemo(() => {
    const order = [];
    for (const metric of view.metrics) {
      if (!order.includes(metric.group)) {
        order.push(metric.group);
      }
    }
    return order;
  }, [view]);

  return (
    <div className="groups">
      {groups.map((group) => (
        <section key={group} className="group">
          <h3>{group}</h3>
          <table>
            <tbody>
              {view.metrics
                .filter((metric) => metric.group === group)
                .map((metric) => (
                  <tr key={metric.key}>
                    <th scope="row" title={metric.key}>
                      {metric.label}
                    </th>
                    {/* 屏幕上这个数字 = 服务端算好的 text，原样呈现。
                        前端不换算量纲、不决定小数位、不重算任何指标。 */}
                    <td className="value">{metric.text}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}

function Curve({ view }) {
  const points = curvePoints(view.curve);
  const window = view.window && view.window.length === 2 ? view.window : null;
  return (
    <section className="curve">
      <h3>净值曲线</h3>
      <svg
        viewBox={"0 0 " + CURVE_WIDTH + " " + CURVE_HEIGHT}
        preserveAspectRatio="none"
        role="img"
        aria-label="净值曲线"
      >
        <polyline points={points} />
      </svg>
      <p className="curve-foot">
        {window ? window[0] + " → " + window[1] : ""} · {view.curve.length}{" "}
        个点（逐点照抄报告， 不插值不平滑）
      </p>
    </section>
  );
}

export default function App() {
  const [reports, setReports] = useState([]);
  const [selected, setSelected] = useState(null);
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    listReports()
      .then((body) => {
        if (cancelled) return;
        const list = body.reports || [];
        setReports(list);
        const firstReadable = list.find((item) => !item.error) || list[0];
        setSelected(firstReadable ? firstReadable.id : null);
        if (list.length === 0) {
          setError(
            "报告目录里没有 .json 报告。先跑一遍回测：backtest --out .rounds/i1/xxx.json",
          );
          setBusy(false);
        }
      })
      .catch((cause) => {
        if (cancelled) return;
        setError(cause.message);
        setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selected) return undefined;
    let cancelled = false;
    setBusy(true);
    setError(null);
    loadReport(selected)
      .then((body) => {
        if (cancelled) return;
        setView(body);
        setBusy(false);
      })
      .catch((cause) => {
        if (cancelled) return;
        setView(null);
        setError(cause.message);
        setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <main>
      <header>
        <h1>回测绩效看板</h1>
        <p className="sub">
          平台层 P0 切片 · Spring Boot + React · 只读数，不算数
        </p>
      </header>

      <div className="toolbar">
        <label htmlFor="report">报告</label>
        <select
          id="report"
          value={selected || ""}
          onChange={(event) => setSelected(event.target.value)}
          disabled={reports.length === 0}
        >
          {reports.map((item) => (
            <option key={item.id} value={item.id}>
              {item.id}
              {item.error ? "（读不出来）" : ""}
            </option>
          ))}
        </select>
        {busy ? <span className="busy">读取中…</span> : null}
      </div>

      {error ? <p className="error">{error}</p> : null}

      {view && !error ? (
        <>
          <Meta view={view} />
          <Curve view={view} />
          <Metrics view={view} />
        </>
      ) : null}

      <footer>
        <p>
          每个绩效数字都由服务端算好（量纲换算 + 定点小数）后随 REST
          一起下发，本页原样呈现； 本页不做指标口径的加工，也不重算任何指标。
        </p>
        <p>
          数据来源：后端直接读取回测报告文件（默认 <code>.rounds/i1</code>），与
          I4 命令行看板是 同一份报告的两个读法。
        </p>
        <p className="warn">
          本层有自建门禁了（<code>platform-spec-parity</code> 管声明层、
          <code>platform-runtime</code> 管运行层、
          <code>platform-text-parity</code> 管两侧显示规则，三条都在 CI 里跑）
          —— 但它们都<b>不起服务、不渲染页面</b> ⇒ <b>这个页面本身</b>（渲染 /
          布局 / 视觉） 仍然没有常驻判据。 ⚠️ 订正（2026-10-01）：
          <b>两侧展示文本是否一致</b>已不是零覆盖项（那是{" "}
          <code>platform-text-parity</code> 的活）；仍然只有手动脚本{" "}
          <code>platform/check_text_parity.py</code>（需要活着的 JVM ⇒
          不能当门禁）的是
          <b>活着的服务端到底下发了什么</b>。 见{" "}
          <code>docs/智能量化交易平台.md</code> 附录C §C.8 / §C.9。
        </p>
      </footer>
    </main>
  );
}
