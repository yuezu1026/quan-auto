# `platform/` —— 回测绩效看板（平台层 P0 切片）

> **这一层现在有两条门禁**（2026-09-30 订正：原来的「运行侧仍然没有」已过期）。
> `platform-spec-parity`（`tools/verify_platform_specs.py`，tier-A）把 Java 侧
> `MetricSpec.METRIC_SPECS` 与 Python 侧 `quanauto/dashboard.py` 的 `METRIC_SPECS`
> **双向**比对（14 行的键/分组/标签/量纲/小数位 + **行序** + 量纲与分组常量 + `STRUCTURE_COUNTS`）。
> `platform-runtime`（`tools/verify_platform_build.py`，tier-A）在**一次性沙箱**里按
> `npm ci` → `npm run build` → `mvn test` **真跑一遍**——**这个顺序本身是判据**（见下「跑起来」）。
> 两条都接在 `tools/run_all_gates.py` 的统一入口里 ⇒ 而 `ci.yml` 最后一步就是那条命令
> ⇒ **两条都真的在 CI 上跑**（代价是 CI 里必须装 JDK 21 与 Node，见 `ci.yml`）。
> `platform-runtime` 探不到工具链时退 **3 = SKIPPED**，harness 把 3 记成「**不算绿**」。
> 门禁数现取 `python tools/run_all_gates.py --list`，不要在文档里抄。
> ⚠️ 两条都**不启动 JVM、不渲染页面** ⇒ **页面本身**仍然没有任何门禁盯着；
> 两侧**展示文本**也只有手动脚本（见下）。分工的出处是 `docs/智能量化交易平台.md` 附录C 的
> §C.5 与它的 C.8 / C.9 两个订正块。
>
> 本层**不改动研究层的任何文件**（`quanauto/dashboard.py` 一行未改），**不碰 `db/*.sql`**，
> **不碰 `.github/workflows/ci.yml`**。它只是同一份回测报告的**另一个读法**。

## 这一层做什么

把 `backtest --out` 写出的报告 JSON，用 Spring Boot 只读地投成 REST，再用一个 React 单页显示。

```
.rounds/i1/*.json  ──读文件──>  api (Spring Boot, 只读)  ──REST──>  web (React)
        └──────────────── 同一份报告 ────────────────┘
quanauto/dashboard.py ── 研究层的另一个读法（未改动）
```

**后端一个指标都不重算**：它只把报告里的 `deterministic.performance` / `equity_curve` /
结构计数透传，并算出屏幕上要显示的 `text`（量纲换算 + 定点小数）。前端连格式化都不做，
只把服务端给的 `text` 原样印出来。两侧的展示规则与 `quanauto/dashboard.py` 的 `format_metric`
同款 —— 但那张 14 条规格表在 Java 侧是**手抄的副本**，两侧**声明**是否一致由
`platform-spec-parity` 门禁双向比对（附录C §C.5 与它的 C.8 订正块）。
⚠️ 门禁**不管**「服务端算出的 `text` 有没有真的印到页面上」—— 那仍然只有手动脚本
`check_text_parity.py`（需要一个活着的 JVM ⇒ 不能当门禁）。

## 目录

| 路径 | 内容 |
| --- | --- |
| `pom.xml` | 聚合 POM。`java.version=21`、Spring Boot `3.5.16` |
| `api/` | Spring Boot 只读 REST + 静态资源托管 |
| `web/` | React 19 + Vite 8 单页 |
| `check_text_parity.py` | **不是门禁**的对拍脚本（见下） |

## 环境要求（**踩过的坑，别重踩**）

- **Maven 用的是 `JAVA_HOME`，不是 PATH 里的 `java`。** 本机 `JAVA_HOME=C:\Program Files\Zulu\zulu-21`，
  而 PATH 里的 `java` 是 25.x ⇒ `mvn` 能编 21 编不了 25。`pom.xml` 里因此把
  `<java.version>` 钉成 **21**；改成 25 会直接报 `错误: 不支持发行版本 25`。
- **Spring Boot 父版本是 3.5.x 而不是 4.x，是刻意的**：3.5 用 Jackson **2.x**（`com.fasterxml.jackson.*`），
  4.x 换到 Jackson 3（`tools.jackson.*`）且改成非受检异常 ⇒ 是另一套 API。
- 本机没有 `gradle`，只用 Maven。Node `v25` / npm `11`。

## 跑起来

