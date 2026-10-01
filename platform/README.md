# `platform/` —— 回测绩效看板（平台层 P0 切片）

> **这一层现在有四条门禁**（2026-10-01 订正：「两条」是 2026-09-30 的说法、「三条」是同日更早一点的
> 说法，都已过期；**计数不要在这种句子里改，要重测它修饰的那个谓词** —— 见下面那条 ⚠️）。
> `platform-spec-parity`（`tools/verify_platform_specs.py`，tier-A）把 Java 侧
> `MetricSpec.METRIC_SPECS` 与 Python 侧 `quanauto/dashboard.py` 的 `METRIC_SPECS`
> **双向**比对（14 行的键/分组/标签/量纲/小数位 + **行序** + 量纲与分组常量 + `STRUCTURE_COUNTS`）。
> `platform-runtime`（`tools/verify_platform_build.py`，tier-A）在**一次性沙箱**里按
> `npm ci` → `npm test` → `npm run build` → `mvn test` **真跑一遍**
> ——**这个顺序与步骤集合本身是判据**（见下「跑起来」）。第二步 `npm test` 是 **2026-10-01 追加**的：
> vitest + jsdom 里**真的把页面挂起来**（用例 `platform/web/test/render.test.jsx`，输入是一份由
> 真报告投影出来的夹具 `platform/web/test/report-view.fixture.json`）
> ⇒ 它让「**页面渲染**」这一格**不再**是零覆盖项；但也仅此一格 ——
> jsdom **没有布局引擎**（几何量恒为 0 ⇒ 写几何断言只是恒真的假绿）⇒ **布局 / 视觉**仍然零覆盖。
> `platform-text-parity`（`tools/verify_platform_text_parity.py`，tier-A，2026-10-01 追加）比**显示规则**：
> 一侧核 `platform/text-parity-cases.json` 这份夹具还新不新鲜（语料是 Python 现算的），
> 另一侧的 Java 用例 `FormatParityTest` 由上面那条 `mvn test` 真的跑 —— 它关掉的是
> 「两侧打印出的字符串可能不同」这条一直只有手挑样本撑着的缝（实测量级见下面「对拍脚本」一节）。
> `platform-web-parity`（`tools/verify_platform_web.py` + `tools/platform-web-bindings.json`，
> tier-A，2026-10-01 追加）管**页面**这一侧**静态可判**的那五件事：页面读的**字段名**能不能在
> Java 记录组件上找到、**数字格式化**有没有漏到前端、页面里有没有**手抄规格表**（拿
> `quanauto/dashboard.py` 当参照物）、页面**页脚那份自报的门禁清单**等不等于注册表，
> 以及上面那份**渲染夹具**的规格列与键集合能不能对得上两侧现在算出来的东西（`PW-FIXTURE-KEYS`）。
> **它只读源码**：不起服务、不编译 Java、不开浏览器。
> 四条都接在 `tools/run_all_gates.py` 的统一入口里 ⇒ 而 `ci.yml` 最后一步就是那条命令
> ⇒ **四条都真的在 CI 上跑**（代价是 CI 里必须装 JDK 21 与 Node，见 `ci.yml`）。
> `platform-runtime` 探不到工具链时退 **3 = SKIPPED**，harness 把 3 记成「**不算绿**」。
> 门禁数现取 `python tools/run_all_gates.py --list`，不要在文档里抄。
> ⚠️ 四条都**不起服务**（跑完就退出，没有一个活着的 JVM 在监听端口）；其中三条**不渲染页面**，
> 而**渲染**那一格归 `platform-runtime` 的 `npm test` 那一步（它同样不起服务）
> ⇒ 仍然没有任何门禁盯着的只剩**布局 / 视觉**那一格；
> 「**活着的服务端**到底下发了哪些字节」（含 `/api/reports` 的信封形状）也仍然只有手动脚本（见下）。
> ℹ️ 订正（2026-10-01，晚）：上一句原文写的是「四条都**不起服务**、**不渲染页面** ⇒
> **页面渲染 / 布局 / 视觉**仍然没有任何门禁盯着」——「不渲染页面」那半句自这一批起
> **是字面为假的**（渲染归 `platform-runtime` 的第二步，出处在附录C **§C.13**）；
> 保留下来的只有「**布局 / 视觉**零覆盖」那半句，且它是**有理由的**零覆盖
> （jsdom 没有布局引擎），不是「没人管」。
> 但它此前还容易被读成「**页面这一层一个字都没被机器看过**」—— 那半句从 `platform-web-parity`
> 落地起就不成立（**字段名 / 格式化落点 / 手抄规格表 / 页脚自报清单 / 渲染夹具键**这五件事
> 现在有判据了）。**两个方向都别读。**
> ℹ️ 订正（2026-10-01，早）：同一句话更早的版本里写的是「三条都**不启动 JVM**、不渲染页面」，
> **前半句是字面为假的** —— `platform-spec-parity` 与 `platform-text-parity` 确实不启动 JVM，
> 而 `platform-runtime` 恰恰是靠**起 JVM 跑 `mvn test`** 才有牙。错因：把「两条」改写成「三条」时
> **只更新了计数、没有重测那个计数修饰的谓词**。
> ⚠️ 同一个坑本轮又踩了一次（轻）：把门禁从三条扩到四条时，「三条都**不起服务**」里的 `platform-web-parity`
> **恰好也是不起服务的** ⇒ 谓词这次**碰巧**没变假。**这不是运气好，是该把它写成判据** ——
> 页脚那份自报清单（`platform-web-parity` 的 `PW-GATE-LIST` 段）就是防止「清单与注册表脱节」的那颗牙；
> 而「不起服务」这个谓词本身仍然只靠人读。
> 分工的出处是 `docs/智能量化交易平台.md` 附录C 的 §C.5 与它的 C.8 / C.9 / C.11 / C.12 订正块，
> 以及 **§C.13**（= C.5 第 5 条的 2026-10-01 订正：**渲染那层** —— `platform-runtime` 的第二步
> `npm test`（vitest + jsdom）真的挂载过页面，而它读的那份夹具由 ⑤ 的 `PW-FIXTURE-KEYS` 双向核对）。
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
ℹ️ 订正（2026-10-01）：上面这两句旁边原来还写着「两侧**展示文本**也只有手动脚本」。
**那一半已过期** —— 「两侧按同一条显示规则打印出同一个串」现在有常驻判据
（`platform-text-parity`：夹具 + `FormatParityTest`，由 `platform-runtime` 的真 `mvn test` 跑）。
仍然只有手动脚本的是**另一件事**：一个**活着的**服务端到底下发了什么（含 `/api/reports`
的信封形状）—— 门禁**不起服务**（跑完就退出，没有活着的 JVM 在监听端口），所以那一层照旧不构成常驻证据。

