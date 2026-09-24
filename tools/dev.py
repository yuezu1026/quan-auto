#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev -- 本地开发循环的单一入口（stdlib，无第三方依赖）。

为什么存在这个文件
------------------
正确的验证永远是两条命令：`python -m pytest -q` 和 `python tools/run_all_gates.py`。
在这台机器上，「跑这两条命令并读到结论」要走 PowerShell 5.1，而它有三个坑叠在一起：

  1. 管道下子进程 stdout 默认按 GBK 写 ⇒ 中文结论到手就是乱码（读法问题，不是产物问题）；
  2. `> $null 2>&1` 会把往 stderr 写过东西的脚本判成 NativeCommandError，
     **吞掉同一行里后续的所有语句**（退出码那行根本不打印）；
  3. 退出码必须单独 `Write-Host "EXIT=$LASTEXITCODE"` 才看得见。

于是「一次验证」实际要 3~4 个回合：起命令 → 落盘 → 读文件 → 读退出码。而每个回合都会
重发整个对话上下文 —— **那才是真正的成本**，机器侧根本不吃紧（实测：pytest 1.0s，
全量门禁 3.5s）。这个脚本把「跑」和「读」压进一次调用。

本脚本**故意不做**这些事
------------------------
  * **不缓存任何 verdict。** 缓存判定 = 假绿工厂，跟「提取为空必须判 FAIL」是同一条
    理由。这里每次都真的把 pytest 和门禁跑一遍。
  * **不新增判据，也不复制门禁注册表。** 它是 runner 不是 gate；权威判据永远是
    `tools/run_all_gates.py` 的退出码。**注册表只有一份**（在 run_all_gates.py 里）——
    让一个工具去解析另一个工具的注册表，本项目规范 §3.6 已经把这类「门禁解析门禁」
    列为漂移源，所以这里不建第二份表，连「按改动路由门禁」也不做：全量快扫只要 3.5s，
    而路由判错会变成假绿（少跑了却报绿），省下的那点输出不值这个风险。
  * **不在「只跑了一部分」时报 PASS。** 单跑门禁时 `run_all_gates.py` 自己会把报告首行
    写成 `SCOPE: PARTIAL`，本脚本原样转述，不改写。
  * **自己打印的字一律 ASCII。** cp936 控制台会把非 ASCII 变成乱码，所以中文细节留在
    `tools/gates-report.txt`（UTF-8）里由人来读 —— 「看结果读报告，不要为看结果重跑」
    本来就是这个仓库的规矩。本脚本只告诉你「红在哪一条」，不转述中文正文。

用法
----
    python tools/dev.py check              # pytest + 全部 门禁，压成几行
    python tools/dev.py full               # 同上，但附上每条门禁的原始输出
    python tools/dev.py gate NAME [NAME…]  # 单跑若干门禁（走 --gate=NAME）
    python tools/dev.py test [pytest …]    # 只跑 pytest，参数透传
    python tools/dev.py status             # 只读：git 状态 + 棘轮基线 + 上次报告首行
    python tools/dev.py --selftest         # 证明本 runner 真的能报 FAIL

必须用 `.venv\\Scripts\\python.exe` 运行（裸 `python` 是 anaconda base，没装 pytest）。

退出码：0 = 本次请求全绿；1 = 有失败（含「解析不到 verdict」这种提取为空的情形）；
2 = 本脚本自身用法/自测错。
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(ROOT, 'tools', 'gates-report.txt')
BASELINE_PATH = os.path.join(ROOT, 'tools', 'gates-baseline.json')
HARNESS = 'tools/run_all_gates.py'

PYTEST_ARGV = ['-m', 'pytest', '-q']

# ---------------------------------------------------------------------------------
# 与 run_all_gates.py 保持**同一组正则**。解析别的门禁的输出本身就是一份契约：
# harness 曾把 verdict 的计数写法硬编码成 `(N issue(s))`，而 md-fidelity 打印的是
# `(total missing=0)` ⇒ 假 FAIL。所以这里两种形状都收，宁可比对宽松，也不做只看一种的
# 假设。取不到 verdict 一律判 FAIL（提取为空不得算通过）。
# ---------------------------------------------------------------------------------
FINDING_RE = re.compile(r'^(?:FINDING|ISSUE) \[([A-Z0-9_]+)\]', re.M)
VERDICT_RE = re.compile(r'^verdict:\s*(\w+)', re.M)

