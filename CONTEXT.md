# CONTEXT.md — 项目 L0 索引

> **这是索引，不是文档。** 它唯一的职责是让你在不读大文件的前提下知道「该读哪个文件」。
> 任何需要超过 100 行才说清的内容都属于 L1/L2，应写进对应产物，不要往这里塞。
>
> 阅读顺序：`CONTEXT.md`（本文件）→ 按需读 1 个 L1 产物 → 只在必要时读 L2 全文。
> 实测《核心模块接口契约文档》63 KB、《数据中心接口契约文档》48 KB —— 全读一遍是
> 十几万 token，而 90% 的提问只需要 §3.6 的约束清单。

---

## 1. 这是什么

智能量化交易平台的**开发进行中**。契约、DDL、门禁三件套已齐，I0 建了工程骨架，
I1 交付了第一条端到端竖切（CSV → MA 双均线 → 撮合 → 绩效 → 回测报告），
I2 正在把数据换成真实数据源（**S1 已落 PIT / `as_of` 边界层**、**S2 已落采集侧适配器
`quanauto/datasources.py`**、**S3 已落落库侧 `quanauto/pgstore.py` 与回测改读接线**（引擎只吃 `DataCenter.as_of()` 产出的 feed，`tests/test_backtest_db_feed.py` 守着 10 条端到端不变量）；未做的只剩真实数据源联调（从未联网）与复权因子恒 1.0）。
依赖清单从 I2 S2 起**不再为空**：`dependencies = ["pandas>=2.0"]`（使用者 `quanauto/datasources.py`，
数据中心契约 §3.2 的方法签名逐字写了 `pd.DataFrame`）+ `[datasources]` extra（`akshare` / `baostock`，
适配器对它们是**惰性 import**）。I0/I1 当时的空清单是**选择不是缺口** —— 那时的竖切只用标准库。

## 2. 硬约束（违反任何一条都会造成不可逆损失）

| # | 约束 | 后果 |
|---|---|---|
| C1 | 本机**没有本地 PostgreSQL 实例**：`psql` 不在 PATH、无服务、无安装目录。容器通道**可用**（Docker Desktop 已跑起来） | 不要再说「从未执行」—— 2026-09-23 起 `db/*.sql` 已在容器上执行过（触测 23/0 与 42/0 PASS），2026-09-24 又在 `postgres:14` / `15` / `16` 上各跑一遍，四份快照 `tools/sql-smoke-report-pg14.txt` / `tools/sql-smoke-report-pg15.txt` / `tools/sql-smoke-report-pg16.txt` / `tools/sql-smoke-report.txt`。但也**不能反过来说「约束已验证」**：只覆盖这四个 tag，「PostgreSQL 14+」仍未证实；逐条证伪（`falsify_smoke.py` 31/31）仍只在 `postgres:17` 上做过；改过 SQL 后结论即作废 |
| C2 | `docs/` 下 3 个 `.md` 由 `.docx` 转换生成，**重新转换会覆盖** | 只能在其**之后追加**，禁止原地改已有段落。原地改 = 内容丢失 + md-fidelity 门禁失败。**追加的内容同样会被重新转换抹掉**，所以每个追加块（模块4 的「以契约为准」注、附录A）都登记在 `verify_md_coverage.py` 的 `ADDENDA` 清单里，被抹掉即 FAIL。⚠️ ADDENDA **只对「有 `.docx` 对应物」的文件成立**：`docs/智能量化交易平台-数据中心接口契约文档.md` 是**手写契约**（`docs/` 下没有同名 `.docx`），可以原地编辑，它的附录 A 也**不该**登记进去 —— 登记上去只会变成一条永远不会被检查的死条目 |
| C3 | 控制台是 **cp936(GBK)** | stdout 出现 GBK 之外的字符（如 `↔`）会让 Python 抛 `UnicodeEncodeError` 并丢掉整段输出；PowerShell `>` 写的是 **UTF-16LE** 不是 UTF-8；含中文的 argv 会被破坏 |
| C4 | `tools/` 下**只用标准库** | 不要引入任何第三方依赖 |
| C5 | ~~本仓库不是 git 仓库~~ **已修完（2026-09-23）**：是 git 仓库（`main`，基线提交 `053f620`，一次迭代一个提交） | ~~没有配置 git remote~~ **也已修完（2026-09-24）**：remote `origin` 已接（公开仓库 yuezu1026/quan-auto），`main` 已推送 ⇒ `.github/workflows/ci.yml` **真的在 GitHub 上跑**。首次真跑就在 `skeleton` 门禁上判红：本机有 `.venv/`、克隆里没有 ⇒ 判据依赖环境，已修；修复后第二次运行（2026-09-24，run 35939823649）**success**。CI 的本地等价证据仍是 `tools/ci-dryrun-report.txt`（快照，改完代码要重跑），两者**不能互相替代** |

## 3. 仓库地图

### A. 由 `.docx` 生成（C2：只可追加，不可原地改）

| .md（产物，勿原地改） | .docx（源） |
|---|---|
| `docs/智能量化交易平台.md` | `docs/智能量化交易平台.docx` |
| `docs/智能量化交易平台-核心模块接口契约文档.md` | 同名 `.docx` |
| `docs/智能量化交易平台-数据中心数据源选型与接入规范.md` | 同名 `.docx` |

### B. 手写产物（可自由编辑）

