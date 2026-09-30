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
