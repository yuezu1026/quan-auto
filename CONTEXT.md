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
`quanauto/datasources.py`**；落库与回测改读尚未开始）。
依赖清单从 I2 S2 起**不再为空**：`dependencies = ["pandas>=2.0"]`（使用者 `quanauto/datasources.py`，
数据中心契约 §3.2 的方法签名逐字写了 `pd.DataFrame`）+ `[datasources]` extra（`akshare` / `baostock`，
适配器对它们是**惰性 import**）。I0/I1 当时的空清单是**选择不是缺口** —— 那时的竖切只用标准库。

## 2. 硬约束（违反任何一条都会造成不可逆损失）

| # | 约束 | 后果 |
|---|---|---|
| C1 | 本机**没有本地 PostgreSQL 实例**：`psql` 不在 PATH、无服务、无安装目录。容器通道**可用**（Docker Desktop 已跑起来） | 不要再说「从未执行」—— 2026-09-23 起 `db/*.sql` 已在容器 `postgres:17` 上执行过（触测 23/0 与 42/0 PASS）。但也**不能反过来说「约束已验证」**：只覆盖那一个镜像，「PostgreSQL 14+」仍未证实，改过 SQL 后结论即作废 |
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
| `docs/开发工作流规范.md` | ★ **开工前的作业规程**：产物四件套 / TDD 红源 / 四类漂移 / 对称棘轮 / 触发测试硬要求 / 六类真实踩坑案例 / token 分层 / 红线清单 |
| `docs/迭代计划.md` | ★ **交付节奏**：竖切 **I0~I4**（股票池那一轮已于 2026-09-23 裁决砍掉）、每个迭代的 DoD 四件套、B7 棘轮燃尽表、不采用的敏捷做法。原「待决」问题（迭代计划里那 5 条 + 更早的 Q1/Q7）已全部裁决并回填，**已无未决项** |
| `docs/智能量化交易平台-风控层接口契约文档.md` | 风控层契约（补充主契约缺失的类型/异常） |
| `docs/智能量化交易平台-数据中心接口契约文档.md` | 数据中心契约，含 D1~D10 设计决策 + **附录 A**（I2 S1 实现侧裁决与偏差登记：`DataFeedError` 继承、`SessionMode` 本地类型、越界两种语义、复权因子恒 1.0 这个已知缺口）+ **附录 B**（I2 S2 适配器裁决与偏差：`ValidationReport` 升格为 5 字段、pandas 是显式依赖、源 SDK 只能惰性导入、符号后缀与指数歧义、两处“安静的事故”（手/股、百分号）、`report_type` 必须适配器层映射完，另记两条已知缺口：停牌/涨跌停标记无处可落（契约内部不一致）、龙虎榜/资金流向无标准 schema）+ **附录 C**（I2 S3 落库侧裁决与偏差：`DataStoreError`(DATA_008) 属实现侧新增异常码、假连接与真库的效力边界、`PsycopgConnection` 的复用与事务语义）。**手写契约，没有 `.docx`** ⇒ 可原地编辑，不进 C2 的 `ADDENDA` |

