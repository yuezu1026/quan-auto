# -*- coding: utf-8 -*-
"""Unified gate harness: one command, one compact verdict, one report file.

Why this exists
---------------
Each gate script grew its own CLI: verify_md_coverage.py needs a directory argument,
verify_contract_refs.py needs one --extra= per supplementary contract, the others take
nothing. That knowledge lived only in whoever ran them last -- and forgetting the
directory argument produced an IndexError with no output, which reads as "the gate
crashed" rather than "you called it wrong". Registering the exact argv here means the
knowledge lives in one place and cannot be lost between sessions.

Deliberately, every Chinese path appears as a Python literal rather than on the command
line: PowerShell 5.1 mangles non-ASCII argv (GBK), so `python tools/run_all_gates.py`
being pure ASCII is what makes this runnable at all.

Two tiers
---------
Tier A -- artifacts under our control, must be GREEN. Any non-zero exit fails the run.
Tier B -- known, recorded debt that cannot be fixed today (B7: the core contract names
          types and exceptions it never defines, and 2 of its python blocks do not
          parse). Measured 2026-09-25 against the real artifacts: total 2, both of them
          T1 (the two unparsable blocks). T2/T3 went 19 + 18 -> 0 on 2026-09-25 when
          附录 G supplied the missing definitions, and the baseline was lowered by hand
          in the same batch. The two T1 findings are STRUCTURAL and will stay:
          docs/智能量化交易平台-核心模块接口契约文档.md is .docx-derived and therefore
          append-only, so the two malformed blocks cannot be repaired in place (a
          re-conversion would wipe the repair). 附录 G5 carries liftable copies of both
          and the `contract-appendix` gate fails if those copies disappear. So this
          baseline tracks T1 only -- which still makes it a tripwire: any newly undefined
          type/exception pushes total above 2 and shows up as DRIFT+.
          A tier-B gate does NOT need a clean exit; its finding count must EQUAL the
          baseline in tools/gates-baseline.json. The ratchet is deliberately symmetric:
            actual == baseline  -> PASS   (debt unchanged)
            actual >  baseline  -> FAIL   (drift: new debt appeared)
            actual <  baseline  -> FAIL   (debt shrank: the baseline must be lowered by
                                          hand, otherwise the improvement is silently
                                          absorbed and the number stops meaning anything)
          The baseline detail is matched exactly, so a 0/0/0 entry is NOT usable: with
          no findings the parsed composition is {}, which can never equal {T1: 0, ...}.

Every gate is run twice: --selftest first (proves the detectors can still fire on a
deliberately bad input), then for real. A gate whose selftest fails is reported as FAIL
regardless of its real verdict -- a detector that cannot be seen failing is not a
detector.

What this harness does NOT do
-----------------------------
It does not execute any SQL. These gates prove the constraints are still WRITTEN, not
that they REJECT -- no amount of green in this report changes that.

Nor does it run pytest. tools/verify_skeleton.py proves the I0 skeleton is still wired
(pyproject / package / test files / the CI `run:` lines); proving that the tests actually
PASS needs an interpreter with pytest installed, which is a third-party dependency this
directory deliberately does not have. The real `python -m pytest -q` exit code lives in
tools/ci-dryrun-report.txt, produced by tools/ci_dryrun.py -- a SNAPSHOT, same caveat as
the SQL smoke report: valid only for the interpreter/commit recorded inside it.

Runtime evidence is a separate channel: tools/run_sql_smoke.py boots a throwaway
PostgreSQL via docker-compose.smoke.yml, applies both DDLs and runs both smoke tests on
a genuinely empty database, then writes tools/sql-smoke-report.txt. That file is a
SNAPSHOT: its verdict is only valid for the image digest recorded inside it, and it must
be re-run after every db/*.sql edit. Whether that green has TEETH is a third channel:
tools/falsify_smoke.py relaxes one named CHECK at a time and requires exactly one sample
to go red (currently 33 cases / 32 named CHECKs / 33 CAUGHT), writing
tools/falsify-report.txt by default and one report per image via `--report=` (since
2026-10-01 the ladder is four files: tools/falsify-report-pg1{4,5,6}.txt + that default
one, which is postgres:17). It is deliberately NOT registered as a gate here --
on a machine without a running daemon it would either fail for environmental reasons or
be silently skipped and reported green, which is the fake-gate pattern this project keeps
getting bitten by. Integrating it properly means "no docker => explicit SKIPPED, counted
as not-green", and that is a separate piece of work.
What IS registered is the pair of static gates that CONSUME those snapshots:
data-center-ddl (C6) and risk-config (C17) each read the `-- PG-VERIFIED-ON:` stamps out
of the DDL headers and compare them BOTH ways against the smoke ladder AND the
falsification ladder -- a stamp naming an image the falsification ladder never covered is
a FAIL, because "verified on that image" means "shown to bite there", not "loaded there".

Parallelism, and the guard that makes it safe
---------------------------------------------
A gate's two runs share nothing but the read-only tree, so the pairs run in a small
thread pool instead of one after another:
the console prints each gate as it finishes, but the report is rebuilt in REGISTRY
order, so the scheduling cannot change a single byte of it. `--jobs=N` sets the width
(default min(8, cpu_count); `--jobs=1` forces the old serial order, which is what you
want when reading interleaved output). Measured 2026-09-29 with the 17 gates: serial
wall 17.8s, parallel wall 11.3s. So it is a 1.6x win, not an 8x one, and the reason is
worth recording: the wall is now the SLOWEST SINGLE GATE'S PAIR. One gate dominates --
risk-table-wiring's selftest alone is 9.6s (14 negative controls, each re-parsing the
contract and the tree), while every other gate's selftest is <= 0.9s and the whole
`real` side is only ~4.1s. 9.6 + 0.8 is already ~10.4s, so the pool has removed
everything removable without touching a gate's own cost. Speeding that gate up would
mean caching its parses between samples, which is precisely how 14 negative controls
turn into 14 no-ops, so it is deliberately left alone.

Parallelism is only safe because no gate writes the repository, and that is now a
CHECK rather than a recollection: the run is bookended by a side-effect fingerprint
(relative path -> size + mtime_ns) taken before the gates start and again before the
report is written. Anything created, deleted or modified under the repo root fails the
run. Two directory CLASSES are skipped, deliberately and for stated reasons -- `.git`
(a gate that calls `git ls-files` may refresh the index stat cache, which is git's own
bookkeeping) and `__pycache__` / `*.pyc` (verify_dashboard re-imports quanauto, so
bytecode WILL appear; it is derived and gitignored, and nothing meaningful can hide in
it). Everything else is watched, UNTRACKED files included -- droppings a gate leaves
behind are exactly what this guard is for. If the fingerprint sees zero files it
reports FAIL rather than "clean": an empty check must never read as a passing one.

Usage:
  python tools/run_all_gates.py                 # run everything, print the table
  python tools/run_all_gates.py --full          # also dump each gate's raw output
  python tools/run_all_gates.py --gate=NAME     # run one gate
  python tools/run_all_gates.py --jobs=N        # pool width (default min(8, cpu))
  python tools/run_all_gates.py --selftest-simulate-write=PATH
                                                # SELFTEST ONLY, never use by hand:
                                                # writes PATH inside the repo so the
                                                # side-effect guard can be shown failing
  python tools/run_all_gates.py --list          # show the registry
  python tools/run_all_gates.py --selftest      # prove this harness can report FAIL

Exit codes: 0 = all gates green, 1 = at least one gate failed or the harness itself
could not run its checks, 2 = the harness' own self-test failed.
"""
import concurrent.futures
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_PATH = os.path.join(ROOT, 'tools', 'gates-baseline.json')
REPORT_PATH = os.path.join(ROOT, 'tools', 'gates-report.txt')

# 运行时验证属于**另一个 channel**，故意不注册成门禁：注册进来后，没有 docker 的
# 环境要么拿到一条环境性 FAIL，要么被静默 SKIP 而报绿 —— 后者正是本项目反复踩的
# 假门禁。这里只指向它的产物，不转述它的结论。
#
# 2026-09-30 修订：`platform-runtime`（`npm ci` / `npm test` / `npm run build` / `mvn test`）
# 是**有意的例外**。区别在于它需要的东西是**可装、可探、缺失时说得清**的（JDK≥21 /
# Node），而且它正是这一层最大的那个缺口；让它能注册的前提是给 harness 加**第三种
# 出口**（见 SKIP_RC 与 evaluate() 里的 SKIPPED 分支）：「环境不在」既不当 FAIL
# （那会把「没装 JDK」写成和「测试红了」同一种红），也不当绿（那更糟）——
# 它是一条单独的、ok=False 的 INCOMPLETE。而 docker/PostgreSQL 那条仍然靠人手动跑、
# 只指向产物：那种环境本机装不了，也不该在每次门禁里起一次容器。
SMOKE_TOOL = os.path.join(ROOT, 'tools', 'run_sql_smoke.py')
SMOKE_REPORT_PATH = os.path.join(ROOT, 'tools', 'sql-smoke-report.txt')
FALSIFY_TOOL = os.path.join(ROOT, 'tools', 'falsify_smoke.py')
FALSIFY_REPORT_PATH = os.path.join(ROOT, 'tools', 'falsify-report.txt')
# 证伪阶梯自 2026-10-01 起也是**逐镜像**记录的（与 sql-smoke 同形），所以尾部 NOTE
# 引用的是 glob 而不是那一份默认快照 —— 否则它会说「只在 postgres:17 上做过」，
# 而四个 tag 上早已各自 33/33 CAUGHT。
FALSIFY_REPORT_GLOB = os.path.join(ROOT, 'tools', 'falsify-report*.txt')

# 门禁自己的退出码约定（见 evaluate 的 SKIPPED 分支与它的样本）：
#   0 = 通过；1 = 判据红了；3 = **环境不在**（工具链探不到），打印 `verdict: SKIPPED (...)`。
# 3 是故意选的：2 留给「自测入口自己红了」，0/1 已被占。
SKIP_RC = 3

CORE_CONTRACT = 'docs/智能量化交易平台-核心模块接口契约文档.md'
DC_CONTRACT = 'docs/智能量化交易平台-数据中心接口契约文档.md'
RISK_CONTRACT = 'docs/智能量化交易平台-风控层接口契约文档.md'

