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
  1. 故意在测试里插一句必然失败的自断言（通过替换一个 UTF-8 字符，见下）；
  2. 断言替换**确实发生了**（`applied=False` 直接 exit 2）—— 变异没生效时「全绿」
     是在**没改过的文件**上得出的结论，本仓库踩过这个坑；
  3. 跑 pytest，要求退出码 != 0（红）；
  4. 还原文件，比对字节与原始内容完全一致；
  5. 再跑，要求退出码 == 0（绿）。

Usage
-----
    python tools/ci_dryrun.py                # 跑全部（含红→绿往返）
    python tools/ci_dryrun.py --no-roundtrip # 只跑绿，跳过往返（快）

Exit codes: 0 = 全部符合预期, 1 = 有一步不符预期, 2 = 工具自身坏了（如变异没应用）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def read_text(path):
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return fh.read().replace('\r\n', '\n')
    except OSError:
        return None


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def tail(text, n=12):
    lines = [ln for ln in text.split('\n') if ln.strip()]
    return lines[-n:]


def roundtrip(spec, py, say, failures):
    """对 spec 描述的一处变异做一次红→绿往返；任一步不符预期就记入 failures。

    返回 2 表示工具自身坏了（靶子不在、变异未应用、还原不回去），调用方必须直接停。
    """
    target = os.path.join(ROOT, spec['path'])
    rel = spec['path'].replace('\\', '/')
    cmd = [py, '-m', 'pytest', '-q'] if spec['cmd'] == 'pytest' else \
        [py, '-X', 'utf8', os.path.join('tools', 'verify_skeleton.py')]
    label = 'pytest' if spec['cmd'] == 'pytest' else 'verify_skeleton'

    say('### %s -- %s' % (spec['tag'], spec['what']))
    say('  靶子: %s  %r -> %r' % (rel, spec['frm'], spec['to']))
    before = read_text(target)
    if before is None:
        say('exit: 2 -- 找不了 %s' % rel)
        return 2
    if before.count(spec['frm']) != 1:
        say('exit: 2 -- 靶字符串在 %s 里出现 %d 次（需要恰好 1 次）；'
            '替换会静默 no-op 或打错地方' % (rel, before.count(spec['frm'])))
        return 2

    after = before.replace(spec['frm'], spec['to'])
    # 🔴 自断言：变异必须真的改了文件，否则「红/绿」都是在**没改过的文件**上得出的
    if after == before:
        say('exit: 2 -- 替换没产生任何变化（变异未应用）')
        return 2
    write_text(target, after)
    if read_text(target) != after:
        write_text(target, before)
        say('exit: 2 -- 变异写入后读回不一致')
        return 2
    say('  applied: True  (字节已变)')

    killed = purge_pycache(ROOT)
    say('  清缓存: 删掉 %d 个 __pycache__（否则同秒写回会让 Python 继续用旧字节码）' % killed)
    try:
        rc_red, out_red, err_red = run(cmd)
    finally:
        write_text(target, before)
        purge_pycache(ROOT)
    restored = read_text(target) == before
    say('  红: %s exit=%d' % (label, rc_red))
    for ln in tail(out_red + err_red, 3):
        say('    | ' + ln)
    say('  还原: %s' % ('字节与原始内容一致' if restored else '不一致！'))

    if not restored:
        failures.append('%s: %s 还原不回去，仓库已被改脏' % (spec['tag'], rel))
        return 0
    if rc_red == 0:
        failures.append('%s: %s 在该变异下仍然退出 0 —— 这一步不会红，绿没有意义'
                        % (spec['tag'], label))
    elif rc_red == 2:
        failures.append('%s: %s 退出 2（自检/收集错误），红得不干净' % (spec['tag'], label))

    rc_green, out_green, err_green = run(cmd)
    say('  绿: %s exit=%d' % (label, rc_green))
    for ln in tail(out_green + err_green, 3):
        say('    | ' + ln)
    if rc_green != 0:
        failures.append('%s: 还原后 %s 退出 %d，期望 0' % (spec['tag'], label, rc_green))
    say()
    return 0


def main(argv):
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
