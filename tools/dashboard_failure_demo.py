"""I4 DoD「触发测试」的实测记录器 —— 手工改一份 BacktestResult，确认一致性门禁会红。

DoD 原文两行：

    | 触发测试 | 手工改一份 BacktestResult 里的某个指标，确认一致性门禁会红 | 一次真实的失败记录 | — |

手工做过一次的手工实验，下一次没人能重做，留下的只是一句话。所以这里把那次手工动作
做成**可重跑**的，并且每一步都自 assert：

  ① 真跑一次回测（CLI，`tests/fixtures/sample_prices.csv`，slippage 0，seed 7）
     ⇒ 干净报告 A（控制组）；
  ② 在 A 的**文本**里改掉 `deterministic.performance.<KEY>` 那一个数（这就是"手工改"：
     打开编辑器，改一个数字，存盘）⇒ 报告 B（实验组）。落盘前断言三件事：
     锚点在 A 里**恰好命中一次**且后面跟的是分隔符（否则 `0.5` 是 `0.53` 的前缀，
     `in` 判定恒真）、A 与 B **逐字节不同**、两份 JSON 的**差异路径只有一条**且就是那个键
     —— 任一条不成立即 `exit 2`，绝不在未修改的文件上得出"门禁红了"的结论；
  ③ `tools/verify_dashboard.py --report=A`（控制组，**必须 exit 0**）；
  ④ `tools/verify_dashboard.py --report=B`（实验组，**必须 exit 1**，且 ISSUE 里必须有
     一条 `ISSUE [C10] <KEY>:`，并且那一行要逐字带上改动前的值和改动后的值）
     —— 没红就 `exit 3`：一个改坏了输入仍然绿的检查器没有牙，绿没有意义。

为什么等的是 `C10` 而不是 `C3`（这条注释值一个 bug 的代价）：这个工具的第一版等的是
`C3`，而**它永远等不到**。`C3` 比较的是「看板读出的数」与「同一份 payload 里的字段」
—— 两个数从同一个 dict 里来，手改文件等于把两边一起改掉，于是永远相等。那不是
「漏报」，是**恒真判据**：报告看起来比真通过还干净。`C10` 比的是「文件里存的数」与
「现在重跑 PerformanceAnalyzer 算出来的数」，只有它是拿文件**外面**的东西当参照，
所以只有它能看见「文件被手改了」。

两段输出原文（含退出码）、那条 diff、以及"这证明了什么 / 没证明什么"一起落进
`tools/dashboard-failure-report.txt`。报告头部**不写 commit**（它描述的是工作树内容），
所以它可以和代码同批提交。

只用标准库；输出全 ASCII（本机控制台 cp936，中文之外的字符会让 Python 抛
`UnicodeEncodeError`）。
"""

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, 'tools', 'verify_dashboard.py')
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'sample_prices.csv')
REPORT_PATH = os.path.join(ROOT, 'tools', 'dashboard-failure-report.txt')

# The one metric a human would plausibly retype. `sharpe_ratio` is in the contract's
# SS2.9 example list, so it is a field a reader of the report would trust.
TARGET_KEY = 'sharpe_ratio'
DELIMITERS = (',', '\n', '}')