# ---------------------------------------------------------------------------------
# The registry. `runner` is the real invocation; `selftest` is mandatory for every
# entry and must exit 0 having printed a SELFTEST OK marker.
# ---------------------------------------------------------------------------------
GATES = [
    {
        'name': 'md-fidelity',
        'tier': 'A',
        'what': '三份 .docx 的每一段（含表格单元）都出现在对应 .md 中',
        'runner': ['tools/verify_md_coverage.py', 'docs'],
        'selftest': ['tools/verify_md_coverage.py', '--selftest', 'docs'],
    },
    {
        'name': 'risk-config',
        'tier': 'A',
        'what': '风控契约 §3.6 与 db/risk_control.sql 与 smoke.sql 三方一致',
        'runner': ['tools/verify_risk_config.py'],
        'selftest': ['tools/verify_risk_config.py', '--selftest'],
    },
    {
        'name': 'data-center-ddl',
        'tier': 'A',
        'what': '数据中心契约 §3.6.1 与 db/data_center.sql 约束清单双向一致',
        'runner': ['tools/verify_data_center.py'],
        'selftest': ['tools/verify_data_center.py', '--selftest'],
    },
    {
        'name': 'data-center-pit',
        'tier': 'A',
        # 上一个门禁守的是建表语句，这个守的是**取数路径**：数据中心契约的 D3/D4/D6/D7
        # 都是「不许发生」的约束（不许绕过 as_of、不许按调用方给的日期取数、回测里不许
        # 用 QFQ/BFILL），而「不许发生」在源码里表现为**两层防线**——入口层的
        # `_require_visible` 和逐行层的 `record_access`。人眼审代码看不出哪天新加的
        # `get_xxx()` 忘了接上其中一层（漏接的后果是静默多读数据、回测结果偏乐观，
        # 不是报错），所以由它来喊。
        # 边界：它只解析源码、**从不执行**，也**不跑 pytest**（那会把第三方依赖拖进
        # tools/）。它证明的是「结构不可能被悄悄拆掉」，不是「守卫运行时真的会拒绝」；
        # 后者由 tests/test_data_center_pit.py 的 18 个用例负责。
        'what': 'DataFeed 取数路径的 as_of 两层防线（_require_visible + record_access）'
                '与回测会话的 QFQ/BFILL 拒绝都还在，且只由 DataCenter.as_of() 产出',
        'runner': ['tools/verify_data_center_pit.py'],
        'selftest': ['tools/verify_data_center_pit.py', '--selftest'],
    },
    {
        'name': 'data-center-adapter',
        'tier': 'A',
        # 上两个门禁守的是建表语句与取数路径，这个守的是**两者之间的那一层**：数据源
        # 适配器（quanauto/datasources.py）把外部 SDK 的列名/取值翻成标准 schema。它是
        # 全项目唯一一处「外部世界命名」与「我们自己的命名」正面接触的地方，而出错方式
        # 全是静默的：源字段名漏进下游（`data['close']` 与 `data['收盘']` 混用不会报错，
        # 只会在某天算出两个不同的数）、采集层直接 import 数据库驱动（研究层与平台层
        # 的边界当场消失）、或者上游 SDK 的枚举值直接写进我们有 CHECK 约束的列、到入库
        # 那一刻才炸。这三件事人眼审 200 行映射表都看不出，机器比集合不会。
        # 边界：它只做**静态**比对——把映射表里的值拿去和 DDL/契约的闭集对照、把 import
        # 根拿去和白名单对照——它**从不执行适配器、不联网、不 import pandas**（C4），也
        # 因此**证明不了「映射是对的数据」**：源列名→标准列的对应关系正确与否，只有真
        # 拉一次数据才知道（这正是 DC 契约附录 B 里记着的未验证项）。
        'what': '适配器把源列名/取值翻成标准 schema、采集层不含数据库驱动、'
                '上游 SDK 只在函数内惰性 import、且行对象不暴露源字段名',
        'runner': ['tools/verify_data_center_adapter.py'],
        'selftest': ['tools/verify_data_center_adapter.py', '--selftest'],
    },
    {
        'name': 'iteration-plan',
        'tier': 'A',
        # The plan is a product too. Without this gate, '已交付' in docs/迭代计划.md is
        # an unchecked self-assessment: the C4 check (every cited evidence path must
        # exist) is the one thing that turns an iteration status into a refutable
        # claim. Tier A: it guards an artifact we author, so it must be green.
        'what': '迭代计划里每个标「已交付」的迭代都引用了一条真实存在的证据路径',
        'runner': ['tools/verify_iteration_plan.py'],
        'selftest': ['tools/verify_iteration_plan.py', '--selftest'],
    },
    {
        'name': 'skeleton',
        'tier': 'A',
        # I0 的产物（pyproject / 包目录 / 测试文件 / CI 接线）都是「存在且能跑，
        # 但删掉不会有人喊」的东西：把 ci.yml 里那行 run_all_gates 注释掉，本报告
        # 在本地照样全绿，而门禁从此再也不会被执行。这个门禁守的就是那条接线。
        # 它**不跑 pytest**：那会把第三方依赖拖进 tools/，并且在没有 .venv 的机器
        # 上报环境性 FAIL。pytest 的真实退出码由 tools/ci-dryrun-report.txt 记录。
        'what': 'I0 骨架还在：pyproject / 包 / 测试 / CI 接线（不跑 pytest）',
        'runner': ['tools/verify_skeleton.py'],
        'selftest': ['tools/verify_skeleton.py', '--selftest'],
    },
    {
        'name': 'backtest-reproducibility',
        'tier': 'A',
        # 本门禁守的是「同种子 ⇒ 同结果」这句话。手跑两次看一眼只是「碰巧成立」的
        # 观察；把它变成判据之后，滑点里多抽一次随机数、字典遍历顺序变了、或者有人
        # 往 deterministic 段塞进一个时戳，都会立刻变红。tier A：它守的是我们自己写的
        # 产物。注意它只证明**本机**可复现，不证明跨机器/跨版本可复现。
        'what': '同种子两次回测的报告 deterministic 段逐字节相同，换种子必须真的变',
        'runner': ['tools/verify_backtest_reproducibility.py'],
        'selftest': ['tools/verify_backtest_reproducibility.py', '--selftest'],
    },
    {
        'name': 'contract-signature',
        'tier': 'A',
        # 本门禁守的是「契约怎么写、代码就怎么长」。I1 里三处契约与实现的偏差全是靠
        # 人眼发现「回测最大回撤 49.9% 不可能」才顺出来 —— 人工读契约会漏，机器比参数
        # 列表不会。清单里的契约侧签名必须能在契约文档里逐字找到，所以它管的是
        # 「有人单边改了签名」，不是「三方都对」。
        # 边界：**不比返回类型**（契约引用了大量本项目不存在的类型），只比参数列表、
        # 数据类字段名、以及契约类里未登记的公有成员；impl_only 类只反向查成员消失。
        'what': '契约侧与实现侧的签名/字段一致，多出来的公有成员必须在清单里登记',
        'runner': ['tools/verify_contract_signature.py'],
        'selftest': ['tools/verify_contract_signature.py', '--selftest'],
    },
    {
        'name': 'dashboard-consistency',
        'tier': 'A',
        # I4 的看板是**纯读数**组件（DoD：「不得另立一套指标定义」）。它最容易出的错
        # 不是崩溃，而是「自己再算一遍」：看板里多一行 `sum(...) / len(...)` 会照常
        # 渲染、照常好看，只是从此它和 PerformanceAnalyzer 各有一套指标，两边哪天不
        # 一致也没人知道。本门禁的 C6 直接禁掉落进看板模块的统计函数与统计模块，
        # C8 再把「改一个数，读数必须跟着动」双向扫一遍。
        # ★ C10 是这个门禁的**参照物**：它现场重跑真的 PerformanceAnalyzer（用报告里
        # 自带的 trades/orders/account_history），与报告里存的 14 个指标逐字段比。
        # 没有它，整个门禁就是恒等式 —— C3 比的是「看板读出的数」和「同一份 payload
        # 里的字段」，两个数同源，手改报告会把两边一起改掉，于是永远相等。第一版正是
        # 这么写的，14 个假样本一个都没证明。**判据的两端必须来自不同来源。**
        # 边界：C10 的参照物就是写出这些数的同一个分析器，所以「公式本身算错了」它看不
        # 见（两边一起错、自洽通过）；它证明的是「报告里的数 = 这份报告自己的成交重新
        # 算出来的数」，即报告是新鲜的、没被手改过。实盘实时看板不在本轮。
        'what': '看板读数只来自 BacktestResult（禁自建统计），且报告里存的 14 个指标与'
                '现场重跑 PerformanceAnalyzer 的结果逐字段一致',
        'runner': ['tools/verify_dashboard.py'],
        'selftest': ['tools/verify_dashboard.py', '--selftest'],
    },
    {
        'name': 'appendix-refs',
        'tier': 'A',
        # 前两个契约侧门禁守的是签名与 ```python 块里的类型，**散文里的交叉引用没人管**。
        # 「见数据中心契约附录标签 B12」这类句子是写给读契约的 agent 的地址；附录被重编号
        # 或整段删掉之后，这句话仍印在文档里、其余门禁全绿，agent 去一个空地址取裁决 ——
        # 要么自己编一份，要么把约束丢掉，两种都不报错。范围 = `git ls-files`（克隆里能
        # 看到的文件），gitignored 的一次性探针不在其中；拿不到文件清单时它拒判，否则等于
        # 审计一个未知子集。它自己的输出里不写「附录+标签」的形状：gates-report.txt 是
        # tracked 文件，报告会被下一轮重新扫，一句悬挂引用会变成幻影 FINDING。
        # 边界：只证明「这个标签存在」，**证明不了标签背后的裁决仍然成立**。
        'what': '契约文档里被引用的附录标签（含条目号、点名归属）都真实存在',
        'runner': ['tools/verify_appendix_refs.py'],
        'selftest': ['tools/verify_appendix_refs.py', '--selftest'],
    },
    {
        'name': 'contract-appendix',
        'tier': 'A',
        # B7 的还债方式决定了必须有这个门禁：主契约由 .docx 派生 ⇒ 只许追加，所以「补上
        # 未定义的类型/异常」只能写成新的附录 G，而不能改原文。附录 G 一旦是**抄件**，
        # 它就会和 quanauto/ 各自漂移：抄错一个默认值（`count: int = 0` 抄成 `= 1`）、
        # 或实现改了字段而附录没跟着改，两种都不报错，而读者会当真。这里逐条对拍的是
        # 基类 / 字段名与顺序 / 字段类型 / 字段默认值 / 枚举取值 / 异常的 code 四类形状，
        # 并额外盯住三件事：登记了却找不到实体（AX-UNUSED-REGISTRY）、契约先行的名字
        # （本迭代不实现的 5 个）一旦有实现就必须回来改判据（AX-PENDING-DRIFT）、以及
        # G5 留下来的两个 T1 可复制副本不许被删掉（AX-T1-COPY —— 删了 T1 计数不会变，
        # 但「能不能照抄」这件事会静默地退回去）。
        # 边界：只证明「本节 = 实现」，不证明实现对；不比方法、不比 docstring、不比注释，
        # 也不对契约先行那 5 个名字的字段表（推定）做任何背书。
        'what': '附录 G 登记的类型/异常与 quanauto/ 的基类/字段/默认值/枚举取值/错误码逐条一致',
        'runner': ['tools/verify_contract_appendix.py'],
        'selftest': ['tools/verify_contract_appendix.py', '--selftest'],
    },
    {
        'name': 'enum-members',
        'tier': 'A',
        # 上面几个门禁都碰不到枚举的**成员表**：contract-appendix 只管附录 G 那批类，
        # contract-signature 的 classes/impl_only 里一个枚举都没有（manifest 实测）。
        # 于是出现过一个货真价实的假绿：往 `quanauto/enums.py` 的 MarketStatus 里插一个
        # 成员，run_all_gates 仍 13/13 全绿，而它的成员表当时与数据中心契约 §3.1.3
        # 三个成员互缺 —— 枚举是「范围」类型，成员多一个少一个都不会有任何运行时症状。
        # 本门禁逐条对拍：契约三份文档的 ```python 块里的枚举 vs `quanauto/*.py`，
        # 比成员名、成员取值、成员顺序，两侧都要求能被解析（块解析不了就拒判）。
        # 免检登记只有两张表：PENDING（契约先行，还没实现的）与 IMPL_LOCAL（实现侧先行，
        # 契约里没有的）。两张表**反向也要成立**：登记了但实际已经比过/已经消失 ⇒ FINDING，
        # 否则它们会变成永久免检的洞。
        # 边界：只比成员表，不比 docstring/行为；「HOLIDAY 该不该并进 CLOSED」这类裁决
        # 不归它管 —— 它只保证「两侧说的不一样时一定有人知道」。
        'what': '契约声明与实现侧枚举的成员名/取值/顺序逐条一致（PENDING/IMPL_LOCAL 反向也查）',
        'runner': ['tools/verify_enum_members.py'],
        'selftest': ['tools/verify_enum_members.py', '--selftest'],
    },
    {
        'name': 'class-coverage',
        'tier': 'A',
        # 上面两条都只盯着**已经被点名**的东西：contract-appendix 只管附录 G 那批，
        # enum-members 只管枚举成员。而「`quanauto/` 里这个类到底归谁管」这件事，
        # 全仓库原先**一条判据都没有** —— 存在一类安静的缺口：契约里没有它的类块、
        # 登记表（manifest.classes / impl_only / CONTRACT_FIRST / T1_REQUIRED）里也
        # 没有它，于是它既不欠账、也不存在，谁都不会知道。实测 2026-09-25：
        # `quanauto/*.py` 共 144 个类，登记侧并集只有 38 个名字。
        # 本门禁把「每个类都必须落进四集合之一」写成判据：① DECLARED（三份契约的
        # ```python 块里 `class X` 真的解析出来了 —— 文本搜不算）；② REGISTERED
        # （四张登记表并集）；③ DEBT（契约**散文里点了名**却没有类块 ⇒ 明账）；
        # ④ LOCAL（契约根本不定义 ⇒ 实现细节）。两个豁免表都查反向：登记了但已经
        # 被声明/已经消失 ⇒ FINDING，否则它们会变成永久免检的洞。
        # ★ B10 的第二条要求就是这里的 `mention split` 一行：**提及不算覆盖**。
        #   29 个待裁名字里 13 个在散文里出现过、16 个连名字都没有 —— 若把提及降级
        #   成「已覆盖」，13 个会当场合规，而契约一个字都没改。计数一律**数出来**。
        # ★ `DAMAGED` 表是给 .docx 损坏的块用的（三份契约里有两处，都在 core，
        #   原文在 T1 表里有可复制副本）。它是**豁免**而不是免检：块解析不了时先看
        #   它提到的类名有没有登记，没登记照样报 CC-UNPARSABLE-CLASS；登记过但块
        #   现在能解析了、或那个类名已经不在了，报 CC-DAMAGED-STALE。
        #   损坏块里的类名用**非锚定**匹配找：实测 core 的 Account 块被转成了
        #   `@dataclass class Account: account_id: str # 账户ID …`（类名挤在行中），
        #   锚定行首会静默漏掉它 —— 而「漏掉」正是本门禁要防的事。
        # 边界：只证明「有没有人管」，不证明实现对；不比方法/docstring/字段；
        # DEBT/LOCAL 两张表的裁决是**人写的**，本门禁只保证「表与代码同时漂了会红」。
        # ⚠️ 29 这个数**不是基线**（B9.2 明说）：它是一次实测快照，门禁要求的是
        #    uncovered 集合**为空**，而不是「不超过 29」。
        'what': 'quanauto/ 每个类都落进「契约声明 / 登记表 / 明账 DEBT / 实现细节 LOCAL」之一（提及不算）',
        'runner': ['tools/verify_class_coverage.py'],
        'selftest': ['tools/verify_class_coverage.py', '--selftest'],
    },
    {
        'name': 'error-codes',
        'tier': 'A',
        # 三份契约各有一张错误码表，`quanauto/errors.py` 是唯一的实现侧真相，而这两侧
        # **此前没有一条机器判据** —— 这个洞被登记过三次（数据中心契约的附录 A8 与附录 C6
        # 都写着「无机器判据，已知缺口」；风控契约 §3.7 那句「两处定义必须保持一致，不得
        # 冲突。」本身就是一条没有判据的要求）。登记了三次却一直没关，是因为它看着像
        # 「人读一遍就行」：11 行表、30 个码。实跑一次就打脸 —— 主契约的附录 D 把 `RISK_001`
        # 写成 CRITICAL，风控契约 §3.7 写 ERROR，而那一行还标着「（主契约原有）」：
        # 转抄时抄错了，两边「各自都绿」了很久。同一轮还抓出一处拼写分歧（看板码少一个
        # 下划线），连它自己给出的那条理由都是自证不成立的。
        # 判据 = 码 / 拼写 / 级别 / 类名四样对拍；两张登记表（契约定义而刻意不实现 PENDING、
        # 实现自有 IMPL_ONLY）**反向也查**：登记的东西一旦落地或消失就报 EC-STALE-PENDING /
        # EC-IMPL-ONLY-STALE，否则它们会长成永久免检的挡箭牌。
        # 边界：**不查描述与建议措施**（那是人写的话，不是标识），也不比继承关系（有一处
        # 已登记的刻意偏差会让 issubclass 判据误报）；方向**有意单向** —— 契约可以定义
        # 实现里还没有的码（errors.py 的原则是「只定义真的会抛的异常」，预支的死代码会让
        # 「已实现」看起来比实际多），反向才报 EC-CODE-INVENTED。扫描面只有三份契约 +
        # `quanauto/*.py`，**不含** `迭代计划.md` / `缺口清单.md`：它们是记录，不是契约。
        'what': '三份契约的错误码表与 quanauto/errors.py 在码/拼写/级别/类名上双向对齐'
                '（PENDING/IMPL_ONLY 两张登记反向也查）',
        'runner': ['tools/verify_error_codes.py'],
        'selftest': ['tools/verify_error_codes.py', '--selftest'],
    },
    {
        'name': 'risk-table-wiring',
        'tier': 'A',
        # 上面那些门禁守的都是「已经有人在写的东西」：DDL 的约束、取数路径、契约
        # 的签名、错误码表。而没有写入方的表是一个**观测空白** —— 它在别的任何判据
        # 里都不报错：DDL/契约一致性门禁照样绿（表和约束都在）、SQL 冒烟照样绿
        # （约束确实会拒绝）、测试照样绿（没有测试碰它），于是关于这张表的每一份
        # 报告都是**空集上通过**。实测 2026-09-25：`risk_decision_log` 就是这样，
        # 它随 DDL 交付、被约束门禁覆盖，而 `quanauto/*.py` 里**零写入者**。
        # 本门禁守四件事：① 契约 §3.6.1 列出的每张表都有产品写入方（除非在契约里
        # 登记了 `WIRING-EXEMPT:` 豁免，且代码侧真的没有 —— 双向查，免得豁免长成
        # 永久免检的挡箭牌）；② 「被提及」不算写入方（用 `ast` 解析并**丢弃
        # docstring**，所以注释/docstring 里的表名永远不能满足它 —— `quanauto/risk.py`
        # 的 `risk_switch_state` 正好是一句 docstring）；③ 写入方归属契约点名的组件；
        # ④ 那条让这一行值得写的**能力**还在（`risk_config_version` 必须递增、
        # `risk_equity_peak` 必须 `GREATEST` —— 两处都是一 token 的改动，其余判据全绿）。
        # 另有一条 D1 结构检查：`RiskEngine.check()` 里不许出现 SQL 字面量 / IO 调用 /
        # 写入方引用（本仓库的接法是「谁拿到 response 谁写」，很容易被"顺手"写回原地）。
        # 边界：**它不跑任何 SQL** ——「语句存在」不等于「行落库」（那是 `run_sql_smoke.py`
        # 与 store 测试的活）；也不判断写入方**对不对**，只判断存在、归属、关键能力还在。
        'what': '风控契约 §3.6.1 每张表都有产品写入方（或按 §八 登记豁免）、写入方归属契约点名'
                '的组件、关键能力未被改掉，且 D1 的 check() 内零 IO 仍成立',
        'runner': ['tools/verify_risk_table_wiring.py'],
        'selftest': ['tools/verify_risk_table_wiring.py', '--selftest'],
    },
    {
        'name': 'platform-spec-parity',
        'tier': 'A',
        # 平台层读取侧切片（`platform/`，2026-09-29 追加）把 `quanauto/dashboard.py` 的
        # 14 条指标规格**逐字抄**进了 `MetricSpec.java`，而抄完之后没有任何东西盯着这
        # 两份 —— 那个文件自己的 Javadoc 就写着「这是一份手抄的副本，而目前没有任何门禁
        # 盯着它」，附录C §C.5 第 2 条也把它登记成缺口。代价跟 §C.5 第 4 条记的同一个味道：
        # 两侧各自都绿（Python 侧由 dashboard-consistency 盯、Java 侧编译得过），**缝上
        # 静默不一致** —— `dashboard.py` 改了小数位/标签/量纲/分组，屏幕与 I4 命令行看板
        # 就悄悄分叉，而没有任何一条判据会红。
        # 本门禁比三样东西（顺序敏感，顺序即界面顺序）：① 5 条量纲 + 4 条分组常量的**取值**；
        # ② 14 行的 key/group/label/unit/digits 与**行序**；③ STRUCTURE_COUNTS 的名字与顺序。
        # 边界：它只比这份**声明式规格表**，不做端到端 —— 「屏幕真印对了」归
        # `platform/check_text_parity.py`，而那个脚本需要一个活着的 JVM（「环境没起来就判红」
        # 的门禁本身就是判据缺陷）⇒ 它**不是门禁**，本门禁也不假装替代它。
        # 它只解析文本与 AST（不 import `quanauto.dashboard`、不编译 Java）；
        # 每一处提取都带空转守卫（提取为空/变薄 ⇒ 拒绝通过，不打印「0 issue(s) PASS」）。
        # 修的永远是 Java 侧或被比对的判据，**`quanauto/dashboard.py` 是冻结的参照**。
        'what': '平台层 MetricSpec.java 与 quanauto/dashboard.py 的规格表双向一致'
                '（量纲/分组常量取值、14 行的键-分组-标签-量纲-小数位与行序、结构计数）',
        'runner': ['tools/verify_platform_specs.py'],
        'selftest': ['tools/verify_platform_specs.py', '--selftest'],
    },
    {
        'name': 'platform-runtime',
        'tier': 'A',
        # 上一条比的是两侧的**声明**；这一条是平台层运行侧的第一条：真的把
        # `npm ci` / `npm test` / `npm run build` / `mvn test` 跑一遍，并在同一棵树里核对
        # 「前端产物真落进了 Java 侧被服务的那个目录」。附录C §C.5 第 4 条把它们
        # 记成缺口，理由一直是「需要 JDK/Node，而判据依赖环境就是判据的缺陷」。
        # 解法不是不跑，而是把「环境不在」变成一个**响亮的、不算绿**的结论：
        # 探不到 mvn / node / java≥21 时它退 SKIP_RC(3) 并打印 `verdict: SKIPPED(缺什么)`，
        # harness 把它记成 SKIPPED 且 ok=False ⇒ 报告照红，只是红得说清了原因
        # （与「测试红了」分开：那种要去看门禁本体，这种要装工具链）。
        # 它跑在 %TEMP% 的沙箱里（`platform/` 去掉 node_modules|target|dist|static，
        # 加 `.rounds/i1` 与一个**空的 `.git`**）—— 一个字节都不写进仓库，因此不必
        # 去放宽 FINGERPRINT_SKIP_DIRS（放宽判据是禁的）；`.git` 是必需的，
        # `ReportCatalog.resolve()` 靠往上找 `.git` 定位报告目录，没有它测试会假红。
        # 步骤**顺序**是判据的一部分：`npm test` 排在 `npm run build` 之前（渲染用例不需要构建产物）、
        # 而 `npm run build` 必须早于 `mvn test` —— 产物写进 `api/src/main/resources/static/`，
        # 而 Spring 从 `target/classes/static/` 托管（`platform/README.md` 里记的那个坑）。
        # 边界（别把这些读进来）：它**不打开浏览器**，也不判断页面好不好看 ——
        # 页面好不好看、有没有元素重叠要一个**真的渲染引擎**（jsdom 没有布局引擎，
        # 那里的 rect 恒为 0×0 ⇒ 断言恒真是假绿）⇒ **布局 / 视觉**仍然零覆盖；
        # 「活着的服务端下发了什么」也仍然只有手动脚本（它要一个活着的 JVM+端口）。
        # ⚠️ 订正（2026-10-01 晚）：这一段原来写的是「**页面本身仍然零覆盖**」，**已过期** ——
        # 第二步 `npm test` 在 vitest + jsdom 里把 `App.jsx` **真的挂起来**
        # （`platform/web/test/render.test.jsx`，输入是一份由真报告投影出来的夹具），
        # 「页面上印了什么」第一次有了机器判据；仍然零覆盖的只剩**布局 / 视觉**那一格
        # （订正记录见 `platform/README.md` 抬头与附录C §C.13）。
        # 它证明的是「这一层今天真的构建了、真的测试了，且产物真的走到被服务的那个目录」。
        # ⚠️ 订正（2026-10-01）：这段原来还写着「展示串两侧是否一致也一样（零覆盖）」，
        # **已过期** —— 那条缝现在由 `platform-text-parity` 门禁盯着（Python 侧核夹具+
        # Java 侧 `FormatParityTest` 由本条的真 `mvn test` 跑）；`check_text_parity.py`
        # 仍不是门禁，但它已经不是唯一手段了。
        'what': 'npm ci/test/build 与 mvn test 真跑一遍（第二步在 jsdom 里真的把页面挂起来），'
                '且前端产物真的落到 target/classes/static（工具链不在时退 3 = SKIPPED，不算绿）',
        'runner': ['tools/verify_platform_build.py'],
        'selftest': ['tools/verify_platform_build.py', '--selftest'],
    },
    {
        'name': 'platform-text-parity',
        'tier': 'A',
        # 上一条把这一层**真的**编译并测了一遍，但测的用例是手挑的 7 条。2026-10-01 实测：
        # 拿 73929 组输入做差分，Java 的 `ReportProjection.fixed` 与 Python 的
        # `dashboard.format_metric` 在 **6887 组（9.3%）**上打印出不同的字符串
        # （丢负号 4161 / 十进制平局落错边 2726），而当时那 7 条断言**全部通过**
        # —— 手挑的样本证明不了「两侧一致」，只有按量纲 × 小数位 × 值铺开的语料加自动比对能。
        # 本门禁是那条缝的常驻判据，两条腿：
        #   ① Python 侧（就是本文件）：核 `platform/text-parity-cases.json` 新不新鲜
        #      （逐字段与现算结果比 ⇒ `PARITY-STALE-CASES`）、语料够不够厚（条数 /
        #      量纲×小数位组合 / 值域边界），以及判定理由与登记项还在不在；
        #   ② Java 侧：`FormatParityTest` 把同一份夹具逐条喂进 `formatMetric` 与 Python 比，
        #      由 `platform-runtime` 那条真 `mvn test` 跑 ⇒ 这边**不启动 JVM**（判据依赖
        #      环境就是判据的缺陷，这条边界是上一轮定下来的，不许回退）。
        # 为什么夹具不是「Java 侧现场生成」：**Python 侧是冻结的参照**，夹具由它现算得出，
        # 于是「一侧改了、另一侧不知道」必然表现为夹具变红，而不是两侧一起改绿。
        # 修法固定：改 Java 侧或改本门禁的判据，**不许**改 `quanauto/dashboard.py`。
        # 边界：不打开页面（它自己不渲染 —— **渲染**那一格归 `platform-runtime` 的第二步，
        # **布局 / 视觉**仍然零覆盖）；不启动 JVM；也不判断
        # 显示规则本身好不好 —— 它只判断两侧**逐字节一致**。
        'what': '平台层显示规则与 Python 侧逐字节一致（语料覆盖平局 / 丢负号 / 大整数三类边界）',
        'runner': ['tools/verify_platform_text_parity.py'],
        'selftest': ['tools/verify_platform_text_parity.py', '--selftest'],
    },
    {
        'name': 'platform-web-parity',
        'tier': 'A',
        # 上面三条兄弟门禁（`platform-spec-parity` 管声明、`platform-runtime` 管运行、
        # `platform-text-parity` 管显示规则）当时在各自 `what` 里**都**写着同一句边界：
        # 「页面本身仍然零覆盖」—— 理由是「要看页面得有一个活着的 JVM 与端口」。
        # ⚠️ 订正（2026-10-01 晚）：那句口径此后**只对布局 / 视觉成立**了 ——
        # `platform-runtime` 的第二步 `npm test` 已经把页面真的挂起来（渲染有判据），
        # 所以本门禁继承的那条边界也只剩**布局 / 视觉**一格（见附录C §C.13）。
        # 而那条理由（「要看页面得有一个活着的 JVM 与端口」）对下面这几件事**不成立**，
        # 而它们正是「静态判得动、但此前一条判据都没有」的部分：
        #   ① 页面读的每一个字段（`view.dataVersion` 这种点链）是不是 Java 记录里真有的
        #      组件。前端写错一个字段名不会报任何错 —— JSON 反序列化不校验字段名，
        #      这一层也没上 TypeScript ⇒ 屏幕上是一个**静悄悄的空**，四条门禁没有一条会红。
        #   ② 页脚那条自报清单（「本层有自建门禁了（…N 条…都在 CI 里跑）」）是不是真的
        #      —— 加第四条门禁而页脚没跟上、或页脚点了一个不存在的名字，此前无人核对。
        #      **自报清单本身就是一句会过期的计数句**，而它此前正落在 `skeleton` 的
        #      `GATE-COUNT` 扫描面之外（那条判据只看 `CONTEXT.md` / `pyproject.toml` /
        #      `quanauto/__init__.py` / `.github/**/*.md`）。
        #   ③ 「绩效数字只在服务端格式化」这条自称是不是还成立：前端一旦出现数字格式化
        #      调用（`toFixed` / `Intl.` / `Number(` …），两侧就各算一套 —— 那正是
        #      `platform-text-parity` 花力气堵的那条缝的**源头**，而它只堵了服务端那一份。
        #   ④ 指标键与分组名有没有在前端手抄一份（`Metrics` 组件里那句注释自称没有抄，
        #      而这条自称此前**没有牙**）。参照物 = `quanauto/dashboard.py` 的规格表，
        #      经 `tools/verify_platform_specs.py` 的 AST 扫描取得（**不 import** 它，
        #      免得凭空多出第三份副本）。
        # 判据是**形状**而不是正确性（与 `line-anchors` 同一哲学）：字段名对不对是形状，
        # 布局好不好看不是。每条登记别名必须带 `anchor`（在 `platform/web/src` 里**恰好
        # 出现一次**的代码字面量）—— 改名 ⇒ 登记表变空壳 ⇒ 报 PW-STALE-ROOT，这是
        # 「只查单向」的补丁（兄弟门禁都用同一个手法）。
        # 三条空转守卫：扫不到 JS / 解析不出任何 Java 记录 / 登记表为空 ⇒ 一律拒判，
        # 绝不许打印「0 issue(s) PASS」。
        # 边界（**这条门禁不解除那条零覆盖项**）：不打开页面 —— 不起服务、不渲染、
        # 不看布局与视觉，**布局 / 视觉仍然零覆盖**（**渲染**那一格归 `platform-runtime`
        # 的第二步 `npm test`，见附录C §C.13）；也不启动 JVM / 不编译 Java
        # （编译与测试归 `platform-runtime`）；看不见未登记的别名（解构、计算属性名、
        # 穿出函数参数的字段都在扫描面外）⇒ 别说「前端字段全被钉住了」。
        # 修法方向：改了页面或 Java 记录 ⇒ 改 JSX / 记录本身；登记表过期 ⇒ 改登记表；
        # **不许**为了让这条变绿去削 `quanauto/dashboard.py`（它是冻结的参照）。
        'what': '平台层页面与 Java 声明一致（字段名对得上记录组件 / 前端不做数字格式化 / '
                '不手抄规格表 / 页脚自报的门禁清单等于注册表 / 渲染夹具的规格列与键集合双向核对）',
        'runner': ['tools/verify_platform_web.py'],
        'selftest': ['tools/verify_platform_web.py', '--selftest'],
    },
    {
        'name': 'line-anchors',
        'tier': 'A',
        # 上面那些文档侧门禁守的是「引用有没有指向」：附录标签在不在（appendix-refs）、
        # 签名/字段/错误码两侧一不一致。而**行号**这条引用方式此前**一条判据都没有**，
        # 而且规则层面早就改成「按内容定位」了（`docs/历史订正.md` 写着不许再拿行号当
        # 锚点；数据中心契约里甚至有整整一块「引用方式订正」，追述同一个地址在两次
        # 前文插行之后（一次插 47 行、一次插 9 行）一路漂到今天的位置）。
        # **教训登记了 ≠ 判据存在** —— 零门禁的直接后果是
        # 2026-10-01 一跑就抓出 4 处：`quanauto/datacenter.py` 两处（其中一处指的是一张
        # 表的某一行、另一处指附录 B21）、`docs/开工前缺口清单.md` 一处（指向的那个
        # 行号今天是一张空行!），以及 `tools/dev.py` 里把 3210 行的数据中心契约写成
        # 「2514 行」兼指两行注释。
        # 判据是**形状**而不是正确性（正确性要语义判据，机械做不到）：扫描面 = `docs/**/*.md`
        # + `CONTEXT.md` + `platform/**` + `.github/**/*.md` + `quanauto|tools|tests/*.py`，
        # 每一条行号引用（`L###` / 「第 N 行」）必须在**同一行**上自带日期 / 提交号 / 版本号
        # （= 自描述的快照读数，它自己声明了「这是某时某物的读数」），否则要登记进
        # `tools/line-anchors.json`；登记项里 `kind=numbered`（指向某个文件的某一行）与
        # `kind=row`（指向某张表的某一行 —— 用**表头文字**定位再数行）会被**按内容真核对**，
        # 漂了报 LA-DRIFT，而 `quoted`（引别人的行号 / 一段漂移史）与 `structural`
        # （报告格式、数据行序号这类**不是地址**的编号）只记账但**必须写 why**。
        # ★ 设计反转值得记：`tests/test_risk_engine.py` 里那 12 处「§3.8 第 N 行」本来
        #   只能记成没牙的 structural，为此**加了 `row` 这一种 kind**；结果第一次跑就发现
        #   风控契约 §3.8 那张表**少了一行**（「名单型规则」，黑名单只拦开仓不拦平仓），
        #   那批引用的序号相对该表整体偏移 2 行 —— 已把缺的那一行补回（这是新发现，
        #   此前没有任何文件记过它）。**给判据加牙的动作本身会抓出东西**，这就是例子。
        # 每条登记项都带 `count`（实测出现次数）：产物改了而登记表没跟上 ⇒ LA-COUNT；
        # 登记项还在而引用已消失（改了锚点忘了删登记）⇒ LA-STALE —— 后者是「只查单向」
        # 的补丁：不然改掉一条坏锚点反而会让登记表变成永久的空壳。
        # 两条空转守卫：一个行号都没提取到 ⇒ 拒判；登记表为空 ⇒ 拒判；一条登记都没有
        # 且没有 numbered/row 项 ⇒ 拒判（否则这门禁「只剩记账，漂了也不会红」）。
        # 边界：它**只管地址像不像话** —— 契约正文说错了话它不会红（那是 error-codes /
        # iteration-plan 那几条的事）；`expect` 只在**目标文件**里唯一才能当判据
        # （出现多次报 LA-WEAK：说不清你指的是哪一处）；`docs/历史订正.md` 是冻结文件，
        # 里面的数只登记不回改。坏锚点优先改成**按内容定位**（引那一格的文字），
        # 其次才是登记成 numbered —— 修法顺序写在它的两条 NOTE 里。
        'what': '文档与源码里的行号引用都被绑住（同一行有日期/提交号/版本号/「会漂」措辞，'
                '或在 tools/line-anchors.json 登记，其中 kind=numbered/row 会被按内容核对）',
        'runner': ['tools/verify_line_anchors.py'],
        'selftest': ['tools/verify_line_anchors.py', '--selftest'],
    },
    {
        'name': 'core-contract-refs',
        'tier': 'B',
        # The two supplements contribute definitions only. DataFeed/MarketStatus/
        # DataFeedError/PITReport/LeakagePoint/ValidationReport live in the data-center
        # contract; omitting it reports them as holes in the core contract, which is a
        # false positive that inflates the baseline and hides real drift.
        # NOTE: "has a definition" is all this gate can see -- it does not compare the
        # definition against quanauto/. MarketStatus used to be the live example (this
        # gate was green while the implementation's member list disagreed with the
        # data-center contract). That member list is now reconciled and the case is
        # guarded by the enum-members gate above, so what remains invisible here is any
        # *future* member-level divergence -- not just in enums: this gate never reads
        # a body, a default, or a member.
        'runner': ['tools/verify_contract_refs.py',
                   '--extra=' + DC_CONTRACT,
                   '--extra=' + RISK_CONTRACT],
        'selftest': ['tools/verify_contract_refs.py', '--selftest'],
        'what': '主契约引用的类型/异常是否都有定义（B7 欠账，受棘轮约束）',
    },
]