| 文件 | 内容 |
|---|---|
| `docs/开工前缺口清单.md` | ★ **当前状态与阻塞项的唯一权威来源**。想知道「现在卡在哪」就读它 |
| `docs/开发工作流规范.md` | ★ **开工前的作业规程**：产物四件套 / TDD 红源 / 四类漂移 / 对称棘轮 / 触发测试硬要求 / 八类真实踩坑案例 / token 分层 / 红线清单 |
| `docs/迭代计划.md` | ★ **交付节奏**：竖切 **I0~I4**（股票池那一轮已于 2026-09-23 裁决砍掉）、每个迭代的 DoD 四件套、B7 棘轮燃尽表、不采用的敏捷做法。原「待决」问题（迭代计划里那 5 条 + 更早的 Q1/Q7）已全部裁决并回填，**已无未决项** |
| `docs/智能量化交易平台-风控层接口契约文档.md` | 风控层契约（补充主契约缺失的类型/异常），文末有 **§六**（I3 交付与裁决：交付边界 3 条 / 逐条裁决 15 条 / 咬出的真缺陷 / 已知限制 / 门禁对应）与 **§七**（I3b 小步：存储层真接线的交付边界、**留痕写入方与 §3.6 「异步批量」的偏离及理由**、实现侧新增的三个名字、真库探针抓到的 `pgstore` 包装层缺陷与修法、门禁/测试对应、两条新登记缺口）。两节都是**追加**记录，上文一个字没改。**手写契约，没有 `.docx`** ⇒ 可原地编辑；在那里「只追加不原地改」是审计约定，不进 C2 的 `ADDENDA` |
| `docs/智能量化交易平台-数据中心接口契约文档.md` | 数据中心契约，含 D1~D10 设计决策 + **附录 A**（I2 S1 实现侧裁决与偏差登记：`DataFeedError` 继承、`SessionMode` 本地类型、越界两种语义、复权因子恒 1.0 这个已知缺口）+ **附录 B**（I2 S2 适配器裁决与偏差：`ValidationReport` 升格为 5 字段、pandas 是显式依赖、源 SDK 只能惰性导入、符号后缀与指数歧义、两处“安静的事故”（手/股、百分号）、`report_type` 必须适配器层映射完，另记两条已知缺口：停牌/涨跌停标记无处可落（契约内部不一致）、龙虎榜/资金流向无标准 schema）+ **附录 C**（I2 S3 落库侧裁决与偏差：`DataStoreError`(DATA_008) 属实现侧新增异常码、假连接与真库的效力边界、`PsycopgConnection` 的复用与事务语义）。**手写契约，没有 `.docx`** ⇒ 可原地编辑，不进 C2 的 `ADDENDA` |