# 全绿时只回显这几类行（表头/结论/棘轮），其余原始输出丢掉；红了就全打。
KEEP_PREFIXES = ('ran ', 'verdict:', 'RATCHET', 'SCOPE:', 'TOTAL', 'GATE FAIL')


def run(argv, timeout=900):
    """跑一个 Python 子进程，返回 (rc, 合并后的 stdout+stderr 文本)。

    stderr 合并进 stdout：门禁的结论可能走任一条流（psql 的 NOTICE 就走 stderr），
    分开读会让「结论不见了」变成一个查不出的问题。
    """
    return run_raw([sys.executable, '-X', 'utf8'] + list(argv), timeout=timeout)


def run_raw(argv, timeout=900):
    """跑任意可执行文件（不带 Python 前缀）。

    区分这两个入口是必须的：一个「总是拼上 sys.executable」的 runner 会把
    `git rev-parse` 送到 Python 解释器那里，报错信息长得像「git 不存在」——
    本文件的 selftest 第一次跑就把这个 bug 抓出来了。
    """
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    cmd = list(argv)
    try:
        proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT after %ds: %s' % (timeout, ' '.join(cmd))
    except OSError as exc:
        return 126, 'could not launch %s: %s' % (' '.join(cmd), exc)
    return proc.returncode, (proc.stdout or b'').decode('utf-8', 'replace')


def last_line(text):
    """最后一行非空输出 —— pytest 的结论就在那里，前面全是进度点。"""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return '(no output)'


def keep_lines(text, prefixes=KEEP_PREFIXES):
    """只留前缀命中的行，压掉全绿时的噪声。"""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith(prefixes):
            out.append(line.rstrip())
    return out


def parse_verdict(text):
    """从门禁输出里取总判据。

    找不到 verdict 行**不是**「没问题」，而是「没读懂」⇒ ok=False。这条守卫本身就值得
    存在：一个静默失配的正则会让下面所有判断都落在空集上，然后打印出比成功还干净的
    结论。返回的 dict 里 `ok` 只表示「提取到且不是非 PASS 的措辞」，真实权威仍是 rc。
    """
    match = VERDICT_RE.search(text)
    codes = FINDING_RE.findall(text)
    if match is None:
        return {'word': None, 'line': None, 'codes': codes, 'found': False}
    word = match.group(1)
    line = None
    for candidate in text.splitlines():
        if candidate.lstrip().startswith('verdict:'):
            line = candidate.strip()
            break
    return {'word': word, 'line': line, 'codes': codes, 'found': True}


def report_scope():
    """读上次报告的 SCOPE 行（只读，不重跑）。"""
    try:
        with open(REPORT_PATH, encoding='utf-8-sig') as handle:
            for line in handle:
                if line.startswith('SCOPE:'):
                    return line.strip()
    except OSError:
        return 'SCOPE: (no report yet)'
    return 'SCOPE: (no SCOPE line)'


def print_header(title):
    print('=' * 68)
    print('dev %s' % title)
    print('=' * 68)


def hint_if_pytest_missing(text):
    if 'No module named pytest' in text:
        print('HINT: this interpreter has no pytest. Use its venv:')
        print('      .\\.venv\\Scripts\\python.exe tools\\dev.py %s'
              % ' '.join(sys.argv[1:]))


# ---------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------
def cmd_check(full):
    print_header('check' + (' --full' if full else ''))
    rc_pytest, out_pytest = run(PYTEST_ARGV)
    print('pytest_rc   %d' % rc_pytest)
    print('pytest      %s' % last_line(out_pytest).encode(
        'ascii', 'replace').decode('ascii'))
    hint_if_pytest_missing(out_pytest)

    argv = [HARNESS] + (['--full'] if full else [])
    rc_gates, out_gates = run(argv)
    parsed = parse_verdict(out_gates)
    print('gates_rc    %d' % rc_gates)
    print('gates_scope %s' % report_scope())

    if rc_gates == 0 and not full:
        for line in keep_lines(out_gates):
            print('  ' + line.encode('ascii', 'replace').decode('ascii'))
    else:
        # 红了就别再压缩：整段原始输出打出来（中文可能被控制台糟蹋，正式读法是
        # tools/gates-report.txt，UTF-8）。
        print('--- harness output (verbatim; if it looks broken, read '
              'tools/gates-report.txt as UTF-8) ---')
        print(out_gates.rstrip())

    print('-' * 68)
    if not parsed['found']:
        print('verdict: FAIL -- no verdict line in harness output '
              '(extraction empty is FAIL, not PASS)')
        return 1
    failed = []
    if rc_pytest != 0:
        failed.append('pytest')
    if rc_gates != 0:
        failed.append('gates')
    if failed:
        print('verdict: FAIL (%s)  issues=%d'
              % ('+'.join(failed), len(parsed['codes'])))
        return 1
    print('verdict: PASS')
    return 0