### C. 工程骨架（2026-09-23 I0 交付；守门门禁 = `skeleton`）

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 包元数据 / pytest 配置。依赖**由真正用它的模块带进来**，禁预置「以后大概会用」的包：现在是 `pandas>=2.0`（使用者 `quanauto/datasources.py`），源 SDK 走 `[datasources]` extra（I0/I1 当时是空数组，因为那时的竖切只用标准库） |
| `quanauto/__init__.py` | 包入口，只有 `__version__` 与 `__all__`（其它模块下的实现不在此 re-export：否则「实现在那里」与「一 import 就全拉起来」长得一样） |
| `quanauto/*.py`（13 个模块：12 个实现 + 入口 `cli.py`） | I1 的产物：`models` / `enums` / `errors` / `events` / `datafeed` / `strategies` / `broker` / `engine` / `performance` / `cli`；**I2 S1 追加 `datacenter.py`**（PIT / `as_of` 边界层）、**I2 S2 追加 `datasources.py`**（采集侧适配器：源列名/取值 → 标准 schema + `ValidationReport`）、**I2 S3 追加 `pgstore.py`**（落库侧：`PgBarStore` 读 / `PgBarIngestor` 幂等 upsert / `PsycopgConnection` 惰性驱动适配）。守门门禁 = `contract-signature`（签名不许偏离契约）+ `data-center-pit` + `data-center-adapter` |
| `tests/test_skeleton.py` | 骨架自检 3 条：版本与 `pyproject.toml` 一致 / import 不把重依赖拉进来 / 测试数不为 0 |
| `tests/test_backtest_slice.py` | I1 的回归测试 22 条：竖切的不变量（同种子可复现、成交价取下一根 K 线开盘价、拒单不抛异常…）。它**没有**红→绿的 git 证据（实现先于测试），替代证据是 `tools/pytest_mutation_check.py` |
| `tests/test_data_center_pit.py` | I2 S1 的 PIT 回归测试 18 条：预取未来数据必须抛 `FutureDataAccessError`、窗口越界必须抛而**不是**裁剪、回测会话下 `QFQ`/`BFILL` 必须被拒。含 I2 DoD 点名的那条**触发测试**（让 store 无视窗口，确认取数真的被拒）。另有一条**故意断言已知缺口**的用例，`S3` 落地后要**删掉它**而不是改期望值 |
| `tests/test_data_center_adapter.py` | I2 S2 的适配器回归测试 32 条：源列名不许透出下游、映射后必须是标准 schema、手→股与百分号两处换算、裸代码/指数后缀/`report_type` 越界必须抛。它测的是映射**机制**，不是映射**内容**（未联过网，见 DC 契约附录 B10） |
| `tests/test_data_center_store.py` | I2 S3 的落库侧回归测试 29 条：读必须按 `data_version` 过滤、窗口闭区间、upsert 的三条分支（新插入 / 同值不写 / 异值抛 `IngestConflictError`）与并发抢写、异常不许二次包装、驱动必须惰性导入。**一律不连库**：假连接只能证明「发出去的语句/参数是什么」，不能证明 PostgreSQL 接受它 —— 后者只有容器通道能证，两者不得互相冒充（DC 契约附录 C） |
| `tests/fixtures/sample_prices.csv` | 回归测试与可复现性门禁共用的小样本行情 |
| `.github/workflows/ci.yml` | 只两条 `run:`（`pytest -q` 与 `run_all_gates.py`），**禁止 `|| true` 吞退出码**；2026-09-24 起已在 GitHub 上真跑（`origin` 公开）—— 首次真跑抓到的环境依赖判据已修 |
| `.venv/` | 仓库内虚拟环境（只装了 pytest）；**不入库**（`.gitignore`） |

### D. 数据库

| 文件 | 规模 | 状态 |
|---|---|---|
| `db/risk_control.sql` | 9 表 / 14 CHECK / 幂等种子 | ✅ **已执行**（2026-09-23，postgres:17） |
| `db/risk_control.smoke.sql` | 18 KB 约束触发测试 | ✅ **已执行**：23 passed / 0 failed |
| `db/data_center.sql` | 9 表 / 16 CHECK / 1 部分唯一索引 | ✅ **已执行**（2026-09-23，postgres:17） |
| `db/data_center.smoke.sql` | 38 KB 约束触发测试（A 前置 / B 16 条必须拒绝 / C 枚举与边界必须接受） | ✅ **已执行**：42 passed / 0 failed |

