# 高保真基准稿 · 回测绩效看板（低保真的 F5）

同目录的 **`dashboard.html`** 就是这份交付物本体：双击就能在浏览器里打开 —— 不需要 JVM、
不需要 `npm`、不连网。本文件是它的**说明书**。

> ⚠️ 这里**不复制**一份代码。代码只有 `dashboard.html` 那一份，由
> `tools/gen_high_fidelity.py` 生成；往这里再贴一份就会立刻开始各自漂移（技能铁律 P4）。

> ℹ️ **同目录现在有两份产物**（2026-10-01 晚追加）：本文件 **§0~§6** 讲的是 `dashboard.html`
> —— 由 `tools/gen_high_fidelity.py` 生成、逐字节照抄**已交付页面**那一屏；**§7** 讲的是
> `design.html` —— 八帧的**皮肤稿**（S0 外壳 + F1~F8 + 状态矩阵 + 追溯表）。
> 两者都不被任何门禁读取，也都**不是**对方的新版本：前者回答「现在是什么」，后者回答
> 「提议长什么样」。**别把 §7 的实测数字搬去 §3** —— 判据不同（前者逐字节对拍真渲染，
> 后者只能人工 + 浏览器实测，因为它没有可对拍的原件）。

## 0. 它是什么 / 不是什么

| 说法 | 成立？ | 依据 |
| --- | --- | --- |
| 它是 `platform/web` 真页面**那一屏**的静态照抄（结构 / class 名 / 文案 / 样式 / 数字） | ✅ | §3 第 1~7 条 |
| 它**没有牙**：不被任何门禁读取，不是规格，不解除任何缺口，**不等于平台层开工** | ✅ | 裁决 Q3 仍然有效；`tools/run_all_gates.py` 里没有任何一条门禁读 `docs/high fidelity/` |
| 它能证明「照抄这一步是机械的、可复核的」 | ✅（**快照式**） | §3 第 4~6 条 |
| 它能证明「页面本身没问题」 | ❌ | 它没跑页面、没起服务，只对着**一次**真渲染的 DOM 快照比过 |
| 它把「布局 / 视觉」那一格填上了 | ❌ | 附录C §C.14 仍然成立：那一格是**零覆盖**。本稿是**给人看的基准**，不是判据 |
| 它替代真页面 | ⚠️ 只在「不起 JVM 也能看这一屏」这个意义上 | 它读不出 REST、切不了报告、没有脚本（0 个 `<script>`） |

它和 `docs/low fidelity/` 那两份的关系：低保真是**提案**（要什么），这一份是**照着已实现的画**
（现在是什么）。两份都不被门禁读取。

## 1. 设计解析摘要（deliverable ①）

### 1.1 版式模型

整页是一条**单列、居中、卡片式**的流。所有取值逐字来自 `platform/web/src/styles.css`（3,032 字节
逐字节内联进产物），不是目测：