## 目录

| 路径 | 内容 |
| --- | --- |
| `pom.xml` | 聚合 POM。`java.version=21`、Spring Boot `3.5.16` |
| `api/` | Spring Boot 只读 REST + 静态资源托管 |
| `web/` | React 19 + Vite 8 单页 |
| `web/test/render.test.jsx` | **渲染那一格的判据**（2026-10-01 追加）：在 vitest + jsdom 里真的挂载 `App.jsx` 并断言打印串 / 分组顺序 / 各错误分支 / 曲线点。⚠️ 里面**故意不写**几何断言（`getBoundingClientRect` 在 jsdom 里恒为 0 ⇒ 那种断言恒真 = 假绿）；它由门禁 `platform-runtime` 的 `npm test` 那一步跑 |
| `web/test/report-view.fixture.json` | 上条那份测试的**输入夹具**（真报告 → 服务端投影 → `dashboard.format_metric` 现算出来的那一份）；它属于**派生**文件 ⇒ 新鲜度由 `platform-web-parity` 的 `PW-FIXTURE-KEYS` 双向核对 |
| `text-parity-cases.json` | **两侧显示规则的语料夹具**（由 Python 侧现算写出，`tools/verify_platform_text_parity.py --write` 重录）：`unit / digits / value / text`，Java 侧 `FormatParityTest` 拿它逐条对拍 |
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

# 2) 前端依赖 + 渲染测试 + 构建（产物落到 api/src/main/resources/static/）
cd platform/web
npm install
npm test        # vitest + jsdom：真的把页面挂起来（这一步也是 platform-runtime 的第二步）
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

