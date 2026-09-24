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
          parse). Measured 2026-09-23 against the real artifacts: total 39
          (T1=2 unparsable blocks, T2=19 undefined types, T3=18 undefined exceptions)
          once both supplementary contracts are supplied via --extra=. A tier-B gate
          does NOT need a clean exit; its finding count must EQUAL the baseline in
          tools/gates-baseline.json. The ratchet is deliberately symmetric:
            actual == baseline  -> PASS   (debt unchanged)
            actual >  baseline  -> FAIL   (drift: new debt appeared)
            actual <  baseline  -> FAIL   (debt shrank: the baseline must be lowered by
                                          hand, otherwise the improvement is silently
                                          absorbed and the number stops meaning anything)

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
to go red (currently 31 cases / 30 constraints / 31 CAUGHT), writing
tools/falsify-report.txt. It is deliberately NOT registered as a gate here --
on a machine without a running daemon it would either fail for environmental reasons or
be silently skipped and reported green, which is the fake-gate pattern this project keeps
getting bitten by. Integrating it properly means "no docker => explicit SKIPPED, counted
as not-green", and that is a separate piece of work.

Usage:
  python tools/run_all_gates.py                 # run everything, print the table
  python tools/run_all_gates.py --full          # also dump each gate's raw output
  python tools/run_all_gates.py --gate=NAME     # run one gate
  python tools/run_all_gates.py --list          # show the registry
  python tools/run_all_gates.py --selftest      # prove this harness can report FAIL

Exit codes: 0 = all gates green, 1 = at least one gate failed or the harness itself
could not run its checks, 2 = the harness' own self-test failed.
"""
import contextlib
import io
import json
import os
import re
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
SMOKE_TOOL = os.path.join(ROOT, 'tools', 'run_sql_smoke.py')
SMOKE_REPORT_PATH = os.path.join(ROOT, 'tools', 'sql-smoke-report.txt')
FALSIFY_TOOL = os.path.join(ROOT, 'tools', 'falsify_smoke.py')
FALSIFY_REPORT_PATH = os.path.join(ROOT, 'tools', 'falsify-report.txt')

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
        'name': 'core-contract-refs',
        'tier': 'B',
        # The two supplements contribute definitions only. DataFeed/MarketStatus/
        # DataFeedError/PITReport/LeakagePoint/ValidationReport live in the data-center
        # contract; omitting it reports them as holes in the core contract, which is a
        # false positive that inflates the baseline and hides real drift.
        'runner': ['tools/verify_contract_refs.py',
                   '--extra=' + DC_CONTRACT,
                   '--extra=' + RISK_CONTRACT],
        'selftest': ['tools/verify_contract_refs.py', '--selftest'],
        'what': '主契约引用的类型/异常是否都有定义（B7 欠账，受棘轮约束）',
    },
]

FINDING_RE = re.compile(r'^(?:FINDING|ISSUE) \[([A-Z0-9_]+)\]', re.M)
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

    if tier == 'A':
        # The exit code is authoritative, but a missing verdict line is a separate
        # defect: the script ran to completion without saying what it concluded.
        if pk['verdict'] is None:
            problems.append('no "verdict:" line in the output')
        elif real['rc'] != 0:
            problems.append('exit=%s with verdict %s (%d finding(s))'
                            % (real['rc'], pk['verdict'], pk['total']))
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


def write_report(rows, registry, report_path=REPORT_PATH):
    """`registry` must be the WHOLE gate list, never the --gate= filtered subset.

    scope_line() answers 'which registered gates are missing from rows?', so the
    comparison set has to be the registry. Handed the subset instead, the comparison
    shrinks with the selection and a one-gate run stamps 'SCOPE: FULL -- all 1
    registered gate(s) ran.' on its own artifact -- the exact false green the scope line
    exists to prevent. The parameter is named `registry` (not `gates_by_name`) on
    purpose: the old name invited passing a mapping built from the filtered list.
    """
    gates_by_name = {g['name']: g for g in registry}
    lines = []
    lines.append(scope_line(rows, list(gates_by_name)))
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
    with open(report_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))
    return len(lines)


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
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            execute([picked_one], report_path=tmp_report)
        with open(tmp_report, encoding='utf-8-sig') as f:
            first = f.readline().strip()
    except (OSError, IndexError) as exc:
        first = '(unreadable: %s)' % exc
    hit = (len(GATES) >= 2 and first.startswith('SCOPE: PARTIAL')
           and ('1 of %d gate(s) ran' % len(GATES)) in first and not_run[0] in first)
    print('  [scope-line-wired-e2e] picked=%s first=%s %s'
          % (picked_one, first[:40], 'OK' if hit else 'MISSED'))
    ok = ok and hit
    try:
        os.remove(tmp_report)
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

    gates_what_short.update({g['name']: g['what'][:44] for g in gates})
    rows = []
    for g in gates:
        st = run_argv(g['selftest'])
        real = run_argv(g['runner'])
        row = evaluate(g, st, real, baseline)
        rows.append(row)
        print('  ran %-20s selftest=%-6s verdict=%-10s (%.1fs)'
              % (g['name'], row['selftest'], row['verdict'], row['seconds']))

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

    # GATES, not `gates`: the report must be able to say what was NOT run.
    n = write_report(rows, GATES, report_path)
    try:
        shown_report = os.path.relpath(report_path, ROOT)
    except ValueError:
        # Windows: relpath raises when the two paths are on different drives, which is
        # exactly what happens when the selftest writes its throwaway report to %TEMP%.
        shown_report = report_path
    print('')
    print('report: %s (%d lines) -- read this instead of re-running the gates'
          % (shown_report, n))
    print('verdict: %s (%d/%d gate(s) green)'
          % ('PASS' if not failed else 'FAIL', len(rows) - len(failed), len(rows)))
    # 这行以前写的是「no SQL was executed … remains UNPROVEN」。2026-09-23 起
    # tools/run_sql_smoke.py 已在真实 PostgreSQL 上跑过两套 DDL+smoke，那句话变成假话，
    # 留着就是漂移。现在如实说明：本报告不含运行时结论，且**故意不转述**它的结论，
    # 免得两处各说一套。要引用，就引用 sql-smoke-report.txt 里的实际记录与镜像摘要。
    print('NOTE: these %d gates execute no SQL -- they prove the constraints are still '
          'WRITTEN, not that they REJECT.' % len(rows))
    print('NOTE: the runtime evidence lives in %s (produced by %s).'
          % (os.path.relpath(SMOKE_REPORT_PATH, ROOT), os.path.relpath(SMOKE_TOOL, ROOT)))
    print('      That file is a SNAPSHOT: read its verdict together with the image '
          'digest recorded inside it, and re-run it after any db/*.sql edit.')
    print('NOTE: proof that the runtime green has teeth lives in %s (produced by %s).'
          % (os.path.relpath(FALSIFY_REPORT_PATH, ROOT), os.path.relpath(FALSIFY_TOOL, ROOT)))
    print('      One case per named CHECK; every case must turn exactly one sample red.')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