| 层 | 规则 | 实测 |
| --- | --- | --- |
| `body` | `margin:0; padding:24px`；`font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif`；`background:#fafbfc` | 底色 `rgb(250, 251, 252)` |
| `main` | `max-width:1000px; margin:0 auto` | 1280 视口下 1000px |
| `header h1` | `font-size:22px; margin:0` | 22px |
| `.sub` | `margin:4px 0 16px; color:var(--muted); font-size:13px` | 13px |
| `.toolbar` | `display:flex; align-items:center; gap:8px; margin-bottom:16px` | label 13px |
| `select` | `padding:5px 8px; border:1px solid var(--line); border-radius:6px; background:#fff; font:inherit; font-size:13px` | 圆角 6px |
| `.meta` | `grid`；`repeat(auto-fit, minmax(180px, 1fr))`；`gap:8px 20px`；`padding:12px 14px`；1px 边框 + 圆角 8px + `#fff` | 列数 **1 / 2 / 4 / 4 / 4**（420/622/900/1280/1600） |
| `.meta div` | `display:flex; gap:8px; font-size:12px` | 12px |
| `.curve` | 与 `.meta` 同款卡片 + `margin-bottom:20px` | — |
| `.curve svg` | `display:block; width:100%; height:180px`（`viewBox="0 0 720 180"` + `preserveAspectRatio="none"`） | 宽 327 / 529 / 807 / 971 / 971 |
| `.curve polyline` | `fill:none; stroke:var(--accent); stroke-width:1.5; vector-effect:non-scaling-stroke` | — |
| `.groups` | `grid`；`repeat(auto-fit, minmax(280px, 1fr))`；`gap:14px` | 列数 **1 / 1 / 2 / 3 / 3** |
| `.group th` | `text-align:left; font-weight:400; color:var(--muted); padding:4px 0` | 表头不做加粗 |
| `.group td.value` | `text-align:right; padding:4px 0; font-variant-numeric:tabular-nums; font-weight:600` | `tabular-nums` 已生效 |
| `.group tr + tr` | `border-top:1px solid var(--line)` | 行间细线 |
| `footer` | `margin-top:24px; padding-top:12px; border-top:1px solid var(--line); font-size:12px; color:var(--muted)` | — |
| `footer .warn` | `color:var(--bad)` | 页脚第三段走警告色 |
| `code` | `Consolas,"Courier New",monospace; font-size:12px; background:#f2f4f7; padding:0 4px; border-radius:4px` | — |

### 1.2 设计 token（照抄 `:root`，一个都没新增）

| token | 值 | 用在哪 |
| --- | --- | --- |
| `--ink` | `#1b1f24` | 正文、以及本稿注释块的竖线 |
| `--muted` | `#6b7280` | 副标题 / 抬头名 / 表头 / 曲线脚注 / 页脚 |
| `--line` | `#e5e7eb` | 全部 1px 分隔线与卡片边框 |
| `--bad` | `#b42318` | `.error` 文字、`footer .warn` |
| `--accent` | `#1f6feb` | 净值折线 |

外加 5 个**字面量**（不在 token 里，照抄也一样）：`body` 的 `#fafbfc`、卡片/选择框的 `#fff`、
`.error` 的 `#fff5f4` + `#f3c0bb`、`code` 的 `#f2f4f7`。

**本稿自己的注释块没有新增任何颜色**：它复用 `--ink`、`--muted`，以及已被照抄样式用过的 `#fff`、
`#f2f4f7`。这条有判据（见 §3 第 2 条）：把注释块里的 hex 集合**减去**被照抄样式的 hex 集合，必须为空。

### 1.3 尺寸与排版

字号 12 / 13 / 14 / 22 px；圆角 4 / 6 / 8 px；卡片内边距 `12px 14px`；间距 8 / 14 / 16 / 20 / 24 px；
数字列右对齐 + `tabular-nums` + `font-weight:600`。

### 1.4 状态：画了什么、没画什么

- **画了**：成功态（`status = SUCCESS`）的那一屏 —— 抬头 7 项、净值曲线 60 点、4 个指标分组共 14 个数、页脚 3 段。
- **没画（真实存在）**：「读取中」`<span class="busy">读取中…</span>`（报告列表为空时选择框 `disabled`，
  此时抬头 / 曲线 / 指标**都不渲染**）；「出错」`<p class="error">…</p>`（渲染被 `view && !error` 挡着，
  **不显示旧数据**）。这两态等于 §4 的缺口，不是「页面没有」。
- **注意**：产物里那个「报告」选择框**是死的** —— 本稿 0 个 `<script>`，切了不会换报告。
  它被照抄下来是为了核对样式与文案，不是摆一个假开关（技能铁律 P5）。
  要能切的那种，看 `docs/low fidelity/prototype.html`。

## 2. 怎么重新生成（deliverable ②）

```powershell
python tools/gen_high_fidelity.py
```