### C. 工程骨架（2026-09-23 I0 交付；守门门禁 = `skeleton`）

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 包元数据 / pytest 配置。依赖**由真正用它的模块带进来**，禁预置「以后大概会用」的包：现在是 `pandas>=2.0`（使用者 `quanauto/datasources.py`），源 SDK 走 `[datasources]` extra（I0/I1 当时是空数组，因为那时的竖切只用标准库），数据库驱动走 `[postgres]` extra（2026-09-25 裁决：`psycopg[binary]>=3.1`，使用者 `quanauto/pgstore.py`；**extra 是 opt-in，CI 只装 `.[dev]`**） |
| `quanauto/__init__.py` | 包入口，只有 `__version__` 与 `__all__`（其它模块下的实现不在此 re-export：否则「实现在那里」与「一 import 就全拉起来」长得一样） |
| `quanauto/*.py`（**15 个模块**：14 个实现 + 入口 `cli.py`；条目数现取 `quanauto/*.py`） | I1 的产物：`models` / `enums` / `errors` / `events` / `datafeed` / `strategies` / `broker` / `engine` / `performance` / `cli`；**I2 S1 追加 `datacenter.py`**（PIT / `as_of` 边界层）、**I2 S2 追加 `datasources.py`**（采集侧适配器：源列名/取值 → 标准 schema + `ValidationReport`）、**I2 S3 追加 `pgstore.py`**（落库侧：`PgBarStore` 读 / `PgBarIngestor` 幂等 upsert / `PsycopgConnection` 惰性驱动适配）、**I3 追加 `risk.py`**（风控引擎：四层作用域优先级 + `KillSwitch` 熔断 + 阈值收紧/放宽与 LKG 回退，由 `tests/test_risk_engine.py` 守），**I3b 小步在 `risk.py` 里真接上存储层**（`DbRiskRuleStore` 三个读写方法 + 四个运行态方法 + `RiskInterceptLogWriter` 拦截留痕；写入方是**调用方** `BacktestEngine._record_risk_block()` 而不是 `RiskEngine`，偏离与理由见风控契约 §七），**I4 追加 `dashboard.py`**（绩效看板：读一份 `BacktestResult` → 按固定 14 行清单摆指标表 + 资金曲线，**纯读数**，一个指标都不重算；口径登记在核心契约 §F，由 `tools/verify_dashboard.py` 与 `tests/test_dashboard.py` 守）。守门门禁 = `contract-signature`（签名不许偏离契约）+ `data-center-pit` + `data-center-adapter` + `dashboard-consistency` |
| `tests/test_skeleton.py` | 骨架自检 3 条：版本与 `pyproject.toml` 一致 / import 不把重依赖拉进来 / 测试数不为 0 |
| `tests/test_backtest_slice.py` | I1 的回归测试 22 条：竖切的不变量（同种子可复现、成交价取下一根 K 线开盘价、拒单不抛异常…）。它**没有**红→绿的 git 证据（实现先于测试），替代证据是 `tools/pytest_mutation_check.py` |
| `tests/test_data_center_pit.py` | I2 S1 的 PIT 回归测试 18 条：预取未来数据必须抛 `FutureDataAccessError`、窗口越界必须抛而**不是**裁剪、回测会话下 `QFQ`/`BFILL` 必须被拒。含 I2 DoD 点名的那条**触发测试**（让 store 无视窗口，确认取数真的被拒）。另有一条**故意断言已知缺口**的用例，`S3` 落地后要**删掉它**而不是改期望值 |
| `tests/test_data_center_adapter.py` | 适配器回归测试：**S2 收工时 32 条，2026-09-25 现取 104 条**（`docs/迭代计划.md` 里 S2 那段「32 条」是**那一轮的收工快照**，不回改）：源列名不许透出下游、映射后必须是标准 schema、手→股与百分号两处换算、裸代码/指数后缀/`report_type` 越界必须抛。它测的是映射**机制**，不是映射**内容**（未联过网，见 DC 契约附录 B10） |
| `tests/test_data_center_store.py` | 落库侧回归测试：**S3 收工时 29 条，2026-09-25 现取 37 条**：读必须按 `data_version` 过滤、窗口闭区间、upsert 的三条分支（新插入 / 同值不写 / 异值抛 `IngestConflictError`）与并发抢写、异常不许二次包装、驱动必须惰性导入。**一律不连库**：假连接只能证明「发出去的语句/参数是什么」，不能证明 PostgreSQL 接受它 —— 后者只有容器通道能证，两者不得互相冒充（DC 契约附录 C） |
| `tests/test_backtest_db_feed.py` | I2 S3 **后半**的端到端回归测试 10 条：引擎只吃 `DataCenter.as_of()` 产出的 `DataFeed`、报告里的 `data_version` 必须是存储层版本且空版本必须抛 `DataVersionError`、`as_of` 之后的行必须被拒、存储层的 `Decimal` 必须在边界处收成 `float`。它守的是一条**缝**：S3 前半（落库）与后半（引擎）各自的用例都看不见对方那一侧，两边各自全绿不等于接起来能跑（实测当场咬出 3 个真缺陷） |
| `tests/fixtures/sample_prices.csv` | 回归测试与可复现性门禁共用的小样本行情 |
| `tests/test_risk_engine.py` | I3 前置的风控引擎本体回归测试 67 条（60 个测试函数，其中一条参数化出 8 组合）：规则装载与阈值校验（`RATIO` 不许写成百分数、全局规则缺一不可）、四层作用域优先级、`check()` 零 IO 且可重入、熔断与重启后峰值仍在、`KillSwitch` 没有一键清仓、阈值变更的收紧/放宽（放宽未确认转 PENDING）、LKG 回退与 `RISK_011`。它守的是**引擎**，看不见回测接线（那条缝由下一行守） |
| `tests/test_backtest_risk_gate.py` | I3 的端到端回归测试 12 条：闸门**默认关闭**（不接线就不拦，I1 口径不变）、接上后每单必过一遍、缩到 0 股＝`REJECT`（不是 REDUCE）、`KillSwitch` 触发后**进市场的订单为 0**、「阈值全放宽 ≡ 不接闸门」这条等价关系、统计**不进**回测报告。含一条把 `_strategy_equity` 的**符号 bug** 钉死的用例（满仓时权益必须为正，否则假回撤→误熔断，症状是「第三笔单莫名被拒」） |
| `tests/test_dashboard.py` | I4 的绩效看板回归测试 36 条，分 6 节：① **字段清单**（看板常量表与 `PerformanceMetrics` 的 dataclass 字段表**双向相等**，不许自建指标）；② **读数只来自报告**（逐字段取自 payload、结构段缺失显示 `-` 而不是 0、`max_drawdown` 取 `max` 不取 `min`）；③ **防空转**（缺段/缺字段/负数/`NaN`/非 ISO 时间戳一律抛 `DashboardError` 并点名字段，绝不退回 0）；④ **渲染确定性**（`text`/`html` 各渲染两次**逐字节相同**、`html` 自包含、文本可 GBK 打印）；⑤ **跨层**（真 `BacktestResult` 落盘 → 读回 → 与两个绩效指标对拍）；⑥ **CLI 接线**（`dashboard` 子命令：真从磁盘读文件，断言 CLI 产出的字节与库函数**逐字节相等**）。⑤⑥ 两节是「两边各自全绿 ≠ 接起来能跑」那条纪律的又一次落地 —— §1~§5 全绿时 `run_dashboard` 一行都没跑过 |
| `.github/workflows/ci.yml` | 只两条 `run:`（`pytest -q` 与 `run_all_gates.py`），**禁止 `|| true` 吞退出码**；2026-09-24 起已在 GitHub 上真跑（`origin` 公开）—— 首次真跑抓到的环境依赖判据已修 |
| `.venv/` | 仓库内虚拟环境，**本机手工装**：`pandas` / `numpy` / `psycopg` + `psycopg-binary` / `pytest`（2026-09-25 现取）。其中 **`numpy` 仍不在 `pyproject.toml` 的声明里**；`psycopg` 已按 2026-09-25 裁决声明进 `[postgres]` extra（**本机这一份仍是手工装的** —— 声明它只是让「怎么装出来」可复现）⇒ **extra 是 opt-in、CI 只装 `.[dev]`**，所以 `pytest_mutation_check.py` 里「把惰性驱动导入改成模块顶层拉驱动」那条变异在 CI 上仍是 `ENV-LIMIT` 而不是 `CAUGHT`；**不入库**（`.gitignore`） |

### D. 数据库

| 文件 | 规模 | 状态 |
|---|---|---|
| `db/risk_control.sql` | 9 表 / 14 CHECK / 幂等种子 | ✅ **已执行**（2026-09-24 起四个镜像：14.24 / 15.18 / 16.15 / 17.11） |
| `db/risk_control.smoke.sql` | 18 KB 约束触发测试 | ✅ **已执行**：23 passed / 0 failed（四个镜像各一次） |
| `db/data_center.sql` | 9 表 / 16 CHECK / 1 部分唯一索引 | ✅ **已执行**（2026-09-24 起四个镜像：14.24 / 15.18 / 16.15 / 17.11） |
| `db/data_center.smoke.sql` | 38 KB 约束触发测试（A 前置 / B 16 条必须拒绝 / C 枚举与边界必须接受） | ✅ **已执行**：42 passed / 0 failed（四个镜像各一次） |