def cmd_gate(names):
    """单跑若干门禁。空参数是用法错（退 2），不是「跑了 0 条 = 通过」。"""
    if not names:
        print('usage: python tools/dev.py gate NAME [NAME ...]')
        return 2
    rc_all = 0
    for name in names:
        rc, out = run([HARNESS, '--gate=' + name])
        parsed = parse_verdict(out)
        status = 'PASS' if (rc == 0 and parsed['found']) else 'FAIL'
        print('gate %-26s rc=%d  %s' % (name, rc, status))
        # 「on-disk」而不是断言「本轮的」：门禁名写错时 harness 直接退非 0、根本没重写报告，
        # 这时磁盘上的 SCOPE 属于上一次运行。据实标注，别让一行陈旧作用域冒充当轮产物。
        print('  on-disk %s' % report_scope())
        if status == 'FAIL' or not parsed['found']:
            rc_all = 1
            print(out.rstrip())
        else:
            for line in keep_lines(out):
                print('  ' + line.encode('ascii', 'replace').decode('ascii'))
    if rc_all == 0:
        print('NOTE: tools/gates-report.txt now covers ONLY the gate(s) above. '
              'Run `dev.py check` before trusting a partial report as full.')
    return rc_all


def cmd_test(argv):
    rc, out = run(PYTEST_ARGV + list(argv))
    print('pytest_rc %d' % rc)
    print(out.rstrip())
    hint_if_pytest_missing(out)
    return 0 if rc == 0 else 1


def cmd_status():
    print_header('status')
    rc, out = run_raw(['git', 'status', '--porcelain'])
    rows = [line for line in out.splitlines() if line.strip()]
    print('git          rc=%d  dirty=%d' % (rc, len(rows)))
    for line in rows:
        print('  ' + line.encode('ascii', 'replace').decode('ascii'))
    rc, out = run_raw(['git', 'log', '-1', '--oneline'])
    print('head         %s' % (out.strip() or '(none)'))
    if os.path.exists(BASELINE_PATH):
        try:
            with open(BASELINE_PATH, encoding='utf-8-sig') as handle:
                data = json.load(handle)
            print('baseline     total=%s detail=%s'
                  % (data.get('total'), data.get('detail')))
        except (OSError, ValueError) as exc:
            print('baseline     unreadable: %s' % exc)
    print('report       %s' % report_scope())
    print('report_path  tools/gates-report.txt (UTF-8; read it, do not re-run '
          'just to look)')
    return 0


# ---------------------------------------------------------------------------------
# selftest —— 三类样本：命中第 1 项的、命中第 2..N 项的、期望 0 报错的
# ---------------------------------------------------------------------------------
CLEAN_HARNESS_OUT = """\
SCOPE: FULL
  ran md-fidelity          selftest=PASS   verdict=PASS       (0.1s)
  ran risk-config          selftest=PASS   verdict=PASS       (0.1s)
verdict: PASS (9/9 gate(s) green)
RATCHET 39/39
"""

RED_HARNESS_OUT = """\
SCOPE: FULL
  ran data-center-pit      selftest=PASS   verdict=FAIL       (0.1s)
ISSUE [P2] feed 类必须自己带 as_of
ISSUE [P7] window 必须按 session 过滤
verdict: FAIL (2 issue(s))
"""