它会：① 从 `platform/web/src/App.jsx` **现折叠**文案（含 JSX 的换行折叠规则）；
② 把 `platform/web/src/styles.css` **逐字节**内联；③ 从 `report-view.fixture.json` 取 14 条指标与
60 个曲线点、拼出整页；④ 跑 15 条内存判据；⑤ **写盘**（CRLF）；⑥ 再拿**磁盘字节**跑 3 条判据；
⑦ 有 `--dump=` 时与真渲染 DOM 逐字符对拍。

它是**工具，不是门禁** —— `tools/run_all_gates.py` 里没有任何一条读它或它生成的 html。

**真源与「源提交」**：`SOURCES` = `platform/web/src/App.jsx`、`platform/web/src/styles.css`、
`platform/web/src/api.js`、`platform/web/test/report-view.fixture.json`。
产物抬头那句「真源提交 `xxxxxxx`」由 `git log -1 --format=%h -- <SOURCES>` 得到，并附
`git status --porcelain -- <SOURCES>` 的结果（有改动就写成「有未提交改动 ⇒ 本稿可能已落后」）。
刻意**不钉 HEAD** —— 否则本稿自己那次提交会被记成「真源提交」。

**造那份对拍用的 DOM dump**（一次性，需要 Node/vitest；本仓库不保留这个测试文件）：

1. 在 `platform/web/test/` 下临时放一个 `// @vitest-environment jsdom` 的用例；
2. 打桩 `fetch`：`/api/reports` 返回 3 条目录（`report-seed7-a` / `report-seed7-b` / `report-seed8`，
   均 `symbol 000001.SZ`、窗口 `2024-01-02T00:00:00`~`2024-03-25T00:00:00`、`bars 60`），
   其余返回 `report-view.fixture.json`；
3. 像 `platform/web/src/main.jsx` 那样挂起来：`createRoot(container).render(<App/>)`（容器是游离的 `div`；
   真页面挂的是 `index.html` 里的 `#root`，而 `index.html` 除了 `#root` 与 `main.jsx` 之外没有别的东西）；
4. `await act(...)` 等一帧，把 `JSON.stringify({html: container.innerHTML}, null, 1)` 写进
   `process.env.HIFI_DUMP` 指的文件；
5. `npm test` 跑它（得到约 5.5 KB 的 JSON）；
6. `python tools/gen_high_fidelity.py --dump=<那个文件>` 看对拍结论；`--selftest` 会额外把本稿
   规范化后的第 200 个字符改成 `X`，证明这份对拍**真的会红**。

三个坑（都踩过）：① vitest 里 `import.meta.url` **不是** `file:` URL，用它算路径会报
`The URL must be of scheme file`；② React 把 `<option>` 的选中写成 **DOM 属性**，`innerHTML` 里
**没有** `selected` ⇒ 本稿也刻意不写它（否则对拍永远不相等）；③ SVG 子元素会被浏览器重新序列化成
`<polyline …></polyline>`（自闭合会被展开）。

## 3. 还原度自查清单（deliverable ③）—— 数字，不是形容词