> 两份 `*.smoke.sql` 都已在**容器 PostgreSQL** 上跑过：`postgres:14`(14.24) / `15`(15.18) /
> `16`(16.15) / `17`(17.11)，每版一份快照 `tools/sql-smoke-report-pg14.txt` /
> `tools/sql-smoke-report-pg15.txt` / `tools/sql-smoke-report-pg16.txt` /
> `tools/sql-smoke-report.txt`（均含镜像 digest），30 条 CHECK 不再是纸面防线。
> 注意：地图里写路径必须写成**能落到文件上的**形式。上面这几行原本用的是花括号速记
> （pg1 + 花括号 4,5,6 + .txt）与斜杠速记（postgres:14/15/16），被 skeleton 门禁的
> MAP-PATHS 当场判红：花括号不是文件系统通配、`postgres:14` 也不是目录 —— 是**地图**
> 写错了而不是门禁太严。这两个速记形式在本文里**故意不加反引号**（MAP-PATHS 只扫反引号里的
> 内容，加了就等于把坏地址重新递给它）。
> 但要分清两件事：**绿色本身不会告诉你有牙** —— 那要靠 `tools/falsify_smoke.py` 去证伪
> （逐条放宽 CHECK 表达式，看它是否变红；现已覆盖 **31/31**，但**只在 `postgres:17` 上做过**）。
> 同时记住边界：只跑过**这四个 tag**，其余 tag / 发行版从未跑过，改过 `db/*.sql` 后那轮结论即作废。
> 每个 DDL 头部有一行 `-- PG-VERIFIED-ON:` 声明验过哪些版本，它由 `verify_data_center.py` 的 C6
> 与 `verify_risk_config.py` 的 C17 拿 `tools/sql-smoke-report*.txt` 里的镜像名**双向**核对 ——
> 多写一个没跑过的版本会红，少写一个跑过的也红。

### E. 门禁与工具