ℹ️ 订正（2026-10-01）：本节原来还顺带承担了「两侧**展示文本**是否一致」这条零覆盖项。
**那一半已经有门禁了**，而且就是在这里发现的问题：拿 73929 组输入做差分，Java 侧
`ReportProjection` 与 Python 侧 `dashboard.format_metric` 在 **6887 组（9.3%）**上打印出
不同的字符串（丢负号 4161 / 十进制平局落错边 2726，另有一类大整数计数），
而当时两侧**所有**已有的用例（Java 手挑的 7 条断言 + 手动对拍脚本的 3 份真报告）**都是绿的**
—— 手挑的样本与「碰巧没碰到边界」的真报告都证明不了「两侧一致」。
现在这件事由 `platform-text-parity`（`tools/verify_platform_text_parity.py`）盯：
Python 侧保证语料新鲜且够厚，Java 侧 `FormatParityTest` 把同一份语料逐条喂进
`formatMetric` 与 Python 比（由 `platform-runtime` 的真 `mvn test` 跑）。
本脚本**仍然有价值且仍然不是门禁**，因为它查的是门禁查不到的那一层：
**一个活着的服务端**真的下发过什么（含信封形状与每一格的 `text`）。

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
它现在的用途是给**仍然零覆盖的那几件事**留一份可复核的手工证据：
① **布局 / 视觉**（`platform-web-parity` 只读源码、**不渲染页面**；渲染那一格已归
`platform-runtime` 的第二步 `npm test`（vitest + jsdom）⇒ 本节「二」那两条里
**只有几何/重叠那一条**仍然只有手工证据，几何量归 jsdom 管不了的那一格）；
② **一个活着的服务端到底下发了什么**（本层的四条门禁**都不起服务**：跑完就退出，
没有一个活着的 JVM 在监听端口 ⇒ 本节「一」里那些数字测的是真跑起来的服务端，
那层仍然只有手工证据，详见下文订正块）。
ℹ️ 订正（2026-10-01）：上一句原文写的是「仍然零覆盖的**那一条**（页面本身）」—— `platform-web-parity`
落地后，「页面这一层一个字都没被机器看过」那半句不成立（**字段名 / 格式化落点 / 手抄规格表 /
页脚自报清单**有了判据）；今天再把 **渲染**那一格也交出去
（归 `platform-runtime` 的 `npm test`，出处在附录C **§C.13**）⇒ 只剩**布局 / 视觉**零覆盖；
而「两侧展示文本是否一致」自 2026-10-01 起有常驻门禁 `platform-text-parity`（那条门禁同样不起服务）。

环境：Spring Boot 3.5.16 / Java 21.0.12 / Tomcat 10.1.55；前端是 Vite 构建产物
（index.html 443 B + 一个 CSS 2290 B + 一个 JS 224816 B，共 227549 B，构建产物本身不进版本库）；
按「先构建前端、再起后端」的正确顺序启动；报告目录 `.rounds/i1`（3 份报告）。

**这份记录覆盖的具体产物**（说清楚，免得读的人以为是泛指）：
`assets/index-iAjTmZUr.js`（224816 B）与 `assets/index-BvxPVP1P.css`（2290 B），
即 `ab6269a` 这一版树构建出来的那一份 —— 页面里那条自我声明（附录C §C.8 / §C.9）已包含在内。
⚠️ 本节早先记的 **224771 B / 227504 B 是补页脚之前那一版构建**，与树里的产物不是同一份，
已按重跑实测改正（这也是为什么这一节必须写明「测的是哪一版」—— 它是快照，不是函数）。
ℹ️ **2026-10-01 实测：源码比磁盘上那份产物新（至少从 `9fe4ab8` 起就没重建过）** ——
`platform/web/src/App.jsx` 页脚在**源码**里现在写的是 `§C.8 / §C.9 / §C.11 / §C.12`，
而**磁盘上那份产物仍是上面那两个字节数对应的那一版**（页脚只写 `§C.8 / §C.9`）
⇒ 在**源码**里读到的自我声明会比**页面上**看到的更新**两版**，这是正常的（源码 ≠ 最近一次构建）。
对账口径：**以 `tools/gates-report.txt` 里 `platform-runtime` 那一段的 `static_bytes` 为准**
（那是沙箱里真跑 `npm ci` → `npm run build` 量出来的，不靠人手记）：
2026-10-01 晚现取 **228233 B**（同一次报 `static_files=3 served_files=3`），
比磁盘上那份产物（合计 227549 B）多 **684 B**。
⚠️ 这个差额**全部来自「源码比产物新」**：`9fe4ab8`（把页脚补成 `§C.8 / §C.9 / §C.11`）与本次
（再补成 `§C.8 / §C.9 / §C.11 / §C.12`）都**没有重建过磁盘上那份产物** ⇒ 差额是累积的，
**不是缩水**，也不是门禁口径不同。
⚠️ 它**每改一次前端源码都会变** ⇒ 这两行是快照；要刷新就按「先 `npm run build`、再起后端」
重来一遍，并回来把 `static_bytes` 与「多 **N** B」**重录**（别让快照和产物脱钩）。
门禁**不会**因为这两个数不同而红 —— 它量的是**沙箱里刚构建出来的那一份**。
ℹ️ **2026-10-01 晚订正（本轮重建过前端）**：上面那句「磁盘上那份产物仍是那两个字节数对应的
那一版」**已不成立** —— 本轮为了对页面做「布局 / 视觉」实测，按「先 `npm run build`、再起后端」
的顺序重跑了一遍构建（`vite build` ⇒ `17 modules transformed`，157 ms），磁盘上的产物现值是
`index.html` **443 B** + `assets/index-BVJOXJxo.js` **225895 B** + `assets/index-BvxPVP1P.css`
**2290 B**（合计 **228628 B**），**已追上源码**：页脚那份自我声明现在是
`§C.8 / §C.9 / §C.11 / §C.12`，与 `platform/web/src/App.jsx` 里的字面量一致。
⇒ 上面那 684 B 的差额**这一轮抹平了**；对账口径不变（沙箱里重建的那一份仍以
`tools/gates-report.txt` 里 `platform-runtime` 那一段的 `static_bytes` 为准，**现取**）。
本轮的这次手工实测（探针跑在 `http://localhost:8080/` 上）测的**就是这一份**产物，
取证快照 `platform/layout-probe-report.txt`，结论见下面新加的那一节。