| # | 判据 | 实测 | 类型 |
| --- | --- | --- | --- |
| 1 | 生成器的内存判据（结构 / 计数 / class 有规则 / token 复用 / 源提交可取得） | **15/15 OK** | 静态 |
| 2 | 写盘后拿**磁盘字节**再核 3 条：等于生成结果（只差 `\n`→`\r\n`）、`styles.css` 3,032 字节逐字节内联、产物是纯 CRLF | **3/3 OK** | 静态 |
| 3 | 与**真 React 渲染**的 DOM 对拍（只比 `<main>` 那一块；两侧同一套空白规范化） | 归一化后 **4,193 / 4,193**，**差异：无** | 快照 |
| 4 | 对拍**有牙**：把规范化后的第 200 个字符改成 `X` | **FAIL 如预期** | 变异 |
| 5 | 浏览器里量 computed style / 结构 / 文案 | **36/36 通过** | 实测 |
| 6 | 文案逐字：14 条指标文本、`.curve-foot`、页脚三段（含 `； 本页`、`是 同一份`、`夹具） 覆盖`、`、 platform-web-parity`、`输入是一份` 这些**单个空格**） | 全部逐字相等 | 实测 |
| 7 | 响应式：5 档视口（420/622/900/1280/1600） | 每档**溢出元素 0 个**、`scrollWidth === clientWidth`；`.groups` 列数 1/1/2/3/3，`.meta` 列数 1/2/4/4/4 | 实测 |
| 8 | 变异：把一格指标 `-3.2043%` 改成 `-3.2044%` | 逐字断言**转假**，且页面仍渲染出 14 格（说明是**内容**失败，不是加载失败） | 变异 |
| 9 | 变异：把 `--ink` 的 `#1b1f24` 改成 `#1b1f25` | token 断言**转假** | 变异 |
| 10 | 干净样本（真产物） | 第 8、9 两条断言都**为真**（防误报） | 对照 |

**环境事实（不是页面缺陷）**：本机是 150% 缩放的显示环境，浏览器把边框宽度对齐到整数**设备**像素
⇒ CSS `1px` 边框的 computed style 读回来是 `0.666667px`。实测 1/2/3/4/5/6 px →
`0.666667 / 2 / 2.66667 / 4 / 4.66667 / 6`（即 `floor(px × 1.5) / 1.5`），而 `window.devicePixelRatio`
在同一环境里读 **≈1.0**（不可用）。所以断言里**不**拿 1px 边框的 computed 值当判据；本稿自己的
2px 虚线、4px 竖线读回来是精确的 `2px` / `4px`。

**没跑的、也跑不了的**：与**真页面**（活着的 Spring 服务）的渲染结果对拍 —— 需要 JVM。
本稿能对着的只有 §3 第 3 条那份**一次性的**快照。

## 4. 能力缺口清单（deliverable ④）

「设计稿想要、但上游/页面现在没有」的东西逐条列在这里。**本稿一行都没自己加**（技能铁律 P2）：

| 缺口 | 现状（取证） | 影响 |
| --- | --- | --- |
| **K 线（OHLC）** | 报告里**没有**每根 bar 的 open/high/low/close：`deterministic.equity_curve[]` 只有 `{cash, drawdown, equity, market_value, timestamp}`，`summary` 只有 `bars: 60`。OHLC 只存在于 `quanauto/models.py` 的 `BarData` 与 `tests/fixtures/sample_prices.csv`，**没进报告** | 这是**报告 schema** 的缺口，页面画不出来；要画得先让报告带 OHLC |
| **买卖点标记** | `deterministic.trades[]` **确实带** `timestamp` / `price` / `direction`（`trades` 3 条、`orders` 3 条） | 数据在，**页面没画** ⇒ 这是**页面**缺口，可以在不改报告的前提下补 |
| **实时 / 流式 / 长任务进度** | 页面只有「读取中」一个瞬时态，没有进度、没有推送、没有取消 | 提案态 |
| **F1~F4、F6~F8** | 仍是提案（见 `docs/low fidelity/`），未实现 | 提案态 |
| **「成交 3 笔」vs「成交笔数 1」** | `summary.trades = 3`（流水条数）与 `performance.total_trades = 1`（平仓口径）来自**同一份报告的两个字段** | **不是 bug、不能「修」**；改它等于发明一个上游没有的事实 |
| **与真页面渲染结果常驻对拍** | 没有活着的 JVM ⇒ 只能一次性快照 | 见 §3 末条 |
| **布局 / 视觉** | 附录C §C.14：仍是零覆盖（jsdom 没有布局引擎 ⇒ 几何断言恒真） | 本稿是**人看的基准**，不是这条的判据 |

## 5. 刻意的差异（产物 vs 真页面）

对拍只比 `<main>` 那一块，所以下面这些差异**不影响** §3 第 3 条，但要说清楚：