| 文件 | 作用 |
|---|---|
| `tools/run_all_gates.py` | ★ **统一入口**，一条命令跑全部门禁并给出一个总判据 |
| `tools/gates-report.txt` | 上一次运行的完整输出 —— **看结果读它，不要重跑** |
| `tools/dev.py` | 本地开发环的**单一入口**（`check` / `full` / `gate` / `test` / `status` / `--selftest`）：一次调用拿 pytest + 门禁的总判据，省掉 PS 5.1 的编码税。**故意不做**：缓存判定（缓存判定＝假绿工厂）、建第二份门禁注册表、按改动路由门禁（少跑却报绿）；**只打 ASCII**，中文细节留在 `tools/gates-report.txt` |
| `tools/gates-baseline.json` | tier-B 棘轮基线，**手工维护**，禁止自动写入 |
| `tools/verify_md_coverage.py` | docx 的每一段（含表格单元）是否都出现在对应 md 中；另断言声明的追加段落（`ADDENDA`）未被重新转换抹掉 |
| `tools/verify_risk_config.py` | 风控契约 §3.6 与 DDL 与 smoke.sql 三方一致 |
| `tools/verify_contract_refs.py` | 契约引用的类型/异常是否有定义（B7 欠账所在） |
| `tools/verify_appendix_refs.py` | ★ **新门禁**（`appendix-refs`）：**散文里的交叉引用**是否指向真实存在的附录条目。扫 `git ls-files` 的全部文本，把各契约/PRD 文档的附录字母与条目号建成索引，再核对每个「某契约 + 附录标签」的引用：字母不属于任何文档 ⇒ `APX-UNKNOWN-APPENDIX`、字母在但条目号不存在 ⇒ `APX-DANGLING-ITEM`、点名了文档而该文档没有这个附录 ⇒ `APX-WRONG-CONTRACT`（全字匹配；斜杠/波浪号连写与并列续写都展开）。**只读文本**：不执行被检代码、不联网；文件清单取自 `git ls-files`，拿不到就**拒判**而不是空转；另有一组空转守卫（引用为 0 / 索引为空 / 判据退化时一律拒判，而不是打印「0 问题」）。它的存在理由：附录被重编号或整段删掉后，文档里那句地址仍印着、其余门禁全绿，而**去取裁决的 agent 会落到空地址**。边界：只证明「这个标签存在」，**证明不了标签背后的裁决仍然成立** |
| `tools/verify_data_center.py` | 数据中心契约 §3.6.1 与 DDL 约束清单双向一致（C1~C7）+ 对 `db/data_center.smoke.sql` 的触发测试覆盖核对（C7） |
| `tools/verify_data_center_pit.py` | ★ **I2 S1 新门禁**：`DataFeed` 取数路径的 `as_of` **两层防线**（显式日期参数越界必须抛 / 经 guard 逐行登记）+ 回测会话下 `QFQ`/`BFILL` 必须被拒 + 只有 `DataCenter.as_of()` 能产出 `DataFeed`。「越界＝抛而非裁剪」这条语义的机器判据就在这里。**只解析 AST，从不执行被检代码、不跑 pytest** |
| `tools/verify_data_center_adapter.py` | ★ **I2 S2 新门禁**（`data-center-adapter`）：采集侧适配器把源列名/取值翻成标准 schema（A1/A2/A4/A8）、采集层不含数据库驱动（A5）、源 SDK 只能惰性 import（A6）、行对象不暴露源字段名（A7）、新映射表/列名元组未登记即报错（A3/A9）、两个空转守卫（A0/A10）。**只解析源码文本**：不执行适配器、不联网、不 import pandas |
| `tools/verify_iteration_plan.py` | 迭代计划里每个标「已交付」的迭代是否引用了一条**真实存在**的证据路径（防幽灵 ✅）+ DoD 四件套形状 |
| `tools/verify_skeleton.py` | I0 骨架是否还在（pyproject / 包 / 测试 / CI 的 `run:` 接线），**不跑 pytest**；另守 `CONTEXT.md` 三件事不许过期 —— 状态陈述（`IMPL-STATUS`）、写死的门禁计数（`GATE-COUNT`）、**地图里的路径必须真实存在**（`MAP-PATHS`）。⚠️ 它们只管结构，**看不见错字/乱码**：改完中文文案要回读核对 |
| `tools/verify_backtest_reproducibility.py` | 回测可复现性：同种子两次跑 `deterministic` 段逐字节一致、换种子必须真的改变、样本非空、段结构合规、入库证据不可是过期快照（R1~R5）；**默认只读**，重录证据要显式 `--record` |
| `tools/verify_contract_signature.py` | 契约签名与实现**双向**一致（S1~S7）+ 未登记成员 / 未实现缺口清单；机器可读投影是 `tools/contract-signature-manifest.json` |
| `tools/contract-signature-manifest.json` | 契约签名的机器可读投影（`classes` / `impl_only` / `non_normative_blocks` / `not_implemented` / `extras`）。`impl_only` 是**只有实现、契约没写**的类（如数据源适配器三角色），只能做反向漂移检查 |
| `tools/verify_dashboard.py` | ★ **I4 新门禁**（`dashboard-consistency`）：C1~C10。C6 直接禁掉落进看板模块的统计函数与统计模块（`performance`/`statistics`/`numpy`/`pandas`/`scipy` 任一 import，或 16 个统计函数名之一 ⇒ FAIL）；C8 双向扫「改一个数，读数必须跟着动」；**C10 是这个门禁的参照物** —— 它用报告自带的 `trades`/`orders`/`account_history` **现场重跑真的 `PerformanceAnalyzer`**，与报告里存的 14 个指标逐字段比。没有 C10，整个门禁就是恒等式（C3 两端同源、手改报告会把两边一起改掉）。**边界**：C10 的参照物就是写出这些数的同一个分析器 ⇒ 「公式本身算错了」它看不见；它证明的是「报告新鲜、没被手改过」 |
| `tools/dashboard_failure_demo.py` | I4 DoD「触发测试」的**可复查脚本**：手工把报告里的一个指标改掉，确认 C10 会红。跑一次落 `tools/dashboard-failure-report.txt`；**不是门禁**（改的是临时副本，不改仓库里的报告） |
| `tools/dashboard-failure-report.txt` | 上一条的输出 —— **快照**：那一行 `ISSUE [C10] <字段>: the report stores … but re-running PerformanceAnalyzer … yields …` 就是 DoD 要的「一次真实的失败记录」 |
| `tools/pytest_mutation_check.py` | 把实现逐处改坏，验证基线**六套件**（`tests/test_backtest_slice.py` / `tests/test_data_center_store.py` / `tests/test_backtest_db_feed.py` / `tests/test_backtest_risk_gate.py` / `tests/test_risk_store.py` / `tests/test_dashboard.py`）真的会红（**不是门禁**：它验证的是测试，且慢。2026-09-25 I4 后实测 **49 变异全被抓 + 5 条 CONTROL + 0 条 ENV-LIMIT**（基线六套件 `passed=181`）；更早两轮是 43 条样本 / 39 + 4 + 0 与 32 条样本 / 29 + 3 + 0。这一栏随环境变：`psycopg` 只声明在 `[postgres]` extra 里（opt-in）、本机 `.venv` 是手工装的、CI 只装 `.[dev]` ⇒ 那边驱动相关的变异会退回 `ENV-LIMIT` —— **`ENV-LIMIT` 逐条预写理由、不计入 CAUGHT 分母、绝不事后追认**） |
| `tools/pytest-mutation-report.txt` | 上一条的逐变异证据 —— **快照**，改实现或改测试后作废 |
| `.rounds/i1/` | I1 可复现性门禁的证据样本（同种子两份 + 换种子一份，共三份）—— 由 `--record` 显式重录，**跑门禁不会改它们** |
| `tools/ci_dryrun.py` | 在本地把 CI 的两条命令真跑一遍 + 两次红→绿往返（含清 `__pycache__`），写 `tools/ci-dryrun-report.txt`。**不是门禁**（需 .venv） |
| `tools/ci-dryrun-report.txt` | CI 的**本地等价**证据 —— **快照**，只对报告内的解释器与 commit 成立 |
| `tools/docx2md.py` | docx→md 转换器（**重跑会覆盖产物 A**） |
| `tools/dump_docx_paras.py` | 定位 docx 段落，排查 md-fidelity 差异时的取证工具 |
| `docker-compose.smoke.yml` | 一次性 PostgreSQL 容器（无 `ports:`、无命名卷 ⇒ `down -v` 必得空库） |
| `docker-compose.dev.yml` | **本机开发库**（发布 127.0.0.1:55432、带命名卷 ⇒ 数据会留在 `quan-auto-dev` 项目里，`down -v` 才清）。为「用客户端连库看数据」而建；项目名与 smoke 的 `quan-auto` 不同 ⇒ 与 `run_sql_smoke.py` 可同时存在、互不拆。**它不是证据通道**：那份文件的头部注释里写清了它一个字都不许被门禁/DoD/契约引用。它之所以另立一份而不是给 smoke 加 `ports:`，为了守住 smoke 头部那两条设计约束（不发布端口 = 不和真实实例抢端口；发布端口会让门禁在端口被占时以环境性失败变红） |
| `tools/run_sql_smoke.py` | ★ **运行时验证通道**：起库 → 验空 → 两份 DDL+触发测试 → 落报告 |
| `tools/falsify_smoke.py` | 证伪器：逐条放宽 CHECK 表达式，确认触测会变红（目前 31/31 CAUGHT / 30 条约束，**只在 `postgres:17` 上做过**） |
| `tools/sql-smoke-report.txt` | 运行时证据（含镜像 digest）—— **快照**，改 `db/*.sql` 后作废；这份是默认路径那份 = `postgres:17`(17.11) |
| `tools/sql-smoke-report-pg14.txt` / `tools/sql-smoke-report-pg15.txt` / `tools/sql-smoke-report-pg16.txt` | 版本阶梯的另三份证据（`postgres:14` 14.24 / `15` 15.18 / `16` 16.15，各 23/0 与 42/0）—— 由 `--report=` 另存，互不覆盖；同样是**快照** |
| `tools/falsify-report.txt` | 上述证伪的逐案例证据 —— **快照**，改 `db/*.sql` 或 `*.smoke.sql` 后作废 |