**顺序要紧**（**现在这条顺序有机器判据了**：`platform-runtime` 门禁会在沙箱里按这个次序真跑，
并把「`mvn test` 之前 `target/classes/static/` 不为空」或「之后没出现该有的产物」报成
`PB-STITCH-PREEXISTING` / `PB-STITCH-MISSING`）：前端构建的产物写进 `api/src/main/resources/static/`，
而 Spring 是从 `classpath:/static/`（= `target/classes/static/`）托管的，
所以**先 build 前端，再启动后端**；先起后端再 build 的话，served 的那份是旧的。
⚠️ 这个错**不会**让任何一条 Java 测试变红 —— 所以人工跑的时候尤其容易漏掉。

```powershell
# 1) 后端测试
mvn -B -f platform/pom.xml test

# 2) 前端依赖 + 构建（产物落到 api/src/main/resources/static/）
cd platform/web
npm install
npm run build
cd ../..

# 3) 起后端（会把 static/ 复制进 target/classes 并托管在 /）
mvn -B -f platform/pom.xml -pl api spring-boot:run
# 然后浏览器打开 http://localhost:8080/
```

端口与报告目录在 `api/src/main/resources/application.yml`：
`server.port=8080`、`quanauto.reports.dir=.rounds/i1`（相对路径按**仓库根**解析，
用 `-Dquanauto.reports.dir=` 或环境变量 `QUANAUTO_REPORTS_DIR` 覆盖）。

开发前端时用 `npm run dev`（5173，`/api` 代理到 8080）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | `status` / `reportsDir` / `reportCount` / `metricCount` |
| `GET` | `/api/reports` | `{"reports":[{id, symbol, windowStart, windowEnd, bars, error}]}` |
| `GET` | `/api/reports/{id}` | 完整的 `ReportView`（字段清单见附录C §C.3） |
| `GET` | `/` | 前端页面 |

错误码：**404** = 换错了 URL（报告不存在）；**422** = 这份报告本身读不出来。
两者刻意分开 —— 合成一个的话，界面就说不清「换个报告试试」和「这份报告坏了」。

**响应体里不会有本机路径**（2026-09-30 修，并已由常驻测试钉住）：下发的永远是
`DashboardException.clientMessage()` 那一句（例如 `{"error":"没有这份报告：nope"}`），
带目录的详细原话（`…（目录 D:\…\.rounds\i1）`）只进**服务端日志**。
⚠️ 改之前不是这样：handler 的类注释写着「不回内部路径」，而实现把**详细消息**原样吐给了浏览器。
详细经过与两处证伪登记在 `docs/智能量化交易平台.md` 附录C **§C.10**。
钉住它的是 Java 侧 `ApiExceptionHandlerTest`，而 `platform-runtime` 门禁**真的跑** `mvn test`
⇒ 这条是**常驻牙**，不是靠人记得。

**没有写入侧**：不提供写回、上传、重算端点。任何一个这样的口子都会让
「屏幕上每个数都来自同一份报告」失效。

## 对拍脚本（**不是门禁**）

```powershell
& .\.venv\Scripts\python.exe -X utf8 platform\check_text_parity.py
# 可选：--base http://localhost:8080   --dir .rounds/i1
```

它同时打后端与 Python 侧（`quanauto.dashboard`），逐字段比对（含每一格的 `text`），
并额外钉住 `/api/reports` 的**信封形状**。报告 `比对过的字段数`；为 0 即 FAIL。

**为什么不是门禁**：它需要一个活着的 JVM 进程。而「环境没起来就判红」的门禁本身就是判据缺陷 ——
与 `.github/copilot-instructions.md` 里那条「判据依赖环境就是判据的缺陷」同一个理由。
所以它只能由人在交付时跑一次，**不构成常驻证据**。

## 研究层那份 `quanauto/dashboard.py` 要不要退役（**方向登记，不是施工许可**）

这一层落地后最容易被问到的一句话是「平台层已经有了，研究层那份 `dashboard.py` 是不是可以删了」。
**答复是：不要删，也不建议现在退役。** 完整的现状对照与理由写在 `docs/迭代计划.md` 的
**§十「后续方向登记：看板展示面按消费面切」（2026-09-30，未施工）** 那一节里
（那是一份**手写**文档，按**节名**找，别按行号找 —— 行号会漂，节名不会）。
这里只留最短的三句：