| 差异 | 为什么 |
| --- | --- |
| `<title>` 是「回测绩效看板 · 高保真基准稿（按 platform/web 照抄）」，真页面是「…· 平台层 P0 切片」 | 这是**另一份文档**，不是那页 |
| 多了上下两个 `.hifi-note` 注释块与两条 `.hifi-mark` 分隔条 | 说明与免责；用 2px 虚线/4px 竖线把自己和「照抄区」在视觉上分开 |
| 没有 `selected` 属性 | 真渲染的 `innerHTML` 里也没有（React 走 DOM 属性）；不写它，对拍才可能**逐字符相等**。默认选中第一项与真页面一致（已实测 `select.value === "report-seed7-a"`） |
| 0 个 `<script>` | 静态稿；选择框因此是死的（见 §1.4） |
| 产物是 CRLF | 与 `.gitattributes` 的 `* -text`（提交什么字节就检出什么字节）以及被内联的 `styles.css` 一致 ⇒ 「逐字节内联」在**磁盘上**也成立 |

## 6. 一次性的坑（记给下一个人）

1. **手抄文案一定会错，而且没人会告诉你**：初稿是手抄的，`输入是一份`（真值 `输入是一份`）、
   `夹具）覆盖`（真值 `夹具） 覆盖`）、`、以及页脚这份自报清单 本身` 三处的**空格位置**抄错了。
   根因：JSX 把一个多行文本子节点折成**一行**，这一步发生在空白折叠**之前** ⇒ 行边界的空格与
   手抄结果相反。修法不是「抄得更仔细」，而是**让生成器现折叠**（`fold_jsx_text`），并对任何
   它不认识的 `{…}` 表达式直接 `HARNESS-FAIL`。
2. **「绿」要能出声**：`--selftest` 里那条变异（改一个字符 ⇒ 对拍必须转红）就是为了让「差异：无」
   有意义。没有它，`差异：无` 与「根本没在比」长得一模一样。
3. **对拍前先问「比的是哪一块」**：`diff_against_dump` 只取 `<main>…</main>`。本稿的注释块在
   `<main>` 外面，所以改注释**不会**也不会**应该**影响那条结论。
4. **这一份不在 `line-anchors` 的扫描面里**：那条门禁只认**恰好两段**路径的 `docs/*.md`
   （`docs/low fidelity/` 与 `docs/high fidelity/` 都是三段 ⇒ 够不着）。所以这里写不写行号都
   **不会被判红** —— 正因如此更该主动按内容定位：没有判据兜底的地方，漂了也没人报。

---

# 7. `design.html` —— 八帧高保真皮肤稿（2026-10-01 晚追加）

同目录的 **`design.html`** 是这一轮的另一份产物：把 `docs/low fidelity/` 的两份低保真产物
（静态规格板 + 可交互原型）与仓库里**已交付的真实语义**，画成一套**带视觉语言**的界面稿。
视觉语言借自一份外部参考稿（`fst-3end-ui-design.html`，只借风格、不带内容）。

**它和 §0~§6 那份不是一套东西**：`dashboard.html` 是「**照着已交付的画**」（有生成器、
逐字节对拍真渲染）；`design.html` 是「**提议长什么样**」（手写、没有生成器、只能实测核对）。

## 7.0 它是什么 / 不是什么

