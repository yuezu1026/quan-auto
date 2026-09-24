#!/usr/bin/env python
"""ci_dryrun -- 在本地把 CI 的两个 `run:` 命令真跑一遍，并记录**真实退出码**。

Why this tool exists
--------------------
I0 §四 的「触发测试」一行要求一次真实的红→绿往返记录。`tools/verify_skeleton.py`
只证明 CI 里**写着**这两条命令，不证明它们会退出 0 —— 那是另一件事，需要真解释器。
这个工具补的就是那一格，产物 `tools/ci-dryrun-report.txt`。

它不是 CI
---------
本仓库**没有配置 git remote**（`git remote -v` 为空），所以 `.github/workflows/ci.yml`
从未在 GitHub 上真实运行过。本工具跑的是**同样的两条命令**，因此只能标成
「本地等价」，不得读成「CI 已跑通」。报告里连这个措辞一起写进去，避免以后有人
把这份快照当成 CI 的绿色。

它为什么不能是门禁
------------------
它需要一个装了 pytest 的解释器（`./.venv`）。把这种东西注册进
`tools/run_all_gates.py`，在没有 `.venv` 的机器上就会产出环境性 FAIL，或者被静默
跳过而报绿 —— 后者正是本仓库反复踩的假门禁。所以它和 SQL 冒烟一样：**快照通道**，
结论只在报告内记录的「解释器 + commit」上成立，改完代码必须重跑。

红→绿往返怎么做才不是自欺
--------------------------
「跑一次绿」证明不了这套命令会红。所以这里做一次**真实往返**：
  1. 按**字节**替换靶文件里的一处内容（靶子见 `ROUNDTRIPS`），行尾与 BOM 原样不动；
  2. 断言替换**确实发生了**（否则 exit 2）—— 变异没生效时「全绿」是在**没改过的
     文件**上得出的结论，本仓库踩过这个坑；
  3. 再断言**解释器读回的值真的变了**（`probe` 一栏）。字节变了而解释器读不到新值，
     说明变异没到达被测模块（陈旧字节码 / import 解析到别的副本 / cwd 不对）。
     2026-09-24 实测到一次这种**假红**：变异已落盘，pytest 仍报 75 passed。只比字节
     的话，那一次会被静静记成「绿」，所以读回现在是硬判据；
  4. 跑目标命令，要求退出码 != 0（红）；
  5. 还原，并断言与原始内容**逐字节**一致、且解释器读回值回到基线；
  6. 再跑，要求退出码 == 0（绿）。

`--selftest` 是对第 2/3 步的**触发测试**：真变异（期望放行）、字节变了但解释器读回
不变（期望抓住 —— 就是上面那次假红）、靶字符串失配（期望 exit 2）三个样本。

Usage
-----
    python tools/ci_dryrun.py                # 跑全部（含红→绿往返）
    python tools/ci_dryrun.py --no-roundtrip # 只跑绿，跳过往返（快）
    python tools/ci_dryrun.py --selftest     # 只跑触发测试（不碰仓库文件）

Exit codes: 0 = 全部符合预期, 1 = 有一步不符预期, 2 = 工具自身坏了（如变异没应用）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(ROOT, 'tools', 'ci-dryrun-report.txt')

# 往返用的靶子。两条都必须是**真正能证伪**的变异 ——
# 第一版把 HEAVY_MODULES 里的 "pandas" 改成 "pandaz"，结果 pytest 照样全绿：
# 它只是把探针列表改了个名，根本没有破掉任何断言。工具当场拒收了这个结果
# （「变异后 pytest 仍然退出 0 —— 这套命令不会红，绿没有意义」）。
# 教训：变异必须打在**断言真的会看的地方**，而不是离它最近的一串字符上。
ROUNDTRIPS = [
    {
        # 1) 改包版本 -> tests/test_skeleton.py 的版本一致断言必须红
        'tag': 'pkg-version-drift',
        'path': os.path.join('quanauto', '__init__.py'),
        'frm': '__version__ = "0.1.0"',
        'to': '__version__ = "0.1.1"',
        'cmd': 'pytest',
        # 解释器层面的读回：只有它变了，才说明变异真的到达了 pytest 看到的那个模块。
        'probe': 'import quanauto; print(quanauto.__version__)',
        'what': '把包版本改成 0.1.1（与 pyproject 不一致）',
    },
    {
        # 2) 把 CI 里的门禁命令换成别的 -> skeleton 门禁必须红
        'tag': 'ci-drops-gates',
        'path': os.path.join('.github', 'workflows', 'ci.yml'),
        'frm': 'python tools/run_all_gates.py',
        'to': 'python tools/verify_risk_config.py',
        'cmd': 'gates',
        'what': '把 CI 里那一行门禁命令换成单跑一个子门禁',
    },
]


def find_python():
    """优先用仓库自带的 .venv；没有就退回当前解释器并如实标注。"""
    for rel in (os.path.join('.venv', 'Scripts', 'python.exe'),
                os.path.join('.venv', 'bin', 'python')):
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            return p, rel.replace('\\', '/')
    return sys.executable, '(当前解释器：未发现 .venv)'


def run(cmd, cwd=ROOT, env=None):
    merged = dict(os.environ)
    # 不写 .pyc：往返里同秒写回、且变异前后文件大小相同，Python 会认为旧字节码
    # 仍然有效 —— 实测过：文件已还原、字节一致，pytest 依旧红，就是陈旧 .pyc 干的。
    merged['PYTHONDONTWRITEBYTECODE'] = '1'
    # 子进程的 stdout 在管道里默认按 locale(cp936) 编码，中文断言消息落到报告里会变乱码。
    # 快照要给人读，这里统一按 UTF-8 出。
    merged['PYTHONIOENCODING'] = 'utf-8'
    if env:
        merged.update(env)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', env=merged)
    return proc.returncode, (proc.stdout or ''), (proc.stderr or '')


def purge_pycache(root):
    """删掉仓库内的 __pycache__（跳过 .venv），返回删掉的目录数。

    这是「还原后仍然红」的真正原因：变异写入与还原发生在同一秒、且两个版本
    字节数相同，于是 .pyc 的 (mtime, size) 双双命中缓存，Python 根本重没读源文件。
    不清理缓存的话，绿/红都不一定能代表磁盘上的源码。
    """
    removed = 0
    for dirpath, dirnames, _ in os.walk(root):
        if os.path.basename(dirpath) == '.venv':
            dirnames[:] = []
            continue
        if '__pycache__' in dirnames:
            dirnames.remove('__pycache__')
            shutil.rmtree(os.path.join(dirpath, '__pycache__'), ignore_errors=True)
            removed += 1
    return removed


def read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def write_bytes(path, data):
    with open(path, 'wb') as fh:
        fh.write(data)


def probe_value(py, snippet, cwd):
    """跑一次性的 `python -c`，返回 (rc, 读回值)。

    刻意连退出码一起返回：探针自己崩了（ImportError 等）时读回值不可信，不能拿
    「崩之前那点输出」去跟基线比 —— 那会变成一个更隐蔽的假绿。
    """
    rc, out, err = run([py, '-c', snippet], cwd=cwd)
    if rc == 0:
        return rc, out.strip()
    return rc, (tail(err, 1)[0] if err.strip() else '(no stderr)')


# apply_mutation 的失败原因 -> 给人看的一句话。键就是返回的 status。
MUTATION_TROUBLE = {
    'missing': lambda i: '找不了 %s' % i['rel'],
    'bad-anchor': lambda i: ('靶字符串在 %s 里出现 %d 次（需要恰好 1 次）；'
                             '替换会静默 no-op 或打错地方' % (i['rel'], i['count'])),
    'noop': lambda i: '替换没产生任何变化（变异未应用）',
    'write-mismatch': lambda i: '变异写入后读回不一致',
    'probe-broken': lambda i: '探针自身就没跑成功（rc != 0），读回值不可信',
    'not-reached': lambda i: ('解释器读回的值在变异前后一模一样（%s）—— 变异没到达'
                              '解释器：陈旧字节码 / import 解析到别的副本 / cwd 不对。'
                              '此时「全绿」是在**没改过的模块**上得出的' % i['base']),
}


def apply_mutation(spec, py, say, root=None):
    """把变异写进靶文件，并在解释器层面读回。返回 (status, info)。

    'ok' 时**文件是变异后的、没有还原** —— 红跑必须发生在变异落盘之后，还原是调用方
    的事（`roundtrip` 在 finally 里做）。其余状态一律已经把文件还原干净。
    """
    root = ROOT if root is None else root
    target = os.path.join(root, spec['path'])
    info = {'path': target, 'rel': spec['path'].replace('\\', '/'), 'raw': None,
            'mutated': None, 'base': None, 'mut': None, 'count': 0}
    if not os.path.isfile(target):
        return 'missing', info

    raw = read_bytes(target)
    info['raw'] = raw
    # 按**字节**替换：先 decode 成 str 再写回会把 CRLF 洗成 LF、把 BOM 弄丢，
    # 于是「还原」那一步实际上是在改行尾，报告却还写着「字节一致」。
    frm = spec['frm'].encode('utf-8')
    to = spec['to'].encode('utf-8')
    info['count'] = raw.count(frm)
    if info['count'] != 1:
        return 'bad-anchor', info

    mutated = raw.replace(frm, to)
    info['mutated'] = mutated
    if mutated == raw:
        return 'noop', info

    snippet = spec.get('probe')
    if snippet:
        rc, info['base'] = probe_value(py, snippet, root)
        if rc != 0:
            return 'probe-broken', info

    write_bytes(target, mutated)
    if read_bytes(target) != mutated:
        write_bytes(target, raw)
        return 'write-mismatch', info
    purge_pycache(root)

    if snippet:
        rc, info['mut'] = probe_value(py, snippet, root)
        if rc == 0 and info['mut'] != info['base']:
            return 'ok', info
        write_bytes(target, raw)
        purge_pycache(root)
        return ('probe-broken' if rc != 0 else 'not-reached'), info
    return 'ok', info


def tail(text, n=12):
    lines = [ln for ln in text.split('\n') if ln.strip()]
    return lines[-n:]


def roundtrip(spec, py, say, failures, root=None):
    """对 spec 描述的一处变异做一次红→绿往返；任一步不符预期就记入 failures。

    返回 2 表示工具自身坏了（靶子不在、变异未应用、变异没到达解释器、还原不回去），
    调用方必须直接停 —— 这时候「绿」不构成任何证据。
    """
    root = ROOT if root is None else root
    if spec.get('cmd') == 'raw':
        cmd = [py] + list(spec['argv'])
        label = spec.get('label', 'raw')
    elif spec['cmd'] == 'pytest':
        cmd = [py, '-m', 'pytest', '-q']
        label = 'pytest'
    else:
        cmd = [py, '-X', 'utf8', os.path.join('tools', 'verify_skeleton.py')]
        label = 'verify_skeleton'

    say('### %s -- %s' % (spec['tag'], spec['what']))
    say('  靶子: %s  %r -> %r'
        % (spec['path'].replace('\\', '/'), spec['frm'], spec['to']))

    status, info = apply_mutation(spec, py, say, root)
    if status != 'ok':
        say('exit: 2 -- %s' % MUTATION_TROUBLE[status](info))
        return 2
    rel = info['rel']
    say('  applied: True  (字节已变)')
    if info['base'] is not None:
        say('  解释器读回: 变异前 %s -> 变异后 %s  （一样就说明变异没到达解释器）'
            % (info['base'], info['mut']))

    try:
        rc_red, out_red, err_red = run(cmd, cwd=root)
    finally:
        write_bytes(info['path'], info['raw'])
        purge_pycache(root)
    restored = read_bytes(info['path']) == info['raw']
    say('  红: %s exit=%d' % (label, rc_red))
    for ln in tail(out_red + err_red, 3):
        say('    | ' + ln)
    say('  还原: %s' % ('与原始字节逐字节一致' if restored else '不一致！'))

    if not restored:
        failures.append('%s: %s 还原不回去，仓库已被改脏' % (spec['tag'], rel))
        return 0
    if info['base'] is not None:
        rc_probe, rest = probe_value(py, spec['probe'], root)
        say('  解释器读回: 还原后 %s' % rest)
        if rc_probe != 0 or rest != info['base']:
            failures.append('%s: 还原后解释器读到的仍是变异值（%s，基线 %s）—— '
                            '接下来的绿不作数' % (spec['tag'], rest, info['base']))
    if rc_red == 0:
        failures.append('%s: %s 在该变异下仍然退出 0 —— 这一步不会红，绿没有意义'
                        % (spec['tag'], label))
    elif rc_red == 2:
        failures.append('%s: %s 退出 2（自检/收集错误），红得不干净' % (spec['tag'], label))

    rc_green, out_green, err_green = run(cmd, cwd=root)
    say('  绿: %s exit=%d' % (label, rc_green))
    for ln in tail(out_green + err_green, 3):
        say('    | ' + ln)
    if rc_green != 0:
        failures.append('%s: 还原后 %s 退出 %d，期望 0' % (spec['tag'], label, rc_green))
    say()
    return 0


# --selftest 的样本：(tag, probe, 期望 roundtrip 返回码)。
# 第二个样本是**故意做绝**的：变异确实写进了文件（字节已变），但探针读的是个常量，
# 于是「解释器读回不变」—— 如果没这道判据，它会被静静记成一次「成功的红→绿」。
SELFTEST_CASES = (
    ('A-真变异', 'import scratchpkg; print(scratchpkg.VALUE)', 0),
    ('B-字节变了但解释器读回不变', 'print("CONST")', 2),
    ('C-靶字符串失配', 'import scratchpkg; print(scratchpkg.VALUE)', 2),
)


def selftest(py):
    """新判据的**触发测试**：不碰仓库文件，靶子建在临时目录里。

    `cmd='raw'` 把 pytest 换成一个假的检查器 —— 真正的往返驱动（`roundtrip` / 
    `apply_mutation`）一行不改地跑，所以这里测的是调用点，而不是另一个纯函数。
    样本三类：真变异（期望 0 push）、假绿（字节变了、解释器读回不变，期望挡住）、
    靶字符串失配（期望 exit 2）。
    """
    scratch = tempfile.mkdtemp(prefix='ci-dryrun-selftest-')
    results = []
    try:
        pkg = os.path.join(scratch, 'scratchpkg')
        os.makedirs(pkg)
        target = os.path.join(pkg, '__init__.py')
        write_bytes(target, b'VALUE = "AAA"\n')
        raw0 = read_bytes(target)
        # 顶替 pytest 的检查器：变异后（BBB）必须红，还原后（AAA）必须绿。
        argv = ['-c', 'import sys, scratchpkg;'
                      'sys.exit(0 if scratchpkg.VALUE == "AAA" else 1)']
        rel = os.path.join('scratchpkg', '__init__.py')

        for tag, probe, want_rc in SELFTEST_CASES:
            spec = {'tag': tag, 'path': rel, 'cmd': 'raw', 'argv': argv,
                    'label': 'fake-checker', 'what': tag, 'probe': probe,
                    'frm': 'VALUE = "AAA"' if not tag.startswith('C') else 'VALUE = "ZZZ"',
                    'to': 'VALUE = "BBB"'}
            fails = []
            rc = roundtrip(spec, py, lambda *a, **k: None, fails, root=scratch)
            same = read_bytes(target) == raw0
            ok = (rc == want_rc) and same and not fails
            results.append(ok)
            print('  [%s] %-24s rc=%d (期望 %d) 逐字节已还原=%s fails=%d'
                  % ('OK ' if ok else 'BAD', tag, rc, want_rc, same, len(fails)))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    good = sum(1 for r in results if r)
    print('verdict: %s (%d/%d sample(s))'
          % ('PASS' if good == len(results) else 'FAIL', good, len(results)))
    return 0 if good == len(results) else 2


def main(argv):
    if '--selftest' in argv:
        print('# ci-dryrun --selftest（不碰仓库文件，只跑临时目录里的触发测试）')
        return selftest(find_python()[0])
    no_roundtrip = '--no-roundtrip' in argv
    py, py_label = find_python()
    pytest_cmd = [py, '-m', 'pytest', '-q']
    gates_cmd = [py, '-X', 'utf8', os.path.join('tools', 'run_all_gates.py')]

    lines = []
    failures = []

    def say(s=''):
        lines.append(s)
        print(s)

    say('# ci-dryrun 报告（本地等价，不是 CI）')
    say()
    say('这一份是**快照**：结论只对下面记录的解释器与 commit 成立，改完代码必须重跑。')
    say('本仓库没有配置 git remote，因此 `.github/workflows/ci.yml` **从未在 GitHub 上真实运行过**；')
    say('下面跑的是与它**相同的两条命令**。')
    say()
    say('generated: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    say('python:    %s  (%s)' % (py, py_label))
    rc, out, _ = run([py, '--version'])
    say('version:   %s' % (out.strip() or '?'))
    rc, out, _ = run(['git', 'rev-parse', 'HEAD'])
    say('commit:    %s' % (out.strip() or '(非 git 仓库)'))
    rc, out, _ = run(['git', 'status', '--porcelain'])
    dirty = len([ln for ln in out.split('\n') if ln.strip()])
    say('dirty:     %d 项未提交改动%s' % (dirty, '（快照与产物不同步）' if dirty else ''))
    # 必须在第 1 步之前就清：上一次运行（或往返的变异）留下的 .pyc 可能比磁盘上的
    # 源码更新，让**还没跑过任何变异**的这一次也拿到旧版本号（实测过：pytest 报出的
    # 版本号与磁盘上写的不同）。这里刻意不写具体版本号 —— 它每轮会 bump，
    # 写死等于给自己留一个必然过期的陈述。
    killed = purge_pycache(ROOT)
    say('pycache:   启动时清掉 %d 个 __pycache__（陈旧字节码会让结论与磁盘源码无关）' % killed)
    say()

    # ---- 1) pytest（绿） ---------------------------------------------------------
    say('## 1. %s' % ' '.join(['python', '-m', 'pytest', '-q']))
    rc, out, err = run(pytest_cmd)
    say('exit: %d' % rc)
    for ln in tail(out + err):
        say('  | ' + ln)
    if rc != 0:
        failures.append('pytest 退出码 %d，期望 0' % rc)
    say()

    # ---- 2) 红→绿往返 ------------------------------------------------------------
    if no_roundtrip:
        say('## 2. 红→绿往返：已跳过（--no-roundtrip）—— 因此本报告**不构成**触发测试证据')
        say()
    else:
        say('## 2. 红→绿往返（I0 触发测试的证据：两个关键的东西各红一次）')
        say()
        for spec in ROUNDTRIPS:
            rc = roundtrip(spec, py, say, failures)
            if rc != 0:
                write_report(lines)
                return rc

    # ---- 3) 门禁 ----------------------------------------------------------------
    say('## 3. python tools/run_all_gates.py')
    rc, out, err = run(gates_cmd)
    say('exit: %d' % rc)
    for ln in tail(out + err):
        say('  | ' + ln)
    if rc != 0:
        failures.append('run_all_gates 退出码 %d，期望 0' % rc)
    say()

    # ---- 判定 --------------------------------------------------------------------
    say('## verdict')
    if failures:
        for f in failures:
            say('ISSUE [CI-DRYRUN] %s' % f)
        say('verdict: FAIL (%d issue(s))' % len(failures))
    else:
        say('verdict: PASS (0 issue(s))')
    write_report(lines)
    print('REPORT=%s' % REPORT_PATH)
    return 1 if failures else 0


def write_report(lines):
    with open(REPORT_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