def child_env():
    env = dict(os.environ)
    # Same as every other tool here: a stale __pycache__ would let a later step read a
    # value that is no longer on disk.
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def run(argv):
    """Run a child with stderr merged into stdout, and report its real exit code.

    Merging matters: a traceback on stderr that is not captured looks exactly like
    "the gate printed nothing", and then the verdict gets read off an empty string.
    """
    proc = subprocess.run([sys.executable, '-X', 'utf8'] + argv, cwd=ROOT,
                          env=child_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def differing_paths(left, right, prefix=()):
    """Every path at which two JSON documents differ (recursing into dicts/lists)."""
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                out.append(prefix + (key,))
            else:
                out.extend(differing_paths(left[key], right[key], prefix + (key,)))
        return out
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        out = []
        for index, (a, b) in enumerate(zip(left, right)):
            out.extend(differing_paths(a, b, prefix + (index,)))
        return out
    return [] if left == right else [prefix]


def main():
    workdir = tempfile.mkdtemp(prefix='dashboard-failure-')
    clean_path = os.path.join(workdir, 'report-clean.json')
    mutated_path = os.path.join(workdir, 'report-mutated.json')
    lines = []
    exit_code = 0
    try:
        # --- step 1: a real report -------------------------------------------------
        rc, out = run(['-m', 'quanauto.cli', 'backtest',
                       '--strategy-csv', FIXTURE, '--slippage-pct', '0', '--seed', '7',
                       '--out', clean_path])
        lines.append('$ python -X utf8 -m quanauto.cli backtest --strategy-csv '
                     'tests/fixtures/sample_prices.csv --slippage-pct 0 --seed 7 '
                     '--out <tmp>/report-clean.json')
        lines.append('EXIT=%d' % rc)
        lines.append(out.rstrip('\n'))
        lines.append('')
        if rc != 0 or not os.path.exists(clean_path):
            lines.append('FATAL: could not produce a clean report (exit %d)' % rc)
            exit_code = 1
            return finish(lines, exit_code)

        with open(clean_path, encoding='utf-8') as fh:
            clean_text = fh.read()
        clean = json.loads(clean_text)

        # --- step 2: the hand edit -------------------------------------------------
        perf = ((clean.get('deterministic') or {}).get('performance') or {})
        if TARGET_KEY not in perf:
            lines.append('FATAL: deterministic.performance has no %r field to edit '
                         '(keys: %s)' % (TARGET_KEY, sorted(perf)))
            exit_code = 2
            return finish(lines, exit_code)
        before = perf[TARGET_KEY]
        after = round(before * 1.37 + 0.011, 6)
        if after == before:
            lines.append('FATAL: the replacement equals the original (%r)' % before)
            exit_code = 2
            return finish(lines, exit_code)

        # The exact literal a human would see and retype in an editor.
        anchor = '"%s": %s' % (TARGET_KEY, json.dumps(before, ensure_ascii=False))
        hits = [i for i in range(len(clean_text)) if clean_text.startswith(anchor, i)]
        if len(hits) != 1:
            lines.append('FATAL: anchor %r occurs %d time(s) in the clean report -- '
                         'exactly one is required' % (anchor, len(hits)))
            exit_code = 2
            return finish(lines, exit_code)
        tail = clean_text[hits[0] + len(anchor)]
        if tail not in DELIMITERS:
            # The prefix trap: without this, editing 0.5 inside 0.53 would pass the
            # "anchor found" assertion while changing the wrong thing.
            lines.append('FATAL: anchor %r is a prefix of a longer token (next char %r)'
                         % (anchor, tail))
            exit_code = 2
            return finish(lines, exit_code)

        mutated_text = (clean_text[:hits[0]] + '"%s": %s'
                        % (TARGET_KEY, json.dumps(after, ensure_ascii=False))
                        + clean_text[hits[0] + len(anchor):])

        # ... and the three assertions that say the edit really landed.
        if mutated_text == clean_text:
            lines.append('FATAL: the mutated file is byte-identical to the clean one '
                         '-- the "failure" below would be measured on an unmodified '
                         'report')
            exit_code = 2
            return finish(lines, exit_code)
        with open(mutated_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(mutated_text)
        with open(mutated_path, encoding='utf-8') as fh:
            on_disk = fh.read()
        if on_disk != mutated_text:
            lines.append('FATAL: what landed on disk differs from what was built in '
                         'memory')
            exit_code = 2
            return finish(lines, exit_code)
        mutated = json.loads(mutated_text)
        paths = differing_paths(clean, mutated)
        if paths != [('deterministic', 'performance', TARGET_KEY)]:
            lines.append('FATAL: expected exactly one differing path '
                         '[deterministic.performance.%s], got %s'
                         % (TARGET_KEY, ['.'.join(map(str, p)) for p in paths]))
            exit_code = 2
            return finish(lines, exit_code)

        lines.append('--- the hand edit -------------------------------------------------')
        lines.append('file: <tmp>/report-clean.json  ->  <tmp>/report-mutated.json')
        lines.append('edit: deterministic.performance.%s  %r -> %r'
                     % (TARGET_KEY, before, after))
        lines.append('applied: anchor %r matched exactly 1 time (line %d)'
                     % (anchor, clean_text.count('\n', 0, hits[0]) + 1))
        lines.append('applied: mutated file differs from the clean file on exactly 1 '
                     'path: deterministic.performance.%s' % TARGET_KEY)
        lines.append('')
        lines.append('--- diff ----------------------------------------------------------')
        lines.extend(difflib.unified_diff(
            clean_text.splitlines(), mutated_text.splitlines(),
            fromfile='report-clean.json', tofile='report-mutated.json', lineterm='',
            n=2))
        lines.append('')

        # --- step 3: the control ---------------------------------------------------
        rc_clean, out_clean = run([GATE, '--report=%s' % clean_path])
        lines.append('$ python -X utf8 tools/verify_dashboard.py '
                     '--report=<tmp>/report-clean.json')
        lines.append('EXIT=%d   (control: the untouched report must stay green)' % rc_clean)
        lines.append(out_clean.rstrip('\n'))
        lines.append('')

        # --- step 4: the experiment ------------------------------------------------
        rc_bad, out_bad = run([GATE, '--report=%s' % mutated_path])
        lines.append('$ python -X utf8 tools/verify_dashboard.py '
                     '--report=<tmp>/report-mutated.json')
        lines.append('EXIT=%d   (experiment: the wrong number must be caught)' % rc_bad)
        lines.append(out_bad.rstrip('\n'))
        lines.append('')

        named = [line for line in out_bad.splitlines()
                 if line.startswith('ISSUE [C10] %s:' % TARGET_KEY)]
        verdict = []
        if rc_clean != 0:
            verdict.append('the control run went RED (exit %d) -- an untouched report '
                           'must be accepted, so the gate is failing for some other '
                           'reason' % rc_clean)
        if rc_bad == 0:
            verdict.append('the mutated report still exited 0 -- this gate has no teeth '
                           'on the failure the DoD asks about')
        if not named:
            verdict.append('no "ISSUE [C10] %s:" line in the red run -- the gate went red '
                           'for something else' % TARGET_KEY)
        else:
            # The line has to carry both numbers, and the right way round: it must say
            # the *edited* value is what the file stores and the *clean* value is what
            # re-running the analyzer yields. (`before` / `after` are named from the
            # point of view of the hand edit; in the gate's message they appear as
            # "stores" / "yields" -- swapping them here would happily accept a gate
            # that mixed the two up.)
            if ('the report stores %r' % after) not in named[0]:
                verdict.append('the C10 line does not quote the edited value %r as what '
                               'the report stores: %s' % (after, named[0]))
            if ('yields %r' % before) not in named[0]:
                verdict.append('the C10 line does not quote the recomputed value %r as '
                               'what the analyzer yields: %s' % (before, named[0]))
        if verdict:
            for item in verdict:
                lines.append('FAILED: ' + item)
            exit_code = 3
        else:
            lines.append('RESULT: PASS')
            lines.append('  control (clean report)      : EXIT=0, 0 issues')
            lines.append('  experiment (one field edited): EXIT=%d, and the issue list '
                         'names C10 and %r' % (rc_bad, TARGET_KEY))
            lines.append('  the C10 line               : %s' % named[0].strip())
            lines.append('')
            lines.append('What this proves: changing one number inside a BacktestResult '
                         'turns this gate red, and the red is C10 -- the check that '
                         're-runs the analyzer over the report\'s own inputs -- '
                         'noticing, not a crash and not a schema error.')
            lines.append('Why C10 and not C3: C3 compares the board read-out with the '
                         'report field, and a hand edit moves both sides at once, so C3 '
                         'stays green (it is structurally blind to this failure). Any '
                         'consistency check whose two sides come from the same file is '
                         'an identity.')
            lines.append('What this does NOT prove: that the rest of the report is '
                         'sane, or that the metric was computed correctly. It is one '
                         'field, one run, one seed.')
    finally:
        # Only what this process created: mkdtemp gives the prefix, and anything else
        # under `%TEMP%` is none of our business.
        if os.path.basename(workdir).startswith('dashboard-failure-'):
            shutil.rmtree(workdir, ignore_errors=True)
    return finish(lines, exit_code)


def finish(lines, exit_code):
    header = [
        'I4 DoD trigger test -- a hand edit of one metric inside a BacktestResult',
        '=' * 78,
        'produced by: python -X utf8 tools/dashboard_failure_demo.py',
        'report shape: quanauto.backtest-report/1 (cli backtest --out)',
        'seed 7 / tests/fixtures/sample_prices.csv / slippage 0 / '
        'python %s' % sys.version.split()[0],
        'this file is not a gate: the gate is tools/verify_dashboard.py.',
        'the head of this file pins no commit -- it describes a working tree.',
        '',
    ]
    write_report(header + lines + ['', 'EXIT=%d' % exit_code])
    print('\n'.join(header + lines))
    print('REPORT=%s' % REPORT_PATH)
    print('EXIT=%d' % exit_code)
    return exit_code


def write_report(lines):
    # newline='\n' explicitly: this console is cp936 and a stray CRLF in a committed
    # snapshot shows up as a whole-file diff.
    with open(REPORT_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.exit(main())