| 说法 | 成立？ | 依据 |
| --- | --- | --- |
| 它把 S0 外壳 + F1~F8 八帧 + §05 状态矩阵 + §06 追溯与缺口 + §07 边界都画出来了 | ✅ | 9 个 `.frame`（1 个 `<div>` + 8 个 `<article>`）、7 个 `<section class="wrap sec">` |
| 它的视觉语言借自外部参考稿（浅色 `:root` **逐字**取自参考稿） | ✅ | §7.2 |
| 它是**提案稿**：除 F5 外每一帧都盖「提案」章、整帧点划线外框 | ✅ | 浏览器实测：`.delivered` 只有 F5 一个；章与边框逐帧对应 |
| 它**没有牙**：不被任何门禁读取、不是规格/契约、不解除任何缺口、**不等于平台层开工** | ✅ | 裁决 Q3 仍有效；`tools/run_all_gates.py` 里没有任何一条读 `docs/high fidelity/` |
| 它把「布局 / 视觉」那一格填上了 | ❌ | 附录C §C.14 仍成立：那一格仍是**零覆盖**。本稿是**给人看的基准**，不是判据 |
| 它主张除 F5 外任何一帧有实现 | ❌ | 八帧 + S0 外壳都是提案 |
| 它与 `dashboard.html` 逐像素一致 | ❌ | 两者连版式模型都不同（那份是单列 1000px 卡片流） |
| 它替代真页面 | ❌ | 它 0 个网络请求、只读本地文件、读不出 REST、切不了报告 |

## 7.1 设计解析摘要（deliverable ①）

**结构**：`<head>` 里一段「这份文件是什么 / 不是什么」的注释横幅 → 15 节编号 CSS → 内联 SVG
图标 sprite（37 个 `<symbol>`，全稿 184 处 `<use>`，浏览器实测**缺失 0**）→ 粘性模糊顶栏 →
HERO（4 张原则卡）→ §01 Design Tokens → §02 组件条 → §03 应用外壳（S0）→ §04 八帧
（F1~F8）→ §05 状态矩阵 → §06 追溯与缺口 → §07 本稿不主张什么 → 页脚 → 唯一一段
`<script>`（主题切换）。

**版式模型**：`1180px` 居中列（`.wrap`），节与节之间 38px 竖向间距；帧用 `.frame` + 桌面
浏览器窗口壳 `.win`（macOS 三点 + 标题条）呈现；表格一律包在 `.tbl-wrap`/`.spec` 里，
窄屏自己横滚而**不**把页面顶宽。

| 层 | 关键取值 | 实测 |
| --- | --- | --- |
| 列宽 | `.wrap{max-width:1180px;margin:0 auto;padding:0 24px}` | 1440 视口下：所有 `.wrap` 左边界都在同一列 `122 / 1180` |
| 标题 | `h1 34px` / `h2 21px` | 34 / 21 px |
| 帧计数 | 9 个 | `.proposal` 8（点划线框 + 「提案」章）+ `.delivered` 1（F5，实线框 + 「已交付」章） |
| 组件 | `.btn`（7 个变体）/ `.badge`（6）/ `.chip` / `.dot` / `.progress`（4 态）/ `.seg` / `.sw` / `.field` / `.flow`（节点 4 态）/ `.kv` / `.dlrow` / `.rulebox` / `.gap` / `.notes` | — |
| 文件 | `167,162` 字节 / `2,122` 行 / **纯 CRLF**（`lone_lf = 0`）；1 个 `<style>` + 1 个 `<script>`；**0** 个 `https?://`、**0** 个 `@font-face`、**0** 个 `<link>` | — |

**设计 token**：浅色 `:root` 的 26 个颜色 + 4 个圆角 + 字体栈 + 阴影 + 焦点环 **逐字取自参考稿**；
`data-theme="dark"` 那一组同样逐字取自参考稿。（这也解释了为什么本稿的浅色底色是偏冷的
`#EEF2F8`，而不是 `dashboard.html` 的 `#fafbfc` —— 两份稿子**故意**用两套语言。）

**本稿新增的 3 个 token**（其余一个没加）：`--up:#B3372F` / `--down:#0E7A5C`（涨跌色。
参考稿没有这个语义；这里刻意**不**用红涨绿跌的纯色，因为 A 股语义与欧美相反，靠颜色单独
表态最容易读错）+ `--hatch`（斜线阴影，专供「图表占位」一个用途，与低保真的图例对齐）。

**状态**：画了「默认 / 空 / 加载 / 错误 / 锁定·只读」五态矩阵（逐字沿用低保真 §5）；
错误态一律带下一步动作；「没有数」与「数是 0」在视觉上分开（`—` vs `0`）。