> 两份 `*.smoke.sql` 都已在**容器 PostgreSQL** 上跑过（`postgres:17` / PostgreSQL 17.11，
> 证据 `tools/sql-smoke-report.txt` 含镜像 digest），30 条 CHECK 不再是纸面防线。
> 但要分清两件事：**绿色本身不会告诉你有牙** —— 那要靠 `tools/falsify_smoke.py` 去证伪
> （逐条放宽 CHECK 表达式，看它是否变红；现已覆盖 **31/31**）。同时记住边界：只跑过**那一个镜像**，
> DDL 标题里的「PostgreSQL 14+」仍未证实，改过 `db/*.sql` 后那轮结论即作废。

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
| `tools/verify_data_center.py` | 数据中心契约 §3.6.1 与 DDL 约束清单双向一致（C1~C7）+ 对 `db/data_center.smoke.sql` 的触发测试覆盖核对（C7） |
| `tools/verify_data_center_pit.py` | ★ **I2 S1 新门禁**：`DataFeed` 取数路径的 `as_of` **两层防线**（显式日期参数越界必须抛 / 经 guard 逐行登记）+ 回测会话下 `QFQ`/`BFILL` 必须被拒 + 只有 `DataCenter.as_of()` 能产出 `DataFeed`。「越界＝抛而非裁剪」这条语义的机器判据就在这里。**只解析 AST，从不执行被检代码、不跑 pytest** |
| `tools/verify_data_center_adapter.py` | ★ **I2 S2 新门禁**（`data-center-adapter`）：采集侧适配器把源列名/取值翻成标准 schema（A1/A2/A4/A8）、采集层不含数据库驱动（A5）、源 SDK 只能惰性 import（A6）、行对象不暴露源字段名（A7）、新映射表/列名元组未登记即报错（A3/A9）、两个空转守卫（A0/A10）。**只解析源码文本**：不执行适配器、不联网、不 import pandas |
| `tools/verify_iteration_plan.py` | 迭代计划里每个标「已交付」的迭代是否引用了一条**真实存在**的证据路径（防幽灵 ✅）+ DoD 四件套形状 |
| `tools/verify_skeleton.py` | I0 骨架是否还在（pyproject / 包 / 测试 / CI 的 `run:` 接线），**不跑 pytest**；另守 `CONTEXT.md` 三件事不许过期 —— 状态陈述（`IMPL-STATUS`）、写死的门禁计数（`GATE-COUNT`）、**地图里的路径必须真实存在**（`MAP-PATHS`）。⚠️ 它们只管结构，**看不见错字/乱码**：改完中文文案要回读核对 |
| `tools/verify_backtest_reproducibility.py` | 回测可复现性：同种子两次跑 `deterministic` 段逐字节一致、换种子必须真的改变、样本非空、段结构合规、入库证据不可是过期快照（R1~R5）；**默认只读**，重录证据要显式 `--record` |
| `tools/verify_contract_signature.py` | 契约签名与实现**双向**一致（S1~S7）+ 未登记成员 / 未实现缺口清单；机器可读投影是 `tools/contract-signature-manifest.json` |
| `tools/contract-signature-manifest.json` | 契约签名的机器可读投影（`classes` / `impl_only` / `non_normative_blocks` / `not_implemented` / `extras`）。`impl_only` 是**只有实现、契约没写**的类（如数据源适配器三角色），只能做反向漂移检查 |
| `tools/pytest_mutation_check.py` | 把实现逐处改坏，验证 `tests/test_backtest_slice.py` 真的会红（**不是门禁**：它验证的是测试，且慢） |
| `tools/pytest-mutation-report.txt` | 上一条的逐变异证据 —— **快照**，改实现或改测试后作废 |
| `.rounds/i1/` | I1 可复现性门禁的证据样本（同种子两份 + 换种子一份，共三份）—— 由 `--record` 显式重录，**跑门禁不会改它们** |
| `tools/ci_dryrun.py` | 在本地把 CI 的两条命令真跑一遍 + 两次红→绿往返（含清 `__pycache__`），写 `tools/ci-dryrun-report.txt`。**不是门禁**（需 .venv） |
| `tools/ci-dryrun-report.txt` | CI 的**本地等价**证据 —— **快照**，只对报告内的解释器与 commit 成立 |
| `tools/docx2md.py` | docx→md 转换器（**重跑会覆盖产物 A**） |
| `tools/dump_docx_paras.py` | 定位 docx 段落，排查 md-fidelity 差异时的取证工具 |
| `docker-compose.smoke.yml` | 一次性 PostgreSQL 容器（无 `ports:`、无命名卷 ⇒ `down -v` 必得空库） |
| `tools/run_sql_smoke.py` | ★ **运行时验证通道**：起库 → 验空 → 两份 DDL+触发测试 → 落报告 |
| `tools/falsify_smoke.py` | 证伪器：逐条放宽 CHECK 表达式，确认触测会变红（目前 31/31 CAUGHT / 30 条约束） |
| `tools/sql-smoke-report.txt` | 运行时证据（含镜像 digest）—— **快照**，改 `db/*.sql` 后作废 |
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

