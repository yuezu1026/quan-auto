# CONTEXT.md — 项目 L0 索引

> **这是索引，不是文档。** 它唯一的职责是让你在不读大文件的前提下知道「该读哪个文件」。
> 任何需要超过 100 行才说清的内容都属于 L1/L2，应写进对应产物，不要往这里塞。
>
> 阅读顺序：`CONTEXT.md`（本文件）→ 按需读 1 个 L1 产物 → 只在必要时读 L2 全文。
> 实测《核心模块接口契约文档》63 KB、《数据中心接口契约文档》48 KB —— 全读一遍是
> 十几万 token，而 90% 的提问只需要 §3.6 的约束清单。

---

## 1. 这是什么

智能量化交易平台的**开工前准备阶段**。仓库里**没有任何可运行代码**，交付物只有三类：
契约、DDL、门禁。真正的实现还没开始，所以这里没有 `src/`、没有依赖清单、没有测试框架。

## 2. 硬约束（违反任何一条都会造成不可逆损失）

| # | 约束 | 后果 |
|---|---|---|
| C1 | 本机**没有本地 PostgreSQL 实例**：`psql` 不在 PATH、无服务、无安装目录。容器通道**可用**（Docker Desktop 已跑起来） | 不要再说「从未执行」—— 2026-09-23 起 `db/*.sql` 已在容器 `postgres:17` 上执行过（触测 23/0 与 42/0 PASS）。但也**不能反过来说「约束已验证」**：只覆盖那一个镜像，「PostgreSQL 14+」仍未证实，改过 SQL 后结论即作废 |
| C2 | `docs/` 下 3 个 `.md` 由 `.docx` 转换生成，**重新转换会覆盖** | 只能在其**之后追加**，禁止原地改已有段落。原地改 = 内容丢失 + md-fidelity 门禁失败 |
| C3 | 控制台是 **cp936(GBK)** | stdout 出现 GBK 之外的字符（如 `↔`）会让 Python 抛 `UnicodeEncodeError` 并丢掉整段输出；PowerShell `>` 写的是 **UTF-16LE** 不是 UTF-8；含中文的 argv 会被破坏 |
| C4 | `tools/` 下**只用标准库** | 不要引入任何第三方依赖 |
| C5 | ~~本仓库不是 git 仓库~~ **已修一半（2026-09-23）**：已是 git 仓库（`main`，基线提交 `053f620`） | 但**只有基线一个提交** ⇒ 只能整体退回基线，没有细粒度还原点。改文件前仍要想清楚 |

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
| `docs/迭代计划.md` | ★ **交付节奏**：竖切 **I0~I4**（股票池那一轮已于 2026-09-23 裁决砍掉）、每个迭代的 DoD 四件套、B7 棘轮燃尽表、不采用的敏捷做法。原「待决」三项已全部裁决并回填 |
| `docs/智能量化交易平台-风控层接口契约文档.md` | 风控层契约（补充主契约缺失的类型/异常） |
| `docs/智能量化交易平台-数据中心接口契约文档.md` | 数据中心契约，含 D1~D10 设计决策 |

### C. 数据库

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

### D. 门禁与工具

| 文件 | 作用 |
|---|---|
| `tools/run_all_gates.py` | ★ **统一入口**，一条命令跑全部门禁并给出一个总判据 |
| `tools/gates-report.txt` | 上一次运行的完整输出 —— **看结果读它，不要重跑** |
| `tools/gates-baseline.json` | tier-B 棘轮基线，**手工维护**，禁止自动写入 |
| `tools/verify_md_coverage.py` | docx 的每一段（含表格单元）是否都出现在对应 md 中 |
| `tools/verify_risk_config.py` | 风控契约 §3.6 与 DDL 与 smoke.sql 三方一致 |
| `tools/verify_contract_refs.py` | 契约引用的类型/异常是否有定义（B7 欠账所在） |
| `tools/verify_data_center.py` | 数据中心契约 §3.6.1 与 DDL 约束清单双向一致（C1~C7）+ 对 `db/data_center.smoke.sql` 的触发测试覆盖核对（C7） |
| `tools/verify_iteration_plan.py` | 迭代计划里每个标「已交付」的迭代是否引用了一条**真实存在**的证据路径（防幽灵 ✅）+ DoD 四件套形状 |
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
| iteration-plan | `python tools/verify_iteration_plan.py` | 无参数（可选末尾跟一个文件路径，用于把守卫指向别的文件） |
| core-contract-refs | `python tools/verify_contract_refs.py --extra=docs/智能量化交易平台-数据中心接口契约文档.md --extra=docs/智能量化交易平台-风控层接口契约文档.md` | **少一个 `--extra=` 就会把已定义的类型误报成缺失**，虚增基线、掩盖真实漂移 |

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

**仓库**：已是 git 仓库（`main` 分支，2026-09-23 建立基线提交 `053f620`，35 个受控文件）。
但**只有基线这一个提交** ⇒ 只有「退回基线」这一条退路。
**I0 只完成了「可回滚」那一半**：「可运行」那一半（`pyproject.toml` + 包目录 + `pytest` + CI）仍未做。

5 个门禁：4 个 tier-A 全绿；`core-contract-refs` 是 tier-B 欠账，基线 **39**（T1=2 / T2=19 / T3=18）。
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
   虽有 git 但**只有基线一个提交**，一旦落盘乱码，只要还没提交就只能整体退回基线。
   改代码一律用编辑工具。
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