## 4. 怎么跑门禁

```powershell
python tools/run_all_gates.py                 # 跑全部，打印总表（推荐）
python tools/run_all_gates.py --gate=NAME     # 只跑一个
python tools/run_all_gates.py --list          # 列出注册表
python tools/run_all_gates.py --full          # 连每个门禁的原始输出一起打
python tools/run_all_gates.py --selftest      # 证明这个 harness 自己会报 FAIL
```

退出码：`0` 全绿 / `1` 有门禁失败或 harness 自身无法完成检查 / `2` harness 自测失败。

**单独调用某个门禁时的必填参数**（漏了会报错或**静默误报**，这两个坑都真踩过）：

| 门禁 | 命令 | 注意 |
|---|---|---|
| md-fidelity | `python tools/verify_md_coverage.py docs` | **必须给目录参数**；漏了在 148 行抛 `IndexError` 且 stdout 为空，看起来像「门禁崩了」 |
| risk-config | `python tools/verify_risk_config.py` | 无参数（可选末尾跟 workspace root） |
| data-center-ddl | `python tools/verify_data_center.py` | 无参数 |
| data-center-pit | `python tools/verify_data_center_pit.py` | 无参数。**只解析源码**，不执行被检代码、不跑 pytest |
| data-center-adapter | `python tools/verify_data_center_adapter.py` | 无参数。**只解析源码文本**，不执行适配器、不联网、**不 import pandas** —— 它证明不了「映射出来的数据是对的」（DC 契约附录 B10） |
| iteration-plan | `python tools/verify_iteration_plan.py` | 无参数（可选末尾跟一个文件路径，用于把守卫指向别的文件） |
| core-contract-refs | `python tools/verify_contract_refs.py --extra=docs/智能量化交易平台-数据中心接口契约文档.md --extra=docs/智能量化交易平台-风控层接口契约文档.md` | **少一个 `--extra=` 就会把已定义的类型误报成缺失**，虚增基线、掩盖真实漂移 |
| skeleton | `python tools/verify_skeleton.py` | 无参数（可选末尾跟一个目录，用于把守卫指向别的根）。**它不跑 pytest**，别把它当回归测试用 |
| backtest-reproducibility | `python tools/verify_backtest_reproducibility.py` | 无参数（重录证据要显式 `--record`）。**默认只读**，跑门禁不会改 `.rounds/` 样本；它只证明**本机**可复现，不证明跨机器/跨版本 |
| contract-signature | `python tools/verify_contract_signature.py` | 无参数。契约侧签名必须能在契约文档里**逐字**找到 ⇒ 它管的是「有人单边改了签名」，不是「三方都对」；**不比返回类型**（契约引用了大量本项目不存在的类型），只比参数列表与数据类字段名 |
| dashboard-consistency | `python tools/verify_dashboard.py` | 无参数。**只解析源码 + 跑真的 `PerformanceAnalyzer`**，不起浏览器、不联网。C10 的参照物是写出这些数的同一个分析器 ⇒ 「公式本身错了」它看不见（核心契约 §F5 记了这个边界） |
| appendix-refs | `python tools/verify_appendix_refs.py` | 无参数。**只读文本**：不执行被检代码、不联网；文件清单取自 `git ls-files`，**拿不到就拒判**（不在 git 仓库里跑会红）。边界：只证明标签存在，**证明不了标签背后的裁决仍然成立** |

每个门禁都支持 `--selftest`。

### 4.1 运行时验证（跑真实 SQL，不属于门禁）

```powershell
python tools/run_sql_smoke.py            # 起容器 → 验空库 → 跑 DDL+触测 → 写报告 → 销毁
python tools/run_sql_smoke.py --selftest # 自证判定逻辑（1 正 + 5 负样本）
python tools/run_sql_smoke.py --keep     # 失败时留容器排查
python tools/falsify_smoke.py            # 证伪那两份绿（逐条放宽约束看它会不会红）
```

退出码：`0` 两份都 `SMOKE PASS` / `1` 有触发测试失败（**约束没拦住**）/ `2` 空库守卫或脚本自检失败 / `3` 无 docker。

**想拿客户端连库看数据**（DBeaver / psql / VS Code 数据库扩展）走的是**另一份** compose，不是上面这份：

```powershell
docker compose -f docker-compose.dev.yml up -d --wait db   # 127.0.0.1:55432 / 库 quan / 用户 postgres
docker compose -f docker-compose.dev.yml down -v           # 删库重置
```

它发布端口、带命名卷，所以**数据会留下来** —— 这也意味着它的库会**非空**，而 `db/*.sql` 全是
`CREATE TABLE IF NOT EXISTS`：在里面重跑 DDL 会**静默什么都不做**（改了约束却看不到变化时，
先 `down -v` 重置，别怀疑自己没跑对）。它跑出来的任何结果**都不是证据**（smoke 报告里的
结论只出自 `docker-compose.smoke.yml` 那条通道），原因见上面 §E 表格里 `docker-compose.dev.yml` 那一行。

**`run_sql_smoke.py` 为什么不在 `run_all_gates.py` 里**：没 docker 的机器上，注册进去要么报一条环境性 FAIL，
要么被静默 SKIP 后**打印绿色** —— 后者正是本项目反复踩的假门禁。要接进来必须先做到
「无 docker ⇒ 明确 `SKIPPED` 且计入跳过数」。想看它的结论就读 `tools/sql-smoke-report.txt`。

`falsify_smoke.py` 的退出码另有一套：`0` 每个案例都被抓到变红 / `1` 有案例没被抓到（触测是摆设）/ `2` 自 assert 失败。
它**每跑一条案例就 `DROP SCHEMA public CASCADE` 重建** —— 因为 `db/*.sql` 全是
`CREATE TABLE IF NOT EXISTS`，不清库的话第二条案例改的 DDL 会**静默空转**（表已存在），
于是「变异没送进数据库」会被读成「触测没用」。结果写 `tools/falsify-report.txt`。