1. **重复不在渲染层。** 两层真重的是三处 —— 那张 14 行规格表、读数规则、显示规则
   （`format_metric` ↔ `formatMetric`）。而 `render_html` 与本层的 React 页面是
   **两套各自重写**的渲染（`_escape` / `_svg_curve` / 逐点 `data-equity` 一个都没出现在
   Java 或 JS 侧）⇒ 删掉 `render_html` **一处重复都不会少**，只是砍掉
   「离线单文件 HTML 归档」这个消费面。
2. **顺序不能反。** 两侧的自动比对都以**研究层**为参照物：`platform-spec-parity`
   （`tools/verify_platform_specs.py`）读的就是 `quanauto/dashboard.py` 的 `METRIC_SPECS`，
   对拍脚本 `platform/check_text_parity.py` 也以 `quanauto.dashboard` 为基线
   ⇒ **先删研究层 = 先杀裁判**，留下的是一份没人再对拍的副本。
3. **真要消重复，正当做法是「生成」而不是「手抄」** —— 但那是另一轮施工
   （要改本层文件、也会改 `platform-spec-parity` 的判据形状），不是本节登记的内容。

⚠️ 本节是**方向登记**，不是状态陈述，也不是施工许可：它不解除任何缺口，也不改任何迭代的 DoD。
门禁数一律现取 `python tools/run_all_gates.py --list`。

## 手工端到端实测记录（2026-09-30，**一次性结论快照，不是常驻证据**）

这一节记的是一次**人工**跑出来的页面端到端结论。它不是门禁、不在 CI 里，
下次谁动 `platform/**` 它**不会自动重跑** ⇒ 过期了也没人报警。
它的唯一用途是给两条登记在册的零覆盖项（**页面本身**、**两侧展示文本是否一致**）
留一份可复核的手工证据 —— **不是**宣布这两条已经有判据了。

环境：Spring Boot 3.5.16 / Java 21.0.12 / Tomcat 10.1.55；前端是 Vite 构建产物
（index.html 443 B + 一个 CSS 2290 B + 一个 JS 224816 B，共 227549 B，构建产物本身不进版本库）；
按「先构建前端、再起后端」的正确顺序启动；报告目录 `.rounds/i1`（3 份报告）。

**这份记录覆盖的具体产物**（说清楚，免得读的人以为是泛指）：
`assets/index-iAjTmZUr.js`（224816 B）与 `assets/index-BvxPVP1P.css`（2290 B），
即 `ab6269a` 这一版树构建出来的那一份 —— 页面里那条自我声明（附录C §C.8 / §C.9）已包含在内。
⚠️ 本节早先记的 **224771 B / 227504 B 是补页脚之前那一版构建**，与树里的产物不是同一份，
已按重跑实测改正（这也是为什么这一节必须写明「测的是哪一版」—— 它是快照，不是函数）。

### 一、两侧展示文本是否一致（零覆盖项 ②）

- `platform/check_text_parity.py`（需要一个活着的 JVM ⇒ 不能当门禁）：
  **831 个字段 / 3 份报告逐字段一致**，含屏幕上会显示的那一列。
- 浏览器里逐格对拍（表格单元格 ↔ `/api/reports/<id>` 里的展示串）：
  **14 行 × 3 份报告，不一致 0 处**；切换报告会真的重新渲染（切回仍 14 行）。
- 分组也一并程序化核对（不是目测）：页面上四个分组标题的**名字与顺序**、
  以及每个分组下的行，与 API 里 `metrics[]` 按 `group` 切出来的结果**完全一致**。

### 二、页面本身（零覆盖项 ①）

- 布局：在 1400×900 / 480×900 / 360×800 三个宽度下，
  **互相重叠的关键元素 0 对**、**横向溢出 0**
  （最大右边缘 1192 / 441 / 321，均小于视口宽度；`scrollWidth <= innerWidth`）；
  纵向有滚动属长页面的正常现象。
- 净值曲线：1 条 `polyline`、**0 个 `circle`** ⇒ 研究层 `quanauto/dashboard.py`
  那个逐点 `<circle data-equity>` **没有搬过来**，与上一节「重复不在渲染层」的判断一致。

### 三、错误与边界分支