# 检查码里的连字符要认。原写法 `[A-Z0-9_]+` 匹配不上 `MAP-PATHS` / `CI-SWALLOW`
# 这类**本仓库自己在用**的代码，于是 parse()['total'] 偏小：tier-A 只是把计数印错
# （另有 verdict 行的数字兜底），tier-B 的棘轮却直接吃这个数 —— 带连字符的欠账会被
# 算成 0，`debt shrank` 与 `composition moved` 两档都可能因此放行。2026-09-25 修，
# 样本见 selftest() 的 [hyphen-code-counted]。
FINDING_RE = re.compile(r'^(?:FINDING|ISSUE) \[([A-Za-z0-9_-]+)\]', re.M)
VERDICT_RE = re.compile(r'^verdict:\s*(\w+)', re.M)
SELFTEST_RE = re.compile(r'^SELFTEST\s+(OK|FAIL)', re.M)

# The count next to a verdict is phrased differently by each gate:
#   'verdict: PASS (0 issue(s))'      -> risk / data-center
#   'verdict: DIRTY (45 finding(s))'  -> contract refs
#   'verdict: PASS (total missing=0)' -> md fidelity
# An earlier version hard-coded the first form, so md-fidelity FAILED with
# 'no verdict line' -- a false alarm from the harness, not a defect in the artifact.
# Both shapes are matched; if neither matches the count falls back to counting the
# FINDING/ISSUE lines, which is the authoritative number anyway.
COUNT_PATTERNS = [
    re.compile(r'\((\d+)\s+(?:finding|missing|issue)'),
    re.compile(r'\b(?:findings?|missing|issues?)\s*=\s*(\d+)'),
]