## 7.2 怎么重新生成（deliverable ②）—— 这一份**没有生成器**

```powershell
# 没有。直接双击 docs/high fidelity/design.html
```

**这是刻意的，也是代价**：`dashboard.html` 能有生成器，是因为它有一个**可以逐字节照抄的
唯一真源**（`platform/web/src/App.jsx` + `styles.css` + 夹具），还能拿一次真渲染的 DOM 对拍。
本稿是**提案**，提案**没有**可对拍的原件 —— 给它写「生成器」只会把「我以为的设计」编码成
一条更长的假绿。

⇒ 因此本稿的全部保证都落在下节那张**量出来的数**上；要改它就直接改 `design.html`，改完
按 §7.3 重测（尤其**响应式 5 档**与**图标缺失数**）。

## 7.3 还原度自查清单（deliverable ③）—— 全是量出来的

| # | 判据 | 实测 | 类型 |
| --- | --- | --- | --- |
| 1 | 响应式：5 档视口（390 / 768 / 1024 / 1440 / 1920） | 每档 `scrollWidth === clientWidth`，**泄漏元素 0 个** | 实测 |
| 2 | 溢出探针**有牙**：往 `body` 里注一个 2000px 宽的 `<div>` | 同一探针**转红**并列出该元素（`before:false → after:true`） | 变异 |
| 3 | 图标完整性 | 37 个 `<symbol>` / 184 处 `<use>`，**缺失 0**（浏览器实测） | 实测 |
| 4 | a11y 不变量 | `[disabled]` **19** 个**全部**带非空 `title`（0 个漏）；可点按钮恰好 **1** 个（`#themeBtn`，它不是 disabled ⇒ 不需要 `title`）；相邻按钮矩形相交 **0** | 实测 |
| 5 | 主题往返 | light → dark → light：`--brand` `#2F6FED`→`#6C9BFF`→回；body 背景 `rgb(238,242,248)`→`rgb(11,16,30)`→回；`aria-pressed` 同步；太阳/月亮图标显示**互斥**；写入 `localStorage['hf-theme']` | 实测 |
| 6 | F5 的**信息**照已交付页面 | `quanauto/dashboard.py` 的 14 条 `METRIC_SPECS` 标签在 F5 段里**按原序**命中；`report-view.fixture.json` 的 14 条 `text` **逐字**命中 | 脚本核对 |
| 7 | F6 的规则取自真值 | `quanauto/risk.py` 的 6 条 `GLOBAL_RULE_IDS` 全部出现（浏览器实测共 7 个 id，含 `blacklist`） | 实测 |
| 8 | 追溯与交付状态**逐帧**对得上低保真 | 8 帧 `.f-trace` 里的 `§` 编号序列 == 低保真 §6 追溯表同行；交付状态列也逐帧相等（F5=已交付，其余=提案） | 脚本核对 |
| 9 | 不编数 | 无值处：`—` 占 **27** 格；方括号占位符 **47** 种 / **134** 处 | 脚本核对 |
| 10 | 干净/坏样本成对 | 真产物 ⇒ 以上全绿；注入超宽元素 ⇒ 第 1 条**转红**（防「探针没生效」） | 对照 |

**量不了、也没量的**：与**活着的 Spring 服务**渲染结果对拍（需要 JVM）；「布局 / 视觉」的
几何判据（jsdom 没有布局引擎 ⇒ 几何断言恒真，附录C §C.14 那句话仍然成立）。本稿用的是
**真浏览器**（VS Code 集成浏览器）实测，那是一次**取证**，不是常驻判据。

## 7.4 能力缺口清单（deliverable ④）

「设计稿想要、但上游 / 页面现在没有」的东西逐条列在这里。**本稿一行都没自己加**（技能铁律 P2）：