**读 `tools/gates-report.txt` 的姿势（踩过）**：它是 **UTF-8 无 BOM**。PS 5.1 的 `Get-Content`
不带 `-Encoding` 时按 **GBK** 解码，中文路径会显示成 `鏅鸿兘閲忓寲浜ゆ槗...`。这**不是**文件坏了，
是读法错了 —— 实测同一文件用编辑器读完全正确。请用 `Get-Content -Encoding utf8`、`read_file`
或编辑器。曾据此误判成 harness 编码 bug，差点去「修」一个一直正确的文件。

## 5. 门禁为什么会这样设计（不要「简化」掉）

- **两层制**：tier-A 是我们能控制的产物，必须全绿；tier-B 是今天还还不上的欠账（B7），
  不要求退出码为 0，只要求**发现数等于基线**。
- **对称棘轮**：`actual < baseline` 也判 FAIL —— 否则一次改进被静默吸收，数字从此失去意义。
  欠账下降时必须**手工**改小 `gates-baseline.json` 的 `total` 并写明原因。
- **没有 `--write-baseline`**：自动更新会让任何回归在重跑一次的瞬间被吸收，正是本项目反复踩的「假门禁」。
- **每个门禁跑两遍**：先 `--selftest` 再跑真的。自测失败一律判 FAIL —— 看不到它失败的探测器不算探测器。

## 6. 当前状态（一句话版）

**仓库**：已是 git 仓库（`main` 分支，2026-09-23 建立基线提交 `053f620`）。受控文件数量**不在此写死**（它会过期）：现取 `git ls-files`。
提交粒度是**整轮提交**（没有细粒度还原点）：基线 `053f620` → 契约/门禁若干 → **I0 骨架 `fe9e060`**，
其后仍是每个迭代一个提交；**当前提交数与 HEAD 一律现取 `git log --oneline`，不要在上面写死数字**
（写死的数字每次提交都会过期，本项目已经在「过时计数」上踩过两次）。
**I0 的「可回滚」与「可运行」两半都已交付，I1 又在其上打了第一条竖切，I2 S1 加厚了 PIT / `as_of` 边界层，I2 S2 把采集侧适配器接上、I2 S3 前半把落库侧读写接上、S3 后半让引擎改读 `as_of()` 产出的 feed，I3 让风控真正介入下单路径（默认关闭），**I3b 小步把风控的存储层与拦截留痕真接上**（留痕由**调用方**写、不是 `RiskEngine` 里写 —— 为了不和 D1「`check()` 内禁 IO」冲突，偏离记录在风控契约 §七），**I4 交付绩效看板**（读一份 `BacktestResult` → 指标表 + 资金曲线；**纯读数**，读数口径登记在核心契约 §F，一致性靠「现场重跑 `PerformanceAnalyzer`」而不是比两个同源数字），I2 的收尾又拆成 I2a（失败可分类 + 东财传输层）与 I2b（真实取数联调：腾讯日线通道 / 东财财务表逐键核对 / 第一次真的入过库）**：
`.venv` + pytest（数量**现取** `python -m pytest -q`；2026-09-25 I4 后实测 `373 passed` = 骨架 3 + 竖切 22 + PIT 18 + 适配器 104 + 落库侧 37 + 端到端接线 10 + 风控引擎 67 + 风控闸门 15 + 风控存储 61 + 看板 36。**更早三份实测**（`337 passed` 是 2026-09-25 I3b 那一刻；`273 passed` = 上一式 + 风控闸门 12；`193 passed` 是 2026-09-24 I3 那一刻，适配器 32 / 落库侧 29）都留着 —— 别的文件要引用时**按现值取**，别抄旧的那份）、
`pyproject.toml`（`dependencies = ["pandas>=2.0"]` —— S2 起**不再是空数组**，理由见 §1；源 SDK 在 `[datasources]` extra 里；
**`psycopg` 声明在 `[postgres]` extra 里**（2026-09-25 裁决，同 [datasources] 的 opt-in 口径）：`pgstore.py` 里**惰性导入**，本机 `.venv` 是**手工装**的 3.3.6、CI 只装 `.[dev]` ⇒ 那条「驱动改成顶层导入」的变异在 CI 上仍是 `ENV-LIMIT`）、
`quanauto/` 包（**15 个模块**：14 个实现 + 入口 `quanauto.cli`；条目数现取 `quanauto/*.py`）、
`tests/`（骨架自检 3 条 + 竖切回归 22 条 + PIT 回归 18 条 + 适配器回归 **104** 条 + 落库侧回归 **37** 条 + 端到端接线回归 10 条 + 风控引擎回归 67 条 + 风控闸门回归 **15** 条 + 风控存储回归 **61** 条 + 看板回归 **36** 条）、`.github/workflows/ci.yml`。
**I2 仍未收口**（2026-09-25 更新；上一版这里写「真实数据源**从未联网联调**」，那句话**已过期**）：S1 交付 PIT 边界层、S2 交付采集侧适配器、S3 前半交付落库侧读写、S3 后半让回测改读 `as_of()` 产出的 feed；
此后 **I2a-2** 把东财端点用标准库 `urllib` 真接上（opener 可注入 ⇒ 离线可测）、**I2b-1** 落地腾讯日线通道并跑通一次真实取数冒烟、
**I2b-2** 把财务表列名与日期过滤语法逐键对过真实返回、**I2b-3** 第一次**真的入过库**（真 PostgreSQL 读回逐字段对拍 —— 并当场咬出两个离线套件看不见的静默缺陷，DC 契约附录 **B20**）。
**仍未满足的收口条件**：① **复权因子仍恒为 1.0**（已知缺口，DC 契约附录 A 记着、附录 B 复述并给出关闭条件）；② 各通道「哪些位置已实测、哪些仍是推断」的未确认清单没清空（登记在 DC 契约附录 **B14 / B16 / B17**）。
⇒ `docs/迭代计划.md` 里 I2 的「产物」行**没有打勾**是**如实**，不是漏填。另有两条永远不动的边界：真实取数天然不可复现、依赖外部站点存活，所以它**不得成为任何门禁或 DoD 的硬要求**；`pytest` 默认套件里不许出现联网用例。
CI 自 2026-09-24 起**在 GitHub 上真跑**（`origin` 公开，`main` 已推）：**首次真跑判红**
—— `skeleton` 抓到本文件里的 `.venv/` 在克隆里不存在（本机存在），同一个 commit、同一份判据
两边结论不同 ⇒ 判据依赖环境，已修并补触发样本；修复后重推，第二次运行
（2026-09-24，run 35939823649）**success**。本地等价证据
`tools/ci-dryrun-report.txt`（含两次红→绿往返）给的是本机解释器的口径，改完代码要
`python tools/ci_dryrun.py` 重跑 —— 两份证据**不能互相替代**。