EXCERPT_RE = re.compile(r'^(?:FINDING|ISSUE|GATE|verdict|SELFTEST|TOTAL|blocks:|'
                        r'error names:|extracted:|CRLF-normalised:|GATE FAIL|  \[)')


def harden_stdout():
    """A gate harness must never die on a decorative character.

    This console is cp936. '↔' (U+2194) is not in GBK, so printing it raised
    UnicodeEncodeError *while the results table was being written*, losing the entire
    verdict -- the run looked like a crash and no gate output was shown at all.
    Unencodable characters now degrade to '?' instead of aborting.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def run_argv(argv, timeout=900):
    """Runs one gate. stderr is merged into stdout because several gates report their
    own setup failures there; dropping it would make a crash look like an empty pass.

    Encoding is forced, not inferred. Measured facts on this machine:
      * `locale.getpreferredencoding(False)` -> cp936, so a naive guess says cp936;
      * but a child whose stdout is a PIPE actually encodes as UTF-8
        (probe: pref=cp936 stdout=utf-8 mode=0);
      * the harness' own stdout, pointed at the terminal, is cp936 -- proved by the
        UnicodeEncodeError on '↔' (U+2194), which cp936 cannot represent.

    So the two ends of the pipe can disagree. Without -X utf8 a piped child writes GBK
    (measured: b'gbk d6d0' for U+4E2D) while this function decoded UTF-8, which turned
    every Chinese path into U+FFFD replacement characters -- and the mangled text was
    then written into gates-report.txt. Worse, '-' and other non-GBK characters do not
    merely mis-encode: with -X utf8 removed the probe child DIED with
    "UnicodeEncodeError: 'gbk' codec can't encode character '\u2194'" (the same U+2194
    that once killed this harness' own table), so a gate could be lost entirely rather
    than merely garbled. An attempt to pin the child with PYTHONIOENCODING='cp936:replace'
    did not take effect and produced the mirror-image corruption. Measured bytes settle
    it: -X utf8 makes a piped child emit UTF-8 (b'utf-8 e4b8ad' for the same char).

    Misdiagnosis worth remembering, because it nearly cost a correct file: a SECOND
    mojibake sighting ('鏅鸿兘...' in gates-report.txt) was blamed on this pipe, but the
    file was in fact correct UTF-8 all along -- PowerShell's bare `Get-Content` decodes a
    UTF-8 file as GBK when the file has no BOM. When evidence looks corrupted, check the
    reader before editing the writer.

    -X utf8 cannot be overridden by the environment and settles both ends at once. Every
    gate opens its files with an explicit `encoding=`, so this changes no file-reading
    behaviour; it only affects the pipe read below.
    """
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, '-X', 'utf8'] + argv, cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'rc': None, 'text': '', 'error': 'TIMEOUT after %ds' % timeout,
                'seconds': round(time.time() - started, 1)}
    return {'rc': proc.returncode,
            'text': proc.stdout.decode('utf-8', 'replace'),
            'error': None,
            'seconds': round(time.time() - started, 1)}


DEFAULT_JOBS_CAP = 8

# Directory NAMES skipped by the side-effect fingerprint (see the module docstring for
# why exactly these two classes). Category skips only -- there is deliberately no
# per-file exemption list, because an exemption list is a hole and the first thing
# anyone would widen when it goes red.
FINGERPRINT_SKIP_DIRS = frozenset(('__pycache__', '.git', '.venv', 'venv',
                                   '.pytest_cache', '.mypy_cache', '.ruff_cache'))
FINGERPRINT_SKIP_SUFFIXES = ('.pyc', '.pyo')


def parse_jobs(raw, cpu):
    """Pure: (value of --jobs= or None, cpu count) -> (jobs, error).

    A bad value is an ERROR, never a silent fall back to serial: `--jobs=8x` would then
    give a correct-looking run that is 3x slower, so the typo would be invisible. On
    error returns (None, message) so main() can refuse and say why.
    """
    if raw is None:
        return max(1, min(DEFAULT_JOBS_CAP, max(1, int(cpu)))), None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None, '--jobs=%r is not an integer' % (raw,)
    if n < 1:
        return None, '--jobs=%d is not >= 1' % n
    return n, None


def fingerprint(root):
    """{relative path (/-separated): (size, mtime_ns)} for every file under `root`."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in FINGERPRINT_SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(FINGERPRINT_SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[os.path.relpath(full, root).replace(os.sep, '/')] = \
                (st.st_size, st.st_mtime_ns)
    return out


def diff_fingerprint(before, after):
    """Pure: the three sets of paths that differ. Sorted, so the report is stable."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return added, removed, changed


def repo_side_effect_report(before, after):
    """Pure: (problems, one-line summary) for a pair of fingerprints.

    The zero-file case is a FAIL, not a pass: `fingerprint()` returning {} makes every
    comparison trivially empty, so the guard would print "nothing was written" forever.
    That is the vacuity pattern this project keeps meeting, so it is checked first.
    """
    added, removed, changed = diff_fingerprint(before, after)
    watched = len(after)
    problems = []
    if watched == 0:
        problems.append('the side-effect fingerprint saw 0 file(s) under the repo root, '
                        'so it can detect nothing -- "no gate wrote the repo" would be '
                        'an empty claim rather than a finding')
    for label, paths in (('created', added), ('deleted', removed),
                         ('modified', changed)):
        if paths:
            shown = ', '.join(paths[:8])
            if len(paths) > 8:
                shown += ' (+%d more)' % (len(paths) - 8)
            problems.append('gate(s) %s %d file(s) inside the repository: %s'
                            % (label, len(paths), shown))
    if problems:
        summary = ('side effects: %d created / %d deleted / %d modified (%d watched)'
                   % (len(added), len(removed), len(changed), watched))
    else:
        summary = 'gates wrote nothing (%d file(s) watched)' % watched
    return problems, summary


def run_gate_pairs(gates, jobs):
    """Yield (gate, selftest result, real result); the ORDER is whichever finishes first.

    The caller rebuilds the rows in registry order before writing the report, so
    completion order is a console-only detail and cannot change a byte of the evidence.
    A worker that raises is turned into a FAILing result instead of propagating: an
    exception here would kill the run before the report exists, and a missing report
    reads as "we did not run", which is the one thing a gate harness must never say.
    """
    def one(g):
        try:
            return g, run_argv(g['selftest']), run_argv(g['runner'])
        except Exception as exc:            # defensive: never lose the report
            dead = {'rc': None, 'text': '', 'seconds': 0.0,
                    'error': 'worker raised %s: %s' % (type(exc).__name__, exc)}
            return g, dict(dead), dead

    if jobs <= 1 or len(gates) <= 1:
        for g in gates:
            yield one(g)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(one, g) for g in gates]
        for fut in concurrent.futures.as_completed(futures):
            yield fut.result()


def order_rows(gates, rows_by_name):
    """Rows in REGISTRY order, whatever order the pool finished them in.

    Extracted and given its own sample because the entire determinism claim of the
    parallel run rests on this one line: as `rows_by_name.values()` the report would list
    gates in completion order, and two runs of the same tree would produce two different
    gates-report.txt files. Returns (rows, missing names) so the caller can refuse to
    write a short report instead of silently stamping it FULL.
    """
    rows = [rows_by_name[g['name']] for g in gates if g['name'] in rows_by_name]
    missing = [g['name'] for g in gates if g['name'] not in rows_by_name]
    return rows, missing


def parse(text):
    findings = {}
    for code in FINDING_RE.findall(text):
        findings[code] = findings.get(code, 0) + 1
    m = VERDICT_RE.search(text)
    n = None
    if m:
        tail = text[m.end():m.end() + 160]
        for pat in COUNT_PATTERNS:
            mm = pat.search(tail)
            if mm:
                n = int(mm.group(1))
                break
    return {
        'findings': findings,
        'total': sum(findings.values()),
        'verdict': m.group(1) if m else None,
        'verdict_n': n,
    }


def excerpt(text, limit=25):
    """Bounded failure output. The full text goes to the report file; the console only
    ever sees the lines that carry a verdict, never the whole dump."""
    lines = [ln for ln in text.splitlines() if EXCERPT_RE.match(ln)]
    if len(lines) > limit:
        lines = lines[:limit] + ['... (%d more lines in %s)'
                                 % (len(lines) - limit, os.path.basename(REPORT_PATH))]
    return lines


def evaluate(gate, selftest, real, baseline):
    """Pure decision function -- no I/O, so the self-test can feed it synthetic results
    and prove each branch actually fires. Returns (row, problems)."""
    problems = []
    name, tier = gate['name'], gate['tier']

    # Guard: a gate that ran but printed nothing means the CLI drifted or it crashed
    # before its first print. Either way it must not be read as success.
    if not real['text'].strip():
        problems.append('produced no output at all -- CLI drift or a crash before the '
                        'first print; cannot be read as a pass')
    if real.get('error'):
        problems.append(real['error'])

    st_status = 'n/a'
    # NOTE: the check is `is not None`, not truthiness. An earlier version used
    # `if gate.get('selftest'):`, so a gate declaring `'selftest': []` skipped this whole
    # block -- including the branch that marks a failed selftest as FAIL. The detector
    # for a broken detector was never executed, and the run reported ok=True. main()
    # now refuses to run a gate with an empty selftest, so the ambiguity is gone.
    if gate.get('selftest') is not None:
        if selftest is None or not selftest['text'].strip():
            st_status = 'NOOUT'
            problems.append('selftest produced no output')
        else:
            m = SELFTEST_RE.search(selftest['text'])
            if not m:
                st_status = 'NOMARK'
                problems.append('selftest printed no SELFTEST OK/FAIL marker')
            elif m.group(1) == 'FAIL' or selftest['rc'] != 0:
                st_status = 'FAIL'
                problems.append('selftest FAILED -- this gate cannot be shown to catch a '
                                'bad input, so its verdict carries no weight')
            else:
                st_status = 'PASS'

    pk = parse(real['text'])
    detail = ''
    skipped = False

    if tier == 'A':
        # The exit code is authoritative, but a missing verdict line is a separate
        # defect: the script ran to completion without saying what it concluded.
        if pk['verdict'] is None:
            problems.append('no "verdict:" line in the output')
        elif real['rc'] == SKIP_RC and pk['verdict'] == 'SKIPPED':
            # 第三个出口：**环境不在**（工具链探不到）。故意 ok=False —— 判据一旦
            # 「环境缺失就自动变绿」，它就不再是判据。也不落进下面那条 rc!=0 分支：
            # 「没装 JDK」与「测试红了」要分别去干两件不同的事，写成同一种红的话，
            # 报告同时丢掉了这两个信息。
            skipped = True
            problems.append('SKIPPED -- the gate exited %d saying its toolchain is absent, '
                            'and this run counts that as NOT green on purpose: a '
                            'criterion that goes green whenever its environment is '
                            'missing is not a criterion. Install the toolchain the '
                            'gate names in its own output and re-run.' % real['rc'])
        elif real['rc'] != 0:
            problems.append('exit=%s with verdict %s (%d finding(s))'
                            % (real['rc'], pk['verdict'], pk['total']))
        # 注意顺序：SKIPPED 这个结论只在「没有别的毛病」时才给。若同一条门禁还带着
        # 自测失败之类的真问题，那它就该按 FAIL 报 —— 别让「环境不在」把真红洗成
        # 一个看起来无害的 INCOMPLETE。两处的判据都是 problems 本身，不是复制一份。
        others = [p for p in problems if not p.startswith('SKIPPED --')]
        if skipped and not others:
            v_status = 'SKIPPED'
            detail = 'skipped'
        else:
            v_status = 'FAIL' if problems else 'PASS'
            detail = '%d finding(s)' % (pk['verdict_n'] if pk['verdict_n'] is not None
                                        else pk['total'])

    else:  # tier B -- ratchet
        want = baseline.get(name)
        # 'present but incomplete' counts as absent: a placeholder entry with a null
        # total would otherwise reach the comparison below and raise TypeError, i.e. a
        # crash instead of a verdict.
        if not want or want.get('total') is None or not want.get('detail'):
            v_status = 'NOBASELINE'
            detail = 'actual=%d, baseline missing' % pk['total']
            problems.append('no usable baseline (need both total and detail); actual is '
                            '%d with codes %s'
                            % (pk['total'], sorted(pk['findings'].items())))
        else:
            want_total = want['total']
            want_detail = want.get('detail', {})
            detail = '%d/%d' % (pk['total'], want_total)
            if pk['total'] > want_total:
                v_status = 'DRIFT+'
                problems.append('debt grew %d -> %d: %s'
                                % (want_total, pk['total'],
                                   {k: (want_detail.get(k, 0), v) for k, v in
                                    sorted(pk['findings'].items())
                                    if v > want_detail.get(k, 0)}))
            elif pk['total'] < want_total:
                v_status = 'DRIFT-'
                problems.append('debt shrank %d -> %d: lower the baseline in %s by hand, '
                                'otherwise the improvement is absorbed and the number '
                                'stops tracking anything'
                                % (want_total, pk['total'], os.path.basename(BASELINE_PATH)))
            elif pk['findings'] != want_detail:
                v_status = 'DRIFT~'
                problems.append('total unchanged but the composition moved: baseline=%s '
                                'actual=%s (one defect kind was traded for another)'
                                % (want_detail, pk['findings']))
            else:
                v_status = 'RATCHET'
                if real['rc'] == 0:
                    # Baseline says debt remains, yet the gate exited clean. One of the
                    # two is lying, and the baseline is worthless until it is reconciled.
                    v_status = 'FAIL'
                    problems.append('baseline records %d finding(s) but the gate exited 0 '
                                    '-- baseline and gate disagree' % want_total)

    return {'name': name, 'tier': tier, 'selftest': st_status, 'verdict': v_status,
            'detail': detail, 'ok': not problems, 'problems': problems,
            'seconds': real.get('seconds', 0),
            'output': real['text'] if not problems else real['text']}


def load_baseline():
    if not os.path.exists(BASELINE_PATH):
        return {}
    with open(BASELINE_PATH, encoding='utf-8-sig') as f:
        return json.load(f)


def scope_line(rows, all_names):
    """The report must state its own scope.

    `--gate=X` rewrites this file with a single gate's results. A reader -- human or
    agent -- who opens gates-report.txt and sees four section headers missing has no way
    to tell 'that gate did not run' from 'that gate was removed'. A partial run that
    reads as a full run is exactly the false green this project keeps running into, so
    the scope is written into the evidence itself.
    """
    covered = [r['name'] for r in rows]
    missing = [n for n in all_names if n not in covered]
    if missing:
        return ('SCOPE: PARTIAL -- %d of %d gate(s) ran; NOT covered: %s.'
                ' Do NOT read this as a full verdict.'
                % (len(covered), len(all_names), ', '.join(missing)))
    return 'SCOPE: FULL -- all %d registered gate(s) ran.' % len(all_names)


def verdict_of(rows, side_problems=()):
    """THE definition of green. Returns (all_green, [lines to write into the report]).

    Both the process exit code and the head of tools/gates-report.txt come from this one
    function, because two definitions of green drift apart exactly like two definitions
    of a count: the file would end up saying PASS while the exit code says 1 (or the
    reverse), and the reader told to 'read the report instead of re-running' has no way
    to notice. `side_problems` is part of the definition on purpose -- the side-effect
    guard failing keeps the run red, and a file that omitted it would contradict the code.

    Why the file needs an aggregate line at all: that verdict used to exist only on the
    console, and the console is not what this project points readers at. Summing gate
    headers by eye is not a verdict -- and one of those headers carries a word ('DIRTY',
    printed by tier-B scripts to mean 'found N findings') that a reader reasonably takes
    for a failure, because nothing in the file says which words are a ratchet's own and
    which are the harness'. It sits on the LAST line of the file today, so the last thing
    a reader sees is the only non-PASS word in it.

    Three ways to be red, each getting its own sentence:
      * 0 rows        -- nothing was audited. An empty report must never read as green;
                         that is the 'extraction came back empty' family, and for a
                         whole-file verdict it is worse, not better.
      * a bad gate    -- named, so the reader does not have to hunt the sections.
      * side problems -- named, so all-PASS headers cannot be totalled into a green run.
    """
    if not rows:
        return False, ['VERDICT: FAIL -- this report carries 0 gate section(s): nothing '
                       'was audited, which is not the same as nothing being wrong.']
    bad = [r['name'] for r in rows if not r['ok']]
    if bad or side_problems:
        why = []
        if bad:
            why.append('%d of %d gate(s) not ok: %s'
                       % (len(bad), len(rows), ', '.join(bad)))
        if side_problems:
            why.append('%d side-effect problem(s): %s'
                       % (len(side_problems), '; '.join(side_problems)))
        lines = ['VERDICT: FAIL -- %s. See the GATE sections below.'
                 % '; '.join(why)]
        # 「环境不在」要单独说一句，理由有两条：① 它的红**根因不在本仓库**，谁读到
        # 「<k> of <n> gate(s) not ok」都会先去翻那条门禁的代码，而这里该干的是装工具链；
        # ② 它照红是**故意的**（判据一旦环境缺失就变绿就等于没有判据），不写出来
        # 下一个人会把它当成 harness 的 bug 去「修」。措辞与 SKIPPED 分支同源但不复用
        # 字符串 —— 这里说的是文件里的读者，那里说的是这条门禁本身。
        sk = [r for r in rows if r['verdict'] == 'SKIPPED']
        if sk:
            lines.append(
                'NOTE: %s exited 3 = SKIPPED: the gate itself says its toolchain is '
                'absent here, so it proved nothing either way. This run counts that as '
                'NOT green on purpose -- a criterion that goes green whenever its '
                'environment is missing is not a criterion -- but it is a different '
                'to-do from a gate that FAILED: install what the gate names and re-run, '
                'do not go read the gate.'
                % ', '.join('%s' % r['name'] for r in sk))
        return False, lines
    lines = ['VERDICT: PASS -- %d of %d gate(s) ok.' % (len(rows), len(rows))]
    odd = [r for r in rows if r['verdict'] != 'PASS']
    if odd:
        lines.append(
            'NOTE: %s report a body verdict other than PASS and still count as ok. A '
            'tier-B ratchet prints DRIFT+/DRIFT-/DRIFT~ in its GATE header when the debt '
            'moved, and RATCHET at baseline; the line under it says verdict: DIRTY in '
            'every one of those cases (DIRTY is that script\'s own word for found N '
            'findings; it prints CLEAN when the count is zero). Read the GATE header, '
            'not that word.'
            % ', '.join('%s=%s %s' % (r['name'], r['verdict'], r['detail']) for r in odd))
    return True, lines


def write_report(rows, registry, report_path=REPORT_PATH, notes=(), verdict=()):
    """`registry` must be the WHOLE gate list, never the --gate= filtered subset.

    scope_line() answers 'which registered gates are missing from rows?', so the
    comparison set has to be the registry. Handed the subset instead, the comparison
    shrinks with the selection and a one-gate run stamps 'SCOPE: FULL -- all 1
    registered gate(s) ran.' on its own artifact -- the exact false green the scope line
    exists to prevent. The parameter is named `registry` (not `gates_by_name`) on
    purpose: the old name invited passing a mapping built from the filtered list.

    `notes` are harness-level lines printed immediately after the scope line (currently
    the side-effect verdict). They belong IN the evidence and not only on the console:
    the reader of this file does not have the console, and a guard whose result is
    absent from the file cannot be audited from the file.
    """
    gates_by_name = {g['name']: g for g in registry}
    lines = []
    lines.append(scope_line(rows, list(gates_by_name)))
    # 汇总结论写进证据文件的开头，紧挨 scope 行：读这份文件的人没有控制台，而
    # 「把 17 条 GATE 表头加一遍」不是结论。顺序 = 先「跑了什么」再「结论如何」。
    for vline in verdict:
        lines.append(vline)
    for note in notes:
        lines.append(note)
    lines.append('')
    for r in rows:
        lines.append('=' * 78)
        lines.append('GATE %s (tier %s) selftest=%s verdict=%s %s'
                     % (r['name'], r['tier'], r['selftest'], r['verdict'], r['detail']))
        lines.append('what: %s' % gates_by_name[r['name']]['what'])
        lines.append('argv: %s' % ' '.join(gates_by_name[r['name']]['runner']))
        for p in r['problems']:
            lines.append('PROBLEM: %s' % p)
        lines.append('-' * 78)
        lines.append(r['output'].rstrip())
        lines.append('')
    text = '\n'.join(lines)
    with open(report_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    # PHYSICAL lines, not list elements. `lines` holds one element per *field*, and
    # `r['output']` is a single element carrying a gate's entire multi-line log, so
    # len(lines) undercounted by ~2.5x: the printed '(79 lines)' described a 201-line
    # file. A wrong number in the harness' own summary of its own evidence is the same
    # defect family as a scope line that lies -- the reader cannot tell it is wrong.
    # Definition: what a reader counting the file sees, i.e. `f.readlines()` semantics --
    # a trailing newline terminates the last line instead of inventing an empty one.
    # (`text.count('\n') + 1` double-counts that trailing newline; the first version of
    # this fix did, and the sample below caught it: claimed=13 physical=12.)
    n_newlines = text.count('\n')
    return n_newlines if text.endswith('\n') else n_newlines + 1


def print_table(rows):
    print('%-20s %-5s %-9s %-10s %-14s %s'
          % ('GATE', 'TIER', 'SELFTEST', 'VERDICT', 'DETAIL', 'WHAT'))
    for r in rows:
        print('%-20s %-5s %-9s %-10s %-14s %s'
              % (r['name'], r['tier'], r['selftest'], r['verdict'], r['detail'],
                 gates_what_short[r['name']]))


gates_what_short = {}


def selftest():
    """Proves each branch of evaluate() can fire. Synthetic results only -- no gate is
    executed, so this cannot be masked by a gate that happens to be green today."""
    ok = True

    def expect(tag, gate, st, real, baseline, want_ok):
        nonlocal ok
        row = evaluate(gate, st, real, baseline)
        hit = row['ok'] == want_ok
        print('  [%s] verdict=%s ok=%s %s'
              % (tag, row['verdict'], row['ok'], 'OK' if hit else 'MISSED'))
        if not hit:
            print('      problems=%s' % row['problems'])
        ok = ok and hit

    good = {'rc': 0, 'text': 'verdict: PASS (0 issue(s))\n', 'error': None, 'seconds': 0.1}
    bad = {'rc': 1, 'text': 'ISSUE [C3] bad\nverdict: FAIL (1 issue(s))\n',
           'error': None, 'seconds': 0.1}
    stok = {'rc': 0, 'text': 'SELFTEST OK: all detectors fire\n', 'error': None,
            'seconds': 0.1}
    stbad = {'rc': 1, 'text': 'SELFTEST FAIL\n', 'error': None, 'seconds': 0.1}
    empty = {'rc': 0, 'text': '', 'error': None, 'seconds': 0.1}
    nomark = {'rc': 0, 'text': 'selftest ran\n', 'error': None, 'seconds': 0.1}

    ga = {'name': 'a', 'tier': 'A', 'what': 'x', 'runner': [], 'selftest': ['placeholder']}
    gb = {'name': 'b', 'tier': 'B', 'what': 'x', 'runner': [], 'selftest': ['placeholder']}
    base = {'b': {'total': 3, 'detail': {'T2': 3}}}

    # Encoding round-trip, using a REAL child. Synthetic results cannot detect a wrong
    # pipe encoding, and that failure mode is silent: gates-report.txt keeps looking
    # fine, with one line of corrupted audit evidence in the middle of it. The probe
    # prints U+2194 as well -- the character that once killed a whole gate run -- so this
    # control also proves that failure mode is gone. Source is pure ASCII on purpose, so
    # nothing in this assertion can itself be mangled.
    probe = run_argv(['-c', 'print(chr(0x4e2d)+chr(0x6587)+chr(0x2194))'])
    want = '\u4e2d\u6587\u2194'
    got = probe['text'].strip()
    hit = (got == want and '\ufffd' not in probe['text'])
    # Code points, not characters: this harness' own stdout is cp936 and cannot render
    # U+2194, so printing the string would show '?' on a *successful* round trip and read
    # as a failure. Digits are unambiguous and survive any code page. On a miss the
    # child's whole traceback comes back too, so that case is downgraded to an ASCII
    # snippet -- a wall of code points buries the one number worth comparing.
    if len(got) < 16:
        shown = str([ord(c) for c in got])
    else:
        shown = got[:70].encode('ascii', 'replace').decode('ascii')
    print('  [child-encoding-roundtrip] got=%s want=%s %s'
          % (shown, [0x4e2d, 0x6587, 0x2194], 'OK' if hit else 'MISSED'))
    ok = ok and hit

    # The scope line: a partial run must say so, in the artifact it writes.
    part = scope_line([{'name': 'a'}], ['a', 'b'])
    full = scope_line([{'name': 'a'}, {'name': 'b'}], ['a', 'b'])
    hit = ('PARTIAL' in part and 'b' in part and 'NOT read this as a full verdict' in part
           and 'PARTIAL' not in full and 'FULL' in full)
    print('  [scope-line-honest] partial=%s full=%s %s'
          % (part[:34], full[:34], 'OK' if hit else 'MISSED'))
    ok = ok and hit

    # End-to-end control for the line above: scope_line() being correct proves nothing
    # unless write_report() is WIRED to it with the whole registry. It was not -- the
    # caller handed it the --gate= filtered list, so the comparison set shrank with the
    # selection and a one-gate run stamped 'SCOPE: FULL -- all 1 registered gate(s) ran.'
    # onto its own report. A pure-function test can never see a wrong argument at a call
    # site, so this drives a REAL --gate= run into a throwaway report and reads the bytes
    # that actually landed there. The gate's own verdict is deliberately NOT asserted:
    # the scope line has to be honest whether that gate is green or red today.
    tmp_report = os.path.join(tempfile.gettempdir(), 'gates-selftest-partial.txt')
    picked_one = GATES[0]['name']
    not_run = [g['name'] for g in GATES if g['name'] != picked_one]
    first = ''
    second = ''
    third = ''
    e2e_out = ''
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            execute([picked_one], report_path=tmp_report)
        e2e_out = buf.getvalue()
        with open(tmp_report, encoding='utf-8-sig') as f:
            first = f.readline().strip()
            second = f.readline().strip()
            third = f.readline().strip()
    except (OSError, IndexError) as exc:
        first = '(unreadable: %s)' % exc
    hit = (len(GATES) >= 2 and first.startswith('SCOPE: PARTIAL')
           and ('1 of %d gate(s) ran' % len(GATES)) in first and not_run[0] in first)
    print('  [scope-line-wired-e2e] picked=%s first=%s %s'
          % (picked_one, first[:40], 'OK' if hit else 'MISSED'))
    ok = ok and hit

    # 同一趟里再证一次「副作用守卫接上了」。上面那条 scope 守卫踩过的坑正是
    # 「纯函数全对、调用点传错了参数」，所以这里也只认真实运行留下的字节：
    # 报告的第 3 行必须是守卫自己的结论（第 1 行是 scope、第 2 行是汇总结论），
    # 且必须是「没写」；控制台上也要有同一行
    # （报告与终端不许各说一套）。这同时是一条**防误报**样本 —— 真实运行里
    # verify_dashboard 会 import quanauto 并落 __pycache__，守卫若把那算成写入就会
    # 常红，而常红的守卫一定会被人关掉。
    want_note = 'SIDE-EFFECT: gates wrote nothing'
    hit = (third.startswith(want_note) and 'file(s) watched' in third
           and want_note in e2e_out)
    print('  [sideeffect-guard-wired-e2e] report_line3=%s %s'
          % (third[:52], 'OK' if hit else 'MISSED'))
    if not hit:
        print('      console lines=%s'
              % [l for l in e2e_out.splitlines()
                 if l.startswith('SIDE-EFFECT:') or l.startswith('GATE FAIL')])
    ok = ok and hit

    # 报告的**汇总结论**同样只认真实运行留下的字节：文件第 2 行与控制台那行必须
    # 同源（都由 verdict_of() 产出）。这个门禁以前踩过的正是「两处各判一次绿」——
    # 只要两处各自演化，早晚一处 PASS 一处 FAIL，而项目恰恰叫人「读报告而不是
    # 重跑」，分叉时读文件的人正好无从发现。这里**不断言那个门禁本身是绿是红**
    # （同一个道理：scope 样本也不断言它），只断言两边一致。
    console_verdict = [l for l in e2e_out.splitlines() if l.startswith('verdict: ')]
    hit = (bool(console_verdict) and second.startswith('VERDICT: ')
           and second.startswith('VERDICT: PASS')
           == console_verdict[0].startswith('verdict: PASS'))
    print('  [verdict-line-agrees-with-console] report_line2=%s console=%s %s'
          % (second[:32], console_verdict[0][:32] if console_verdict else '(none)',
             'OK' if hit else 'MISSED'))
    ok = ok and hit
    try:
        os.remove(tmp_report)
    except OSError:
        pass

    # ---- verdict_of() 的每个出口都要有样本 ---------------------------------------
    # 这是全仓库「绿」的唯一定义，它有四种出口：没毛病 / 有坏门禁 / 有副作用问题 /
    # **一行都没有**。少测一支 ⇒ 「那一支没人守」与「那一支不会出事」在报告里长得
    # 一模一样（本项目历史上最贵的那类假绿）。最后一支尤其要测：空报告报 0 条
    # GATE 却 PASS，就是把「什么都没审」印成「什么都没错」。
    def _vrow(name, ok_flag, verdict='PASS', detail=''):
        return {'name': name, 'tier': 'A', 'selftest': 'PASS', 'verdict': verdict,
                'detail': detail, 'ok': ok_flag, 'problems': [], 'seconds': 0.0,
                'output': ''}

    green, vlines = verdict_of([_vrow('a', True), _vrow('b', True)])
    hit = (green is True and len(vlines) == 1
           and vlines[0].startswith('VERDICT: PASS -- 2 of 2 gate(s) ok.'))
    print('  [verdict-clean-is-green] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    green, vlines = verdict_of([_vrow('a', True), _vrow('b', False)])
    hit = (green is False
           and any(l.startswith('VERDICT: FAIL') and '1 of 2' in l and 'b' in l
                   for l in vlines))
    print('  [verdict-red-names-the-gate] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    green, vlines = verdict_of([_vrow('a', True)],
                               ['gate(s) created 1 file(s) inside the repository'])
    hit = (green is False
           and any(l.startswith('VERDICT: FAIL') and 'side-effect problem' in l
                   for l in vlines))
    print('  [verdict-red-names-side-effects] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    # 第五支：有门禁是 **SKIPPED**（退出码 3 = 环境不在）。红，但要在文件开头就说清
    # 「这不是去读那条门禁的信号，是去装工具链的信号」。断言两件事：① 仍是红；
    # ② VERDICT 行后面**真的跟了**那句 NOTE（只断言「红」的话，把 NOTE 删掉也照样绿）。
    green, vlines = verdict_of([_vrow('a', True),
                                _vrow('b', False, verdict='SKIPPED', detail='skipped')])
    hit = (green is False
           and vlines[0].startswith('VERDICT: FAIL')
           and any(l.startswith('NOTE:') and 'SKIPPED' in l and 'b' in l
                   for l in vlines[1:]))
    print('  [verdict-red-explains-a-skipped-gate] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit
    # 干净侧：没有 SKIPPED 时**不许**多印那句 NOTE（否则读的人会去找一个不存在的
    # 「环境缺了」）。上面绿的样本已断言 len(vlines)==1，这里补的是「有别的红、
    # 但没有 SKIPPED」时也不许多印。
    green, vlines = verdict_of([_vrow('a', False, verdict='FAIL')])
    hit = green is False and not any(l.startswith('NOTE:') for l in vlines)
    print('  [verdict-note-only-when-actually-skipped] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    green, vlines = verdict_of([])
    hit = (green is False and len(vlines) == 1 and vlines[0].startswith('VERDICT: FAIL'))
    print('  [verdict-empty-report-is-NOT-green] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    green, vlines = verdict_of([_vrow('r', True, verdict='RATCHET', detail='2/2')])
    hit = (green is True and len(vlines) == 2
           and any(l.startswith('NOTE:') and 'DIRTY' in l and 'RATCHET' in l
                   for l in vlines))
    print('  [verdict-note-explains-dirty] %s' % ('OK' if hit else 'MISSED'))
    ok = ok and hit

    # ---- 副作用指纹的四类出口，外加一条空转守卫样本 ------------------------------
    # 每一项都必须有样本：少一个，「守卫没接上」和「确实没写」在报告里长得一样。
    sandbox = tempfile.mkdtemp(prefix='gates-sideeffect-')
    empty_dir = tempfile.mkdtemp(prefix='gates-sideeffect-empty-')
    watched = os.path.join(sandbox, 'watched.txt')
    with open(watched, 'w', encoding='utf-8', newline='\n') as f:
        f.write('before\n')

    def side_probe(mutate):
        b = fingerprint(sandbox)
        mutate()
        return repo_side_effect_report(b, fingerprint(sandbox))

    def side_expect(tag, mutate, want_label, want_path):
        nonlocal ok
        problems, _summary = side_probe(mutate)
        hit = (len(problems) == 1
               and any(want_label in p and want_path in p for p in problems))
        print('  [%s] problems=%s %s' % (tag, problems, 'OK' if hit else 'MISSED'))
        ok = ok and hit

    def _modify():
        # 长度也变了，(size, mtime_ns) 两半都会动，不会依赖时钟精度
        with open(watched, 'a', encoding='utf-8', newline='\n') as f:
            f.write('after-after-after\n')

    def _create():
        with open(os.path.join(sandbox, 'new.txt'), 'w', encoding='utf-8',
                  newline='\n') as f:
            f.write('x\n')

    def _delete():
        os.remove(os.path.join(sandbox, 'new.txt'))

    def _cache():
        d = os.path.join(sandbox, '__pycache__')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'x.pyc'), 'wb') as f:
            f.write(b'\x00\x01\x02')
        with open(os.path.join(sandbox, 'loose.pyc'), 'wb') as f:
            f.write(b'\x00\x01\x02')

    pr, sm = side_probe(lambda: None)
    hit = (pr == [] and '1 file(s) watched' in sm)
    print('  [sideeffect-clean] problems=%s summary=%s %s'
          % (pr, sm, 'OK' if hit else 'MISSED'))
    ok = ok and hit
    side_expect('sideeffect-modified', _modify, 'modified', 'watched.txt')
    side_expect('sideeffect-created', _create, 'created', 'new.txt')
    side_expect('sideeffect-deleted', _delete, 'deleted', 'new.txt')
    # 豁免必须是**类别**而不是逐个文件：真实运行里 __pycache__ 一定会出现。
    # 判据取「看着的文件数不变」而不是「problems 为空」—— 后者在指纹本身空转时
    # 照样成立。
    pr, sm = side_probe(_cache)
    hit = (pr == [] and '1 file(s) watched' in sm)
    print('  [sideeffect-cache-class-ignored] problems=%d summary=%s %s'
          % (len(pr), sm, 'OK' if hit else 'MISSED'))
    ok = ok and hit
    # 空转守卫：指纹看不到任何文件时，比对永远为空、守卫会永远打印「没写」。
    pr, sm = repo_side_effect_report(fingerprint(empty_dir), fingerprint(empty_dir))
    hit = (len(pr) == 1 and 'saw 0 file(s)' in pr[0])
    print('  [sideeffect-empty-is-a-failure] problems=%s %s'
          % (pr, 'OK' if hit else 'MISSED'))
    ok = ok and hit

    for _d in (sandbox, empty_dir):
        shutil.rmtree(_d, ignore_errors=True)

    # '(N lines)' must be the number of physical lines in the file, checked against the
    # bytes on disk rather than against the formula -- a refactor of the join must not be
    # able to keep this green while the number drifts again. `readlines()` is a different
    # stdlib path from the count-and-join above, so it is an independent witness. The
    # sample row's output holds embedded newlines on purpose: that is the shape
    # len(lines) miscounted, and the trailing-newline shape that broke the first fix.
    tmp_lines = os.path.join(tempfile.gettempdir(), 'gates-selftest-lines.txt')
    fake_row = {'name': 'a', 'tier': 'A', 'selftest': 'PASS', 'verdict': 'PASS',
                'detail': 'x', 'problems': [], 'output': 'l1\nl2\nl3\nl4\nl5'}
    claimed = write_report([fake_row], [ga], tmp_lines)
    with open(tmp_lines, encoding='utf-8-sig') as f:
        physical = len(f.readlines())
    hit = claimed == physical
    print('  [report-line-count-honest] claimed=%d physical=%d %s'
          % (claimed, physical, 'OK' if hit else 'MISSED'))
    ok = ok and hit
    try:
        os.remove(tmp_lines)
    except OSError:
        pass

    # A gate with no selftest at all must be rejected, not silently accepted as green:
    # it would be a detector nobody has ever seen fire.
    noself = {'name': 'c', 'tier': 'A', 'what': 'x', 'runner': [], 'selftest': []}
    row = evaluate(noself, None, good, base)
    hit = (not row['ok']) and any('cannot be shown to catch' in p or 'no output' in p
                                  for p in row['problems'])
    print('  [noselftest-gate-rejected] verdict=%s ok=%s %s'
          % (row['verdict'], row['ok'], 'OK' if hit else 'MISSED'))
    ok = ok and hit

    expect('A-clean', ga, stok, good, base, True)
    # Positive control for the parser: the md-fidelity gate phrases its count as
    # 'total missing=0'. A hard-coded '(N issue(s))' regex used to read this as 'no
    # verdict line' and FAIL a healthy gate.
    expect('A-alt-count-phrasing', ga, stok,
           {'rc': 0, 'text': 'verdict: PASS (total missing=0)\n', 'error': None,
            'seconds': 0.1}, base, True)
    expect('A-red', ga, stok, bad, base, False)
    expect('A-nooutput', ga, stok, empty, base, False)
    # tier-A 的第三种出口：**环境不在**（rc=3 + verdict: SKIPPED，本仓库目前只有
    # platform-runtime 用它）。两条要求缺一不可：① 不算绿（「环境缺失就自动变绿」
    # 的判据等于没有判据）；② 不能退化成下面那条 rc!=0 的通用红 —— 那会把
    # 「没装 JDK」和「测试红了」印成同一句话，而这两件事要人去干完全不同的活。
    # 这里直接断言 problems 的措辞，因为 ok 在两支里都是 False，光看 ok 分不出走了哪支。
    row = evaluate(ga, stok,
                   {'rc': SKIP_RC, 'text': 'SKIP mvn not found on PATH\n'
                                           'verdict: SKIPPED (mvn not found on PATH)\n',
                    'error': None, 'seconds': 0.1}, base)
    hit = (row['ok'] is False and row['verdict'] == 'SKIPPED'
           and any('SKIPPED' in p and 'NOT green' in p for p in row['problems'])
           and not any('with verdict SKIPPED' in p for p in row['problems']))
    print('  [A-skip-is-not-green] verdict=%s ok=%s %s'
          % (row['verdict'], row['ok'], 'OK' if hit else 'MISSED'))
    if not hit:
        print('      problems=%s' % row['problems'])
    ok = ok and hit
    # 反向样本：退了 3 但**没有**说 SKIPPED（例如脚本崩在探工具链之前）不能被当成
    # 「环境不在」放行 —— 否则任何崩溃都成了免检通道。它必须落到通用红。
    expect('A-rc3-without-skipped-verdict', ga, stok,
           {'rc': SKIP_RC, 'text': 'ISSUE [PB-X] boom\nverdict: FAIL (1 issue(s))\n',
            'error': None, 'seconds': 0.1}, base, False)
    # 第三种出口**不能把真红洗白**：同一条门禁若自测是坏的（这条最严重 —— 它意味着这条
    # 门禁证明不了自己有牙），报出来的必须是 FAIL 而不是 SKIPPED。这里断言的就是那句
    # 「others 非空 ⇒ 不给 SKIPPED」，光看 ok 分不出这两支。
    row = evaluate(ga, stbad,
                   {'rc': SKIP_RC, 'text': 'verdict: SKIPPED (no toolchain)\n',
                    'error': None, 'seconds': 0.1}, base)
    hit = (row['ok'] is False and row['verdict'] == 'FAIL'
           and any('selftest FAILED' in p for p in row['problems']))
    print('  [A-skip-does-not-hide-a-broken-selftest] verdict=%s ok=%s %s'
          % (row['verdict'], row['ok'], 'OK' if hit else 'MISSED'))
    if not hit:
        print('      problems=%s' % row['problems'])
    ok = ok and hit
    expect('A-noverdict-line', ga, stok,
           {'rc': 0, 'text': 'all good honest\n', 'error': None, 'seconds': 0.1}, base,
           False)
    expect('SELFTEST-failed', ga, stbad, good, base, False)
    expect('SELFTEST-nomarker', ga, nomark, good, base, False)
    expect('B-ratchet-holds', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] x\nFINDING [T2] y\nFINDING [T2] z\n'
                             'verdict: DIRTY (3 finding(s))\n',
            'error': None, 'seconds': 0.1}, base, True)
    expect('B-debt-grew', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] w\n' * 4 + 'verdict: DIRTY (4 finding(s))\n',
            'error': None, 'seconds': 0.1}, base, False)
    expect('B-debt-shrank', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] x\nFINDING [T2] y\n'
                             'verdict: DIRTY (2 finding(s))\n',
            'error': None, 'seconds': 0.1}, base, False)
    expect('B-composition-moved', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] x\nFINDING [T1] y\nFINDING [T1] z\n'
                             'verdict: DIRTY (3 finding(s))\n',
            'error': None, 'seconds': 0.1}, base, False)
    expect('B-baseline-absent', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] x\nverdict: DIRTY (1 finding(s))\n',
            'error': None, 'seconds': 0.1}, {}, False)
    expect('B-baseline-incomplete', gb, stok,
           {'rc': 1, 'text': 'FINDING [T2] x\nverdict: DIRTY (1 finding(s))\n',
            'error': None, 'seconds': 0.1},
           {'b': {'total': None, 'detail': None}}, False)
    expect('B-baseline-and-gate-disagree', gb, stok, good, base, False)

    # 带连字符的检查码必须被算进 total：棘轮（tier-B）直接拿 total 与基线比，
    # 少算一个就等于把那一份欠账放行。这条用纯函数断言，因为走 expect() 的话
    # 「认不出代码 ⇒ total=0 ⇒ 欠账缩水 ⇒ FAIL」与「认出了但确实缩水」同样都是
    # ok=False，样本根本分不出对错。
    pk_h = parse('FINDING [MAP-PATHS] x\nFINDING [T1] y\nverdict: FAIL (2 issue(s))\n')
    hit = (pk_h['total'] == 2 and pk_h['findings'] == {'MAP-PATHS': 1, 'T1': 1})
    print('  [hyphen-code-counted] total=%s codes=%s %s'
          % (pk_h['total'], dict(sorted(pk_h['findings'].items())),
             'OK' if hit else 'MISSED'))
    ok = ok and hit

    # --jobs：默认值、上限、显式值、以及**坏值必须报错**。坏值静默退回串行是最坏的
    # 一种处理 —— 报告完全正确、只是慢 3 倍，打字错误就此隐身。cpu=0 也要落在 1，
    # 否则池宽 0 会让整趟一个门禁都不跑。
    j_default, e_default = parse_jobs(None, 4)
    j_capped, _ = parse_jobs(None, 64)
    j_one, _ = parse_jobs('1', 64)
    j_bad, e_bad = parse_jobs('8x', 4)
    j_zero, e_zero = parse_jobs('0', 4)
    j_nocpu, _ = parse_jobs(None, 0)
    hit = (j_default == 4 and e_default is None and j_capped == DEFAULT_JOBS_CAP
           and j_one == 1 and j_bad is None and bool(e_bad)
           and j_zero is None and bool(e_zero) and j_nocpu == 1)
    print('  [jobs-parse] default=%s capped=%s explicit=%s bad=%s zero=%s cpu0=%s %s'
          % (j_default, j_capped, j_one, e_bad, e_zero, j_nocpu,
             'OK' if hit else 'MISSED'))
    ok = ok and hit

    # 并行唯一能污染证据的通道就是「行的顺序」。这里喂给 order_rows 一个**完成顺序**
    # （倒序）的映射，要求它照样输出注册表顺序；否则同一棵树跑两次会得到两份不同的
    # gates-report.txt。半份结果必须被报成 missing，而不是写成一条 FULL。
    three = [{'name': n} for n in ('alpha', 'beta', 'gamma')]
    got, miss = order_rows(three, {'gamma': {'name': 'gamma'},
                                   'alpha': {'name': 'alpha'},
                                   'beta': {'name': 'beta'}})
    short, short_miss = order_rows(three, {'alpha': {'name': 'alpha'}})
    hit = ([r['name'] for r in got] == ['alpha', 'beta', 'gamma'] and not miss
           and short_miss == ['beta', 'gamma'] and len(short) == 1)
    print('  [parallel-order-is-registry-order] order=%s short_missing=%s %s'
          % ([r['name'] for r in got], short_miss, 'OK' if hit else 'MISSED'))
    ok = ok and hit

    # 最后一条，也是最重要的一条：上面所有副作用样本都只证明「比对算术对」。它们
    # 一条都证明不了 `execute()` 把结论接到了退出码上 —— 接线漏掉时，一个真的会写
    # 仓库的门禁拿到 PASS，而报告里只多一行没人会看的 SIDE-EFFECT。所以这里让
    # execute() 自己在两次指纹之间写一个文件，断言退出码 == 1、控制台点名、报告里
    # 留下 PROBLEM 行。删掉守卫 ⇒ 这一条必红（其余样本全绿）。
    probe = os.path.join(ROOT, '_selftest_side_effect_probe.txt')
    tmp_w_report = os.path.join(tempfile.gettempdir(), 'gates-selftest-write.txt')
    argv_backup = list(sys.argv)
    rc_wrote = None
    wrote_out = ''
    rep2 = ''
    try:
        if os.path.exists(probe):
            os.remove(probe)
        sys.argv.append('--selftest-simulate-write=' + probe)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc_wrote = execute([picked_one], report_path=tmp_w_report)
        wrote_out = buf2.getvalue()
        with open(tmp_w_report, encoding='utf-8-sig') as f:
            rep2 = f.read()
    except (OSError, IndexError) as exc:
        rep2 = '(unreadable: %s)' % exc
    finally:
        sys.argv[:] = argv_backup
        if os.path.exists(probe):
            os.remove(probe)

    def _fired(text):
        return ('PROBLEM: gate(s) created 1 file(s) inside the repository: '
                '_selftest_side_effect_probe.txt') in text

    hit = (rc_wrote == 1
           and 'GATE FAIL: gate(s) created 1 file(s) inside the repository' in wrote_out
           and 'SIDE-EFFECT GUARD FAILED' in wrote_out
           and 'SIDE-EFFECT: side effects: 1 created' in wrote_out
           and 'SIDE-EFFECT: side effects: 1 created' in rep2
           and _fired(rep2)
           # 连判决词一起钉住。注释里不写「本来写错过」—— 这一行从未写错过；是
           # 上面那次证伪变异（把 side_problems 从 all_green 里摘掉）让同一条运行
           # 印出了 `PASS (1/1 gate(s) green; SIDE-EFFECT GUARD FAILED)`：一行里
           # PASS 与 GUARD FAILED 并存。判据只断言「有 SIDE-EFFECT GUARD FAILED
           # 字样」的话，那种自相矛盾的行照样算过，所以断言的必须是整句。
           and 'verdict: FAIL (1/1 gate(s) green; SIDE-EFFECT GUARD FAILED)' in wrote_out
           # 报告的**第 2 行**也必须在**这一趟真红的运行里**说出同一条结论。这是
           # 「一份产物两种读法」的唯一可自动化的观测点：只要有人让 verdict_of()
           # 不再把副作用问题算进去（或让它不接进文件），报告就会在这一趟写 PASS，
           # 而控制台那行仍写 FAIL —— 读文件的人与读终端的人会得到相反的结论。
           # 断言的是内容（点名 side-effect problem）而不只是「有个 VERDICT 行」。
           and rep2.splitlines()[1].startswith('VERDICT: FAIL')
           and 'side-effect problem' in rep2.splitlines()[1])
    print('  [sideeffect-guard-fails-e2e] rc=%s %s' % (rc_wrote, 'OK' if hit else 'MISSED'))
    if not hit:
        print('      stdout=%s'
              % [l for l in wrote_out.splitlines()
                 if l.startswith('SIDE-EFFECT') or l.startswith('GATE FAIL')
                 or l.startswith('verdict:')])
        print('      report_notes=%s' % [l for l in rep2.splitlines()[:6]])
    ok = ok and hit
    for _p in (tmp_w_report,):
        try:
            os.remove(_p)
        except OSError:
            pass

    # Registry guards: an empty or tier-A-less registry must not produce a green run.
    reg_ok = bool(GATES) and any(g['tier'] == 'A' for g in GATES)
    print('  [registry-nonempty] gates=%d %s' % (len(GATES), 'OK' if reg_ok else 'MISSED'))
    ok = ok and reg_ok

    print('SELFTEST %s' % ('OK: every branch fires, ratchet holds on an unchanged count'
                           if ok else 'FAIL'))
    return 0 if ok else 2


def main():
    harden_stdout()
    if '--selftest' in sys.argv:
        return selftest()
    if '--list' in sys.argv:
        for g in GATES:
            print('%-20s tier=%s  %s' % (g['name'], g['tier'], g['what']))
            print('%-20s   real: %s' % ('', ' '.join(g['runner'])))
            if g.get('selftest'):
                print('%-20s   self: %s' % ('', ' '.join(g['selftest'])))
        return 0

    picked = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--gate=')]
    return execute(picked)


def execute(picked, report_path=REPORT_PATH):
    """Run the selected gates and write the report; returns the process exit code.

    Split out of main() so the selftest can drive a REAL --gate= run against a throwaway
    report path and read the bytes it produced. That matters here: the scope guard was
    green while the report lied, because the bug was in the call site, not in
    scope_line(). No amount of testing the pure function can see a wrong argument at the
    one place it is called.
    """
    gates = [g for g in GATES if not picked or g['name'] in picked]
    baseline = load_baseline()

    # Harness-level vacuity guards. A registry that silently lost its gates would print
    # a clean-looking table and exit 0 -- the exact failure this whole file exists to
    # prevent, so it is checked before anything else.
    if not GATES:
        print('GATE FAIL: the registry is empty -- nothing was checked, refusing to '
              'report success')
        return 1
    if not any(g['tier'] == 'A' for g in GATES):
        print('GATE FAIL: no tier-A gate registered -- every gate is debt-tracked, so a '
              'green run would mean nothing')
        return 1
    if picked and not gates:
        print('GATE FAIL: --gate=%s matched nothing; known: %s'
              % (picked, [g['name'] for g in GATES]))
        return 1
    # Every registered gate must carry a self-test. Without one there is no evidence the
    # detectors can still fire, and a green verdict from it means nothing.
    noself = [g['name'] for g in gates if not g.get('selftest')]
    if noself:
        print('GATE FAIL: gate(s) registered without a selftest: %s -- a detector nobody '
              'has ever seen fail is not a detector' % noself)
        return 1

    raw_jobs = None
    for a in sys.argv:
        if a.startswith('--jobs='):
            raw_jobs = a.split('=', 1)[1]
    jobs, jerr = parse_jobs(raw_jobs, os.cpu_count() or 1)
    if jerr:
        print('GATE FAIL: %s -- refusing to fall back silently, because a wrong pool '
              'width still produces a correct-looking report' % jerr)
        return 1

    # Snapshot the tree BEFORE any gate runs. The matching `after` snapshot is taken
    # before write_report(), so this report file cannot be a false positive of itself.
    before = fingerprint(ROOT)

    # SELFTEST-ONLY HOOK, and it deliberately sits AFTER the `before` snapshot: it exists
    # so that `--selftest` can show a run going red end to end instead of only asserting
    # the comparison arithmetic. The guard's whole job is to stop a report from being
    # stamped FULL while a gate wrote the tree, and the way that fails in practice is not
    # a wrong diff -- it is `side_problems` never reaching the exit code. Without this
    # sample, deleting the two `all_green` clauses leaves every other sample green.
    # Called from the selftest ONLY; a normal run has no such argv entry.
    for _a in sys.argv:
        if _a.startswith('--selftest-simulate-write='):
            with open(_a.split('=', 1)[1], 'w', encoding='utf-8', newline='\n') as _f:
                _f.write('simulated gate side effect\n')

    gates_what_short.update({g['name']: g['what'][:44] for g in gates})
    print('  running %d gate(s) with jobs=%d (per gate: selftest then real)'
          % (len(gates), jobs))
    rows_by_name = {}
    for g, st, real in run_gate_pairs(gates, jobs):
        row = evaluate(g, st, real, baseline)
        rows_by_name[g['name']] = row
        print('  ran %-20s selftest=%-6s verdict=%-10s (%.1fs)'
              % (g['name'], row['selftest'], row['verdict'], row['seconds']))

    # Completion order must not leak into the evidence.
    rows, missing_rows = order_rows(gates, rows_by_name)
    if missing_rows:
        print('GATE FAIL: %d of %d gate(s) produced no result (%s) -- refusing to write '
              'a report that would read as a full verdict'
              % (len(missing_rows), len(gates), ', '.join(missing_rows)))
        return 1

    side_problems, side_summary = repo_side_effect_report(before, fingerprint(ROOT))
    print('SIDE-EFFECT: %s' % side_summary)
    for p in side_problems:
        print('GATE FAIL: %s' % p)

    print('')
    print_table(rows)
    print('')

    failed = [r for r in rows if not r['ok']]
    for r in failed:
        print('--- %s output (bounded) ---' % r['name'])
        for ln in excerpt(r['output']):
            print('  ' + ln)
    if '--full' in sys.argv:
        for r in rows:
            print('=== %s full output ===' % r['name'])
            print(r['output'])

    # The side-effect verdict goes INTO the report, not only onto the console: whoever
    # reads gates-report.txt has no console, and a guard whose result is missing from the
    # file cannot be audited from the file.
    notes = ['SIDE-EFFECT: %s' % side_summary] + ['PROBLEM: %s' % p for p in side_problems]
    # 一句话结论与退出码同源：两处各自判绿早晚会分叉（文件写 PASS 而退出码 1，或反过来），
    # 而本项目恰恰叫人「读报告而不是重跑」——分叉时那个读文件的人无从发现。
    all_green, verdict_lines = verdict_of(rows, side_problems)
    # GATES, not `gates`: the report must be able to say what was NOT run.
    n = write_report(rows, GATES, report_path, notes, verdict_lines)
    try:
        shown_report = os.path.relpath(report_path, ROOT)
    except ValueError:
        # Windows: relpath raises when the two paths are on different drives, which is
        # exactly what happens when the selftest writes its throwaway report to %TEMP%.
        shown_report = report_path
    print('')
    print('report: %s (%d lines) -- read this instead of re-running the gates'
          % (shown_report, n))
    print('verdict: %s (%d/%d gate(s) green%s)'
          % ('PASS' if all_green else 'FAIL', len(rows) - len(failed), len(rows),
             '' if not side_problems else '; SIDE-EFFECT GUARD FAILED'))
    # 这行以前写的是「no SQL was executed … remains UNPROVEN」。2026-09-23 起
    # tools/run_sql_smoke.py 已在真实 PostgreSQL 上跑过两套 DDL+smoke，那句话变成假话，
    # 留着就是漂移。现在如实说明：本报告不含运行时结论，且**故意不转述**它的结论，
    # 免得两处各说一套。要引用，就引用 sql-smoke-report.txt 里的实际记录与镜像摘要。
    print('NOTE: these %d gates execute no SQL -- they prove the constraints are still '
          'WRITTEN, not that they REJECT.' % len(rows))
    print('NOTE: the runtime evidence lives in %s (produced by %s).'
          % (os.path.relpath(SMOKE_REPORT_PATH, ROOT), os.path.relpath(SMOKE_TOOL, ROOT)))
    print('      That path is the DEFAULT snapshot; `--report=` writes a sibling without '
          'touching it,')
    print('      which is how the version ladder was recorded (tools/sql-smoke-report-pg1*.txt '
          '-- postgres:14/15/16).')
    print('      A snapshot is evidence only together with the image digest recorded inside '
          'it,')
    print('      and re-running after any db/*.sql edit overwrites it -- so save to a new path.')
    print('NOTE: these %d gates DO read those snapshots, but only to compare the image names ' % len(rows))
    print('      against each DDL\'s PG-VERIFIED-ON stamp (in both directions); they do not '
          're-run SQL.')
    print('NOTE: proof that the runtime green has teeth lives in %s (produced by %s),'
          % (os.path.relpath(FALSIFY_REPORT_PATH, ROOT), os.path.relpath(FALSIFY_TOOL, ROOT)))
    print('      one case per named CHECK, every case must turn exactly one sample red. That'
          ' is the')
    print('      DEFAULT snapshot; the per-image ladder is %s'
          % os.path.relpath(FALSIFY_REPORT_GLOB, ROOT))
    print('      (postgres:14/15/16, same `--report=` convention as the smoke ladder) -- and'
          ' it has to')
    print('      exist per image, because a stamp that names an image the ladder never'
          ' falsified on')
    print('      would otherwise be a claim that the constraints were LOADED there, not shown'
          ' to bite.')
    return 0 if all_green else 1


if __name__ == '__main__':
    sys.exit(main())