def selftest():
    failures = []
    stats = {'checks': 0}

    def check(tag, cond, detail=''):
        stats['checks'] += 1
        print('  [%s] %s%s' % (tag, 'OK' if cond else 'FAIL',
                               ('  ' + detail) if detail else ''))
        if not cond:
            failures.append(tag)

    print('== dev.py selftest (clean / red / empty / control / real-subprocess) ==')

    # 样本 1：命中「提取成功 + 全绿」这一支
    parsed = parse_verdict(CLEAN_HARNESS_OUT)
    check('CLEAN-verdict-parsed',
          parsed['found'] and parsed['word'] == 'PASS' and not parsed['codes'],
          'word=%s codes=%d' % (parsed['word'], len(parsed['codes'])))
    check('CLEAN-verdict-line-verbatim',
          parsed['line'] == 'verdict: PASS (9/9 gate(s) green)', parsed['line'])
    check('CLEAN-keep-lines',
          any('ran md-fidelity' in line for line in keep_lines(CLEAN_HARNESS_OUT)),
          'kept=%d' % len(keep_lines(CLEAN_HARNESS_OUT)))

    # 样本 2：命中「判红」这一支（verdict 是 FAIL，且带 ISSUE 码）
    parsed = parse_verdict(RED_HARNESS_OUT)
    check('RED-verdict-parsed',
          parsed['found'] and parsed['word'] == 'FAIL', 'word=%s' % parsed['word'])
    check('RED-issue-codes',
          parsed['codes'] == ['P2', 'P7'], 'codes=%s' % parsed['codes'])
    check('RED-last-line',
          last_line(RED_HARNESS_OUT) == 'verdict: FAIL (2 issue(s))',
          last_line(RED_HARNESS_OUT))

    # 样本 3（空转守卫）：提取为空必须判 FAIL，不能当 PASS
    for tag, blob in (('EMPTY-no-output', ''),
                      ('EMPTY-garbage', 'not a gate report at all\n')):
        parsed = parse_verdict(blob)
        check(tag, not parsed['found'], 'found=%s' % parsed['found'])
        check(tag + '-not-pass', parsed['word'] is None, 'word=%s' % parsed['word'])

    # 样本 4（控制组）：棘轮行不该被当成 verdict 的一部分而错判
    parsed = parse_verdict(CLEAN_HARNESS_OUT + 'verdict: RATCHET (39/39)\n')
    check('CONTROL-two-verdicts-takes-first', parsed['word'] == 'PASS',
          'word=%s' % parsed['word'])

    # 样本 5（干净样本）：真跑一次 --list，必须退出 0 且输出是 ASCII 之外的？
    # 不 —— 这里只证明「runner 能把一个真实子进程的 rc 与输出带回来」。
    rc, out = run([HARNESS, '--list'])
    check('REAL-harness-list-rc', rc == 0, 'rc=%d' % rc)
    check('REAL-harness-list-nonempty', bool(out.strip()),
          'bytes=%d' % len(out))
    rc, out = run_raw(['git', 'rev-parse', '--is-inside-work-tree'])
    check('REAL-git-rc', rc == 0 and out.strip() == 'true', out.strip())

    print('-' * 68)
    if failures:
        print('SELFTEST FAIL: %d/%d checks failed: %s'
              % (len(failures), stats['checks'], ', '.join(failures)))
        return 1
    print('SELFTEST OK: %d/%d checks; parse takes the first verdict, empty '
          'extraction is FAIL, and the runner really talks to a subprocess'
          % (stats['checks'], stats['checks']))
    return 0


USAGE = """\
dev -- local dev loop, one command (stdlib only).

  python tools/dev.py check              pytest + every gate, compact
  python tools/dev.py full               same, with each gate's raw output
  python tools/dev.py gate NAME [NAME]   run one or more gates
  python tools/dev.py test [pytest ...]  pytest only (args passed through)
  python tools/dev.py status             read-only: git + ratchet baseline + report head
  python tools/dev.py --selftest         prove this runner can report FAIL

Use .venv\\Scripts\\python.exe. This script prints ASCII on purpose; the Chinese
detail lives in tools/gates-report.txt (UTF-8) -- read it, do not re-run to look.
"""


def main(argv):
    if not argv:
        print(USAGE)
        return 2
    head, rest = argv[0], argv[1:]
    if head in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    if head == '--selftest':
        return selftest()
    if head == 'check':
        if rest:
            print('check takes no arguments (got %s)' % ' '.join(rest))
            return 2
        return cmd_check(full=False)
    if head == 'full':
        return cmd_check(full=True)
    if head == 'gate':
        return cmd_gate(rest)
    if head == 'test':
        return cmd_test(rest)
    if head == 'status':
        return cmd_status()
    print('unknown command: %s' % head)
    print(USAGE)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