### 一、两侧展示文本是否一致（原「零覆盖项 ②」，**2026-10-01 订正**）

⚠️ 订正：标题里的「零覆盖项 ②」是 2026-09-30 的登记，**已过期** —— 那条缝现在由
`platform-text-parity` 门禁盯着（Python 侧核语料新鲜度与厚度，Java 侧 `FormatParityTest`
把同一份语料逐条喂进 `formatMetric` 与 Python 比，由 `platform-runtime` 的真 `mvn test` 跑）。
下面这三条**仍然只有手工证据**，因为门禁**不起服务**（没有活着的 JVM 在监听端口）：它们测的是一个真跑起来的服务端。

- `platform/check_text_parity.py`（需要一个活着的 JVM ⇒ 不能当门禁）：
  **831 个字段 / 3 份报告逐字段一致**，含屏幕上会显示的那一列。
- 浏览器里逐格对拍（表格单元格 ↔ `/api/reports/<id>` 里的展示串）：
  **14 行 × 3 份报告，不一致 0 处**；切换报告会真的重新渲染（切回仍 14 行）。
- 分组也一并程序化核对（不是目测）：页面上四个分组标题的**名字与顺序**、
  以及每个分组下的行，与 API 里 `metrics[]` 按 `group` 切出来的结果**完全一致**。
  ⚠️ 这三条当时全绿，**而两侧的显示规则其实有大面积分歧**（差分量级见上面「对拍脚本」
  一节的订正）—— 这三份真报告恰好没踩到那几个边界。⇒ 这三条不能当「规则正确」的证据。

### 二、页面本身（原「零覆盖项 ①」，**2026-10-01 订正**）

ℹ️ 订正（2026-10-01）：原登记是「页面本身（渲染 / 布局 / 视觉）」整格零覆盖。
其中**渲染**那一格已交出去：`platform-runtime` 的第二步 `npm test` 在 vitest + jsdom 里
真的把 `App.jsx` 挂起来并断言了「14 格逐字等于服务端给的 `text` / 原值不露 / 分组顺序 /
空目录 / 错误分支 / 曲线点」（订正出处：附录C **§C.13**）。
下面这两条里只有**第一条（几何 / 重叠 / 溢出）仍然零覆盖**、仍然只有手工证据：
jsdom **没有布局引擎**（`innerWidth === 1024` 但所有 `getBoundingClientRect()` 恒为 0、
`offsetHeight` 恒为 0）⇒ 这类断言在 jsdom 里**恒真**，写了就是**假绿**。
第二条（结构性：几条 `polyline` / 有没有 `circle`）其实**已经可判**，
但那条判据没写进测试 ⇒ 仍然只有手工证据（想拿它就把结构断言补进 `render.test.jsx`）。

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
两条登记的归属**各自变了一半**（2026-10-01）：
- 「**页面本身**」：**布局 / 视觉仍然零覆盖**（渲染那一格自 2026-10-01 起归
  `platform-runtime` 的第二步 `npm test`，见附录C §C.13），而**字段名 / 谁在格式化数字 / 手抄规格表 /
  页脚自报清单 / 渲染夹具键**这五件事已经归 `platform-web-parity`
  （它只读源码 ⇒ 本节「二」里那条**几何 / 重叠**仍然只有手工证据）。