| 场景 | HTTP | 表现 |
| --- | --- | --- |
| 正常报告 | 200 | 14 个指标、曲线 60 点 |
| 报告缺 `deterministic` 段 | 422 | 「报告里没有 deterministic 段 —— 看板只认…那种形状，读不到就停在这里（不去猜别的键名）」 |
| 报告 JSON 语法坏 | 422 | 「报告读取失败：bad」；列表里那条带 `error` 字段、下拉标「（读不出来）」 |
| 报告 id 不存在 | 404 | 「没有这份报告：nope」 |
| 报告目录为空 | 200 | `{"reports":[]}`，下拉 `disabled`，显示「报告目录里没有 .json 报告。先跑一遍回测：…」 |
| 报告目录不存在 | 200 | 启动**不报错**（只记一行「报告目录：…（不存在）」），同样返回空列表 |

- **404 与 422 保持两类**（不存在 / 读不懂），没有合并成一个码。
- 响应体里**两个码都不含本机路径**（上表里那两串就是逐字实测的响应体）：
  带目录的那一句只在服务端日志里。这一条在 2026-09-30 被修好并用 JUnit 钉住，
  复测记录见下面的「四」小节。
- 选中一份读不出来的报告 ⇒ 视图清空并显示错误文字；**切回正常报告完全恢复**。
- 列表里**只有「语法坏」那一条**带 `error` 文本、下拉标「（读不出来）」；
  缺 `deterministic` 段的那条 `error` 是 `null` —— 因为列表只读 `summary` 段。
  这两条是重跑时逐项打印出来核对的，不是照着上一版抄的。
- 以上分支**未出现 500**。
- 浏览器控制台会有一个 `/favicon.ico` 的 404（页面没有声明 icon），后端把它记成
  WARN「静态资源没找到：No static resource favicon.ico.」。它**不是本次回归**、也不是 500 分支。
- 一条观察（不是缺陷断言）：「读取中…」那段文案只在首次加载出现，切报告时不出现。
- 一条观察（同上）：空目录那句提示写死了「默认 `.rounds/i1`」，
  而这次边界实测是把后端指向临时目录跑的 ⇒ 提示里的目录名与实际读的目录可能不一致。
  这是前端写死的文案；后端只在 `/api/health` 里下发真实的 `reportsDir`。

### 四、2026-09-30 复测：错误响应体不再带本机路径（本节唯一**有常驻牙**的一条）

上面三节都是「没有判据、只好人工看一眼」的登记；这一条不一样 —— 它**修好了、而且有牙**，
写在这里是因为它是在同一次手工回归里被发现的，而发现它的证据只有手工那一次能留下。

- **改之前**（实测，本机后端 8080）：`GET /api/reports/nope` 的响应体是
  `{"error":"没有这份报告：nope（目录 D:\\project-quan\\quan-auto\\.rounds\\i1）"}`
  —— 本机绝对路径直接下发给了浏览器。而 `ApiExceptionHandler` 的类注释当时写着它「不回内部路径」。
- **改之后**（同一台机、同一条请求）：响应体 `{"error":"没有这份报告：nope"}`（无 `D:`、无 `\\`、无 `.rounds`），
  而**同一时刻服务端日志**仍然打出 `报告不存在：没有这份报告：nope（目录 D:\\…\\.rounds\\i1）`
  ⇒ 同一份信息，**两个去处**：详情进日志（本机、排障用），下发句进响应体（跨机、给浏览器）。
- **牙在哪**：`platform/api/src/test/java/com/quanauto/dashboard/ApiExceptionHandlerTest.java`（7 条 JUnit 用例）
  钉住它，而 `platform-runtime` 门禁**真的会跑** `mvn test` ⇒ 它是常驻的，不依赖人记得跑。
  同一批还保住了「本机路径本来就不存在」的那条消息（`ReportProjection` 的 422 文案）**逐字不变**。
- **两处证伪**（不是只看它变绿）：① 把 handler 里的 `clientMessage()` 改回 `getMessage()` ⇒ 3 条用例红；
  ② 把上游构造下发消息时又拼上「（目录 …）」 ⇒ 1 条用例红。两处各自打在一个成因上，逐字节还原后回到绿。
- **依赖的环境**：`ReportNotFoundException` 的详细消息里带的是**报告目录**，所以第一条用例是在
  临时目录上构造异常的（并先断言详细消息里**真的**有那个目录名 —— 前提失效时用例会以「前提失效」
  失败，而不是静默变成恒真）。

⚠️ 再强调一次：这一节是**某一次手工跑的结论快照**，不是判据。
两条零覆盖项的登记状态**不变**；要变，得先把它们真的做成门禁。