门禁数量一律**现取**（`python tools/run_all_gates.py --list`），不在这里写死：
tier-A 全绿；`core-contract-refs` 是 tier-B 欠账，基线 **39**（T1=2 / T2=19 / T3=18）。
最大阻塞是 **B7**：主契约引用了 19 个类型和 18 个异常却从未定义，还有 2 个 python 块无法解析。
**数据库侧已不再是「运行时未证实」**：两份触发测试 2026-09-23 先在容器 `postgres:17`（17.11）上
各拿到 **23/0** 与 **42/0** PASS，2026-09-24 又在 `postgres:14`(14.24) / `15`(15.18) / `16`(16.15)
上各跑一遍，同样是 **23/0** 与 **42/0** —— 即「四个 tag 上都通过」是实测的。
这两份绿的**每一条命名 CHECK 都已被 `tools/falsify_smoke.py` 单独证伪（31/31 CAUGHT）** ——
不再是抽样，但那次证伪**只在 `postgres:17` 上做过**，另三个镜像上只有冒烟层面的通过。
细节与编号一律以 `docs/开工前缺口清单.md` 为准，不要在本文件里复制它。

## 7. 工作纪律（每条都对应一次真实踩坑）

1. **每个自建门禁必须做触发测试**：构造一个注定失败的输入，确认它真的 FAIL。只跑全绿＝假门禁。
2. **提取为空必须判 FAIL**：校验器一旦提取失配，所有探测项都在空转，却会打印「0 问题 PASS」——
   比真失败更隐蔽的假绿。每个提取结果后面都要跟一条硬守卫。
3. **一个探测器一个样本**：若检查项串联且前置项会 `return`/`raise`，只测一个坏样本会让后面
   几项根本没跑却看不出来。样本至少三类：触发第 1 项的、触发第 2~N 项的、**期望 0 报错的干净样本**。
4. **绝不为了通过而削弱检查**：门禁报错时改产物或改检查器，不改判据。
5. **基线只能是实测值**：`gates-baseline.json` 里的数字必须来自真实运行，不能是估算。
6. **改完文档必须重测行号引用**：`L###` / 「第 N 行」会漂移，改完要重新实测。
7. **不要用终端整文件改写源文件**：PS 5.1 按 GBK 落盘会把中文注释变乱码。本仓库（C5）
   虽有 git 且已按迭代提交，但**提交粒度是整轮**（一次改动可能横跨十几个文件），
   把某个文件写坏后再 `git checkout --` 只能退回**上一轮**的状态，中间的手工编辑会一起丢。
   改代码一律用编辑工具；真被写坏就先 `git checkout -- <file>` 还原、再重做编辑。
8. **变异样本必须自 assert**：构造坏样本后要断言它**真的被改到了**（命中 1 处、且与原文件不同），
   否则你是在**未修改的文件**上得出「全绿」的结论。这条踩过两次。
9. **覆盖判据要钉到「形状」而不是「名字出现过」**：核对触发测试覆盖时，若判据只是「约束名
   在代码行里出现过」，那么删掉整个拒绝样本段它仍会报满覆盖。必须钉到
   「拒绝之后断言是它拒绝的」这种等式形状，并先剥掉 SQL 注释（否则注释掉断言也算覆盖）。
   `tools/verify_data_center.py` 的 `NEG6`~`NEG9` 就是这条的三个触发样本。
10. **静态检查核对不了语义**：门禁能证明「用例还在、覆盖齐」，证明不了「样本值真的落在拒绝
    区间内」；后者只能靠真实执行 —— 走 `tools/run_sql_smoke.py` 的容器通道（见 §4.1），
    过去「本机无 PostgreSQL ⇒ 一律未证实」的说法自 2026-09-23 起不再成立。
11. **第一次全绿必须先去证伪**：一份从未变红过的触测，和一份什么都没测的触测看起来一样。
    做法是只**放宽表达式**（不删约束）跑 `tools/falsify_smoke.py`，期望 `n_pass = 总数-1`、`n_fail = 1`。
12. **跑多个样本前先确认上一个没留下现场**：`CREATE TABLE IF NOT EXISTS` 使「同一库里再跑一遍
    改过的 DDL」变成对**旧表**跑测试 ⇒ 多案例一次起库时必须**每案例重建库/清 schema**，
    并断言清干净了。2026-09-23 把证伪案例从 2 条扩到 31 条时正是撞在这里。
13. **报绿前先自报分母**：`n passed` 只是分子。某一段整块停止执行只会让数字变小、不会变红，
    所以「分母是多少、谁在保证分母」必须能回答（本项目靠静态门禁 `C7` / `covered_by_smoke`）。
14. **改前先验事实，尤其当「事实」是别人写下来的**：本仓库多处长期写着「不是 git 仓库」，
    而 `.git` 其实一直存在（只是 0 提交）—— 结论碰巧对、依据是错的。凡要拿一条陈述
    当判据前提，先用命令实测它，再决定要不要改。