| 缺口 | 现状（取证） | 影响 |
| --- | --- | --- |
| **K 线（OHLC）** | 报告里**没有**每根 bar 的 open/high/low/close（同 §4 第 1 条） | F4 的「结果图」只能画成**斜线阴影占位块**，不是画不出，是**没数据可画** |
| **买卖点标记** | `deterministic.trades[]` 带 `timestamp`/`price`/`direction`（同 §4 第 2 条） | 数据在、页面没画 ⇒ 页面缺口 |
| **F1~F4、F6~F8 与 S0 外壳** | 仍是提案 | `platform/web/src/App.jsx` 只有 `<header>` + 一条报告选择工具条，**没有侧栏、没有常驻顶栏、没有环境徽标位** ⇒ S0 那一整套外壳目前只存在于本稿与低保真里 |
| **「成交 3 笔」vs「成交笔数 1」** | 同一份报告的两个字段（同 §4 第 5 条） | 不是 bug、不能「修」 |
| **实时 / 流式 / 长任务进度** | 只有「读取中」一个瞬时态 | 提案态 |
| **布局 / 视觉** | 附录C §C.14：仍是零覆盖 | 本稿是**人看的基准**，不是这条的判据 |
| **参考稿里本稿**没**移植的** | toast（需要真交互）、模态框、移动端窄屏帧、`@keyframes ping` 呼吸点 | 本稿是静态规格稿；要这些看 `docs/low fidelity/prototype.html` |
| **与活着的服务对拍** | 需要 JVM | 只能一次性取证 |

## 7.5 一次性的坑（本轮实测，记给下一个人）

1. **「画得像已交付」是最容易被当成事实引用的假主张**：S0 外壳初版盖的是「已交付 · 结构」章、
   用的是实线框 —— 而去看 `platform/web/src/App.jsx` 才发现**根本没有侧栏**。
   ⇒ 通法：**对上「已交付」这三个字之前，先去读那份真源文件**；图好看不构成证据。
2. **CSS 简写会清掉同一元素上另一个类的长写属性**：`.sec{padding:38px 0 4px}` 把同时挂在
   该元素上的 `.wrap{padding:0 24px}` 的左右内边距一起清了 ⇒ 整节贴到视口边。
   改成 `padding-top` / `padding-bottom` 两个长写。
3. **`min-width:0` 是「表格把页面顶爆」的单一修法**：grid / flex 子项默认 `min-width:auto`，
   表头或长文本的 min-content 宽度会把容器撑破 ⇒ 390px 视口下相撞出 30+ 个越界元素。
   给 grid 子项加 `min-width:0` + 让 `.spec` / `.tbl-wrap` 自己 `overflow-x:auto` + 顶栏
   `flex-wrap:wrap`，5 处小改之后 390px 干净。
4. **溢出探针必须带负样本**：往页面里注一个 2000px 的元素，探针**必须**转红。
   否则「泄漏 0 个」和「探针根本没生效」在报告里长得一模一样。
5. **追加式写文件仍要检查尾部标签是否已存在**：本轮出现过**重复的 `</body></html>`** ——
   浏览器一点看不出来，磁盘上是错的。追加完必须数一遍。
6. **「我以为存在的通用类」往往不存在**：裸 `class="mono"` 用了三处，而样式表里只有
   `.tbl td.mono` / `.win-title.mono` ⇒ 那三处等于没生效；同类还有 `.btn:disabled` 写了两条。
   ⇒ 用之前先 `grep` 一次：**没有判据兜底的 CSS，写错了不会有任何声音**。
7. **同一份文件的段落分批写，容器类会静默丢失**：§05~§07 少了 `wrap` ⇒ 只有到 1440 视口量
   子元素左边线才看得出来（前三节在 122，后三节在 0）。收尾时**逐节量一遍对齐**。
8. **本文件是三段路径，不在 `line-anchors` 的扫描面里**（同 §6 第 4 条）⇒ 这里写不写行号都
   不会被判红；正因如此更该按内容定位。