- 「**两侧展示文本是否一致**」：已经不是零覆盖项 —— 它被做成了门禁 `platform-text-parity`；
  但那条门禁不起服务，所以本节「一」里那些**活服务端**的数字仍属手工证据。

## 布局 / 视觉：一支一次性探针（2026-10-01 晚追加，**不是门禁**）

上面「手工端到端实测记录 · 二」里那条**几何 / 重叠 / 溢出**，到 2026-10-01 晚多了一件东西：
一支**可复跑的探针** `platform/probe_layout.js` + 它那一次取证的快照 `platform/layout-probe-report.txt`。
**但它仍然不是判据**，这一格在门禁意义上**仍然零覆盖** —— 理由只有一条，而且上面已经写过：
几何量只有在**真有布局引擎**的地方才有意义，而本仓库能常驻跑的那个 DOM 环境（vitest + jsdom）
没有布局引擎 ⇒ 在那里写几何断言**恒真**。要把它变成门禁，需要**同时**有一个活着的 JVM 与一个真浏览器
⇒ **判据依赖环境就是判据的缺陷**。所以它只能是「一次性取证 + 谁要谁复跑」，
与 `platform/check_text_parity.py` 同一个档位。

- **跑法**：先 `npm run build`（顺序反了 served 旧包），再按 `JAVA_HOME=21` 起后端，
  然后在浏览器 Console 里粘 `platform/probe_layout.js` 整段、敲 `probeLayout()`；
  或 Playwright 里 `page.addScriptTag({ path: 'platform/probe_layout.js' })` → `page.evaluate(() => probeLayout())`。
  ⚠️ 本层「跑起来」那节的 `JAVA_HOME` 坑在这里同样适用（`set "VAR=值"` 要带引号那一对）。
- **它判 12 件事**（一项一个探测器，互不 return）：提取为空（`LP-EMPTY`，最要命的一条 —— 提取失配时
  后面每个探测器都在空转，报告却会打印「0 问题」）、横向越界（`LP-OVERFLOW`）、整页横向滚动（`LP-SCROLL`）、
  兄弟重叠（`LP-OVERLAP`）、文本被截断（`LP-CLIP`）、该可见的塌成 0×0（`LP-ZERO`）、
  服务端下发读不出来（`LP-API`）、指标格数/标签/文本与服务端逐字一致（`LP-TEXT-COUNT` / `LP-LABEL` / `LP-TEXT`）、
  折线点数与报告曲线点数（`LP-CURVE-POINTS` / `LP-CURVE-COUNT`）。
- **2026-10-01 晚那一次**：3 份报告 × 3 档视口（1280 / 800 / 375）**12 个码全部沉默**，
  且提取到的对象都非零（4 个分组 / 14 个指标格 / 14 个行头 / 1 条 polyline）
  ⇒ 沉默有意义。同一次里还做了**触发测试**：12 个码**逐个**构造坏样本，**12/12 都报出了预期那个码**
  （含一条「什么都不改」的对照为 0）。逐条见 `platform/layout-probe-report.txt`。
  ⚠️ 那一轮有条变异**没打到靶子**（给 `main` 设内联 `width:3000px` 什么都没报 —— `main` 有 `max-width`，
  封顶了），换成「往 `main` 里塞一个宽 div」才打到。记在报告里，因为这正是「变异没打到分支」
  与「探测器没写」长得一模一样的那种坑。
- **它量不到什么**（别读多）：好不好看（本仓库没有设计稿可比）、纵向排布是否合理（只判「兄弟重叠」）、
  键盘可达性/焦点顺序/对比度、以及**只在这一档宽度下成立**（三档是手挑的，不是穷举）。
- 🔴 **那一页的自我声明里那句「仍然是零覆盖的只剩 布局 / 视觉」仍然成立**（没有常驻判据就是没有），
  不要因为多了这支探针就把那句话改成「已有判据」。