每个门禁都支持 `--selftest`。

### 4.1 运行时验证（跑真实 SQL，不属于门禁）

```powershell
python tools/run_sql_smoke.py            # 起容器 → 验空库 → 跑 DDL+触测 → 写报告 → 销毁
python tools/run_sql_smoke.py --selftest # 自证判定逻辑（1 正 + 5 负样本）
python tools/run_sql_smoke.py --keep     # 失败时留容器排查
python tools/falsify_smoke.py            # 证伪那两份绿（逐条放宽约束看它会不会红）
```

退出码：`0` 两份都 `SMOKE PASS` / `1` 有触发测试失败（**约束没拦住**）/ `2` 空库守卫或脚本自检失败 / `3` 无 docker。

**它为什么不在 `run_all_gates.py` 里**：没 docker 的机器上，注册进去要么报一条环境性 FAIL，
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
**I0 的「可回滚」与「可运行」两半都已交付，I1 又在其上打了第一条竖切，I2 S1 加厚了 PIT / `as_of` 边界层，I2 S2 把采集侧适配器接上、I2 S3 前半把落库侧读写接上**：
`.venv` + pytest（数量**现取** `python -m pytest -q`；2026-09-24 I2 S3 实测 `104 passed` = 骨架 3 + 竖切 22 + PIT 18 + 适配器 32 + 落库侧 29）、
`pyproject.toml`（`dependencies = ["pandas>=2.0"]` —— S2 起**不再是空数组**，理由见 §1；源 SDK 在 `[datasources]` extra 里；
Psycopg 驱动同理：`pgstore.py` 里**惰性导入**，本机 `.venv` 并未装它）、
`quanauto/` 包（12 个实现模块，入口 `quanauto.cli`）、
`tests/`（骨架自检 3 条 + 竖切回归 22 条 + PIT 回归 18 条 + 适配器回归 32 条 + 落库侧回归 29 条）、`.github/workflows/ci.yml`。
**I2 仍远未完成**：S1 交付 PIT 边界层、S2 交付采集侧适配器、S3 前半交付落库侧读写；**回测改读真实日线还没写**，
复权因子也仍恒为 1.0（已知缺口，DC 契约附录 A 记着、附录 B 复述并给出关闭条件）——
`docs/迭代计划.md` 里 I2 的「产物」行**没有打勾**是**如实**，不是漏填。
CI 自 2026-09-24 起**在 GitHub 上真跑**（`origin` 公开，`main` 已推）：**首次真跑判红**
—— `skeleton` 抓到本文件里的 `.venv/` 在克隆里不存在（本机存在），同一个 commit、同一份判据
两边结论不同 ⇒ 判据依赖环境，已修并补触发样本；修复后重推，第二次运行
（2026-09-24，run 35939823649）**success**。本地等价证据
`tools/ci-dryrun-report.txt`（含两次红→绿往返）给的是本机解释器的口径，改完代码要
`python tools/ci_dryrun.py` 重跑 —— 两份证据**不能互相替代**。

门禁数量一律**现取**（`python tools/run_all_gates.py --list`），不在这里写死：
tier-A 全绿；`core-contract-refs` 是 tier-B 欠账，基线 **39**（T1=2 / T2=19 / T3=18）。
最大阻塞是 **B7**：主契约引用了 19 个类型和 18 个异常却从未定义，还有 2 个 python 块无法解析。
**数据库侧已不再是「运行时未证实」**：2026-09-23 两份触发测试在容器 `postgres:17`（17.11）上
分别是 **23/0** 与 **42/0** PASS，且这两份绿的**每一条命名 CHECK 都已被
`tools/falsify_smoke.py` 单独证伪（31/31 CAUGHT）** —— 不再是抽样。
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
