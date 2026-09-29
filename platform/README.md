# `platform/` —— 回测绩效看板（平台层 P0 切片）

> **这一层不在 CI 里，也没有任何自建门禁覆盖它。**
> 本仓库 17 个门禁全部是解析 Python 或 SQL 文本的静态检查器；`mvn test`、`npm run build`
> 与页面本身没有任何门禁盯着。见 `docs/智能量化交易平台.md` 附录C §C.5。
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
同款 —— 但那张 14 条规格表在 Java 侧是**手抄的副本**，没有门禁比对两者（附录C §C.5）。

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

**顺序要紧**：前端构建的产物写进 `api/src/main/resources/static/`，
而 Spring 是从 `classpath:/static/`（= `target/classes/static/`）托管的，
所以**先 build 前端，再启动后端**；先起后端再 build 的话，served 的那份是旧的。

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
