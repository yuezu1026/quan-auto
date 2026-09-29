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
    python tools/dev.py outline FILE       # 只读：文件的标题目录 + 每节行范围与 ≈token
    python tools/dev.py find REGEX         # 只读：只回命中行，不回整份文件
    python tools/dev.py --selftest         # 证明本 runner 真的能报 FAIL

必须用 `.venv\\Scripts\\python.exe` 运行（裸 `python` 是 anaconda base，没装 pytest）。

退出码：0 = 本次请求全绿；1 = 有失败（含「解析不到 verdict」这种提取为空的情形）；
2 = 本脚本自身用法/自测错。
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile

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
# outline / find —— 把「按需读一个 L1 产物」从「整份读进来」改成「先看目录再取一节」
#
# 为什么这两个命令属于这里：本仓库最大的**按需**成本是三份契约文档（合计约 6.9k 行；
# 单读数据中心契约一份 ≈49k token），而 90% 的提问只需要其中一节。整份读进来等于把
# 48k token 花在 1k 的问题上，而且那个成本**每个回合都要重发** —— 回合才是真正的成本，
# 机器侧那十几秒根本不吃紧（本文件顶部那段实测已经记过同一件事）。
#
# 与文件顶部那条「只打 ASCII」的关系：目录/命中的**载荷本身就是中文**，转 ASCII 等于
# 把载荷丢掉。所以这里改成**逐行降级**：能打印就原样打印，遇到当前控制台编码不了的
# 行才退成 backslashreplace 并打一行 NOTE —— 「乱码」永远不会静默发生。
#
# 边界：它们**不是**门禁（不判对错），也**不是** grep 的替代品 —— 返回的是
# 「去哪一行看」，不是结论。结论读 `tools/gates-report.txt`。
# ---------------------------------------------------------------------------------
HEADING_RE = re.compile(r'^(#{1,6})[ \t]+(\S.*?)[ \t]*$')
FENCE_RE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})')
FIND_GLOBS = ('*.md', '*.py', '*.sql', '*.json', '*.yml')
FIND_MAX_DEFAULT = 40
FIND_LINE_MAX = 180


def est_tokens(text):
    """粗估 token：CJK 一字 ≈1，其余 ≈4 字符 1。只用来**比大小**，不是真值。

    故意不接真 tokenizer：C4 规定 tools/ 只用标准库，而这里要回答的问题只是
    「这份文件的哪一节值得读」——量级对就够用。
    """
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF01 <= o <= 0xFF5E:
            cjk += 1
    return int(cjk + (len(text) - cjk) / 4.0)


def read_lines(path):
    """读成行列表；**读不到返回 None，不是空列表**。

    两处都是踩过的坑：
      * 先按字节读、`utf-8-sig` 解码并**规范化 CRLF**：裸 `\n` 的匹配在 CRLF 文件上
        静默失配 —— 本项目实测过一次（`CRLF count=1373 / LF-only=0` ⇒ 提取到 0 字符
        而报告全绿，看起来比成功还干净）。
      * 「读不到」与「文件是空的」必须分得开：前者是失败，后者是合法的空输入。
        None vs [] 就是这条区分（空转守卫的思想，用在这里是防「静默跳过」）。
    """
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError:
        return None
    text = raw.decode('utf-8-sig', 'replace').replace('\r\n', '\n')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()  # 行尾那个空串是终止符，不是一行
    return lines


def headings(lines):
    """返回 [(行号(1 基), 级别, 标题)]。

    🔴 **必须跳过围栏代码块**（``` / ~~~）。第一版没跳，实测当场抓到：数据中心契约
    （2514 行）L72-73 是代码块里的 Python 注释（`# feed 是一个…` / `# 它的 9 个方法
    签名…`），被当成两条**一级标题** ⇒ 目录里凭空多出一个「占据 L73-2514、47772 tok」
    的假节，把真正的「附录 B」吞掉。报告看起来完全合理 —— 这正是本仓库那句
    「变异没打到分支与探测器不存在长得一样」的邻座：**提取器错了，却报出一份像样的表**。
    样本 `CLEAN-fenced-code-not-headings` 守着这条。
    """
    found = []
    fence = None  # (marker_char, length)；None = 不在代码块里
    for i, line in enumerate(lines, 1):
        fence_hit = FENCE_RE.match(line)
        if fence_hit:
            marker = fence_hit.group(1)[0]
            length = len(fence_hit.group(1))
            if fence is None:
                fence = (marker, length)
            elif marker == fence[0] and length >= fence[1]:
                fence = None  # 闭合围栏：同字符且不短于开启的那条
            continue
        if fence is not None:
            continue
        m = HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2)))
    return found


def heading_sections(lines, depth):
    """返回 [(行号, 级别, 标题, 节起, 节止, ≈token)]，只留 level <= depth 的。

    节范围 = 到**下一个级别不高于自己**的标题为止 ⇒ `##` 的范围包含它的 `###` 子节，
    与「这一节一共多少 token」的直觉一致（把子节排除在外的目录会低报这一节的成本，
    而那正是读者要看的数）。
    """
    marks = headings(lines)
    out = []
    for idx, (lineno, level, title) in enumerate(marks):
        if level > depth:
            continue
        end = len(lines)
        for later, later_level, _ in marks[idx + 1:]:
            if later_level <= level:
                end = later - 1
                break
        out.append((lineno, level, title, lineno, end,
                    est_tokens('\n'.join(lines[lineno - 1:end]))))
    return out


def owner_of(marks, lineno):
    """某个行号落在哪个标题下（find 的上下文）。"""
    title = '(no heading)'
    for i, level, text in marks:
        if i > lineno:
            break
        title = '%s %s' % ('#' * level, text)
    return title


def say(line):
    """打印一行；当前控制台编码不了就**显式转义并吼一声**，不静默产生乱码。"""
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'backslashreplace').decode('ascii'))
        print('NOTE: 上面那行按当前控制台编码打不出来，已转义显示；'
              '要原串请加 --out=FILE（UTF-8 落盘）')


def write_out(path, text):
    """UTF-8 落盘。PS 5.1 的 `>` 写的是 UTF-16LE，所以想转存一律走这里。"""
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def tracked_files(globs):
    """`git ls-files` 的清单，按 fnmatch 过滤；取不到返回 None（不猜、不空转）。

    `-c core.quotePath=false` 是必须的：git 默认把非 ASCII 路径写成八进制转义，
    于是 `docs/智能量化…md:12:` 会变成一串 `\\346\\231\\272…`，看起来像文件名叫错了
    （本项目「读报告要用 UTF-8」那类坑的同族 —— 又是读法被当成产物坏了）。
    """
    rc, out = run_raw(['git', '-c', 'core.quotePath=false', 'ls-files'])
    if rc != 0:
        return None
    names = [line for line in out.split('\n') if line.strip()]
    if not globs:
        return names
    return [n for n in names if any(fnmatch.fnmatch(n, g) for g in globs)]


def cmd_outline(argv):
    path, depth, limit, out_path = None, 2, 200, None
    for arg in argv:
        if arg.startswith('--depth='):
            if not arg[8:].isdigit():
                print('bad --depth: %s' % arg)
                return 2
            depth = int(arg[8:])
        elif arg.startswith('--max='):
            if not arg[6:].isdigit():
                print('bad --max: %s' % arg)
                return 2
            limit = int(arg[6:])
        elif arg.startswith('--out='):
            out_path = arg[6:]
        elif arg.startswith('-'):
            print('unknown option: %s' % arg)
            return 2
        elif path is None:
            path = arg
        else:
            print('outline takes exactly one file (got %s)' % ' '.join(argv))
            return 2
    if path is None or depth < 1:
        print('usage: python tools/dev.py outline FILE [--depth=N] [--max=N] [--out=FILE]')
        return 2
    abs_path = path if os.path.isabs(path) else os.path.join(ROOT, path)
    lines = read_lines(abs_path)
    if lines is None:
        print('cannot read: %s' % path)
        return 2
    rows = heading_sections(lines, depth)
    total = est_tokens('\n'.join(lines))
    body = ['outline %s  lines=%d  ~tokens=%d  depth=%d'
            % (path, len(lines), total, depth)]
    if not rows:
        body.append('NOTE: depth<=%d 里一个标题都没有 —— 别把这行读成「文件是空的」；'
                    '无标题的文件（CSV 之类）本来就没有目录' % depth)
    for _lineno, level, title, start, end, tokens in rows[:limit]:
        body.append('%5d-%-5d %7d tok  %s%s'
                    % (start, end, tokens, '  ' * (level - 1),
                       '#' * level + ' ' + title))
    if len(rows) > limit:
        body.append('NOTE: 只打了前 %d 个标题（共 %d 个），要全用 --max= 放宽'
                    % (limit, len(rows)))
    if rows:
        body.append('TOTAL   %d tok（=整份读进来要付的成本）；'
                    '取某一节的内容用 `dev.py find`，或按上面的行范围读' % total)
    if out_path:
        write_out(out_path, '\n'.join(body) + '\n')
        print('outline written: %s (%d lines)' % (out_path, len(body)))
        return 0
    for line in body:
        say(line)
    return 0


def cmd_find(argv):
    pattern, globs, limit, out_path = None, [], FIND_MAX_DEFAULT, None
    for arg in argv:
        if arg.startswith('--in='):
            globs.append(arg[5:])
        elif arg.startswith('--max='):
            if not arg[6:].isdigit():
                print('bad --max: %s' % arg)
                return 2
            limit = int(arg[6:])
        elif arg.startswith('--out='):
            out_path = arg[6:]
        elif arg.startswith('-'):
            print('unknown option: %s' % arg)
            return 2
        elif pattern is None:
            pattern = arg
        else:
            print('find takes exactly one pattern (got %s)' % ' '.join(argv))
            return 2
    if pattern is None:
        print('usage: python tools/dev.py find REGEX [--in=GLOB ...] [--max=N] [--out=FILE]')
        return 2
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        print('bad regex: %s' % exc)
        return 2
    files = tracked_files(globs or FIND_GLOBS)
    if files is None:
        # 拿不到清单就**拒判**：一个「扫了 0 个文件、0 命中」的报告比错的报告更危险。
        print('git ls-files failed -- find 需要 git 工作树，拿不到就不猜')
        return 2
    if not files:
        print('no tracked file matches %s' % (globs or list(FIND_GLOBS)))
        return 2
    total_hits, shown, body = 0, 0, []
    current_file = None
    for rel in files:
        lines = read_lines(os.path.join(ROOT, *rel.split('/')))
        if lines is None:
            continue
        marks = headings(lines)
        current_owner = None
        for i, line in enumerate(lines, 1):
            if not rx.search(line):
                continue
            total_hits += 1
            if shown >= limit:
                continue
            if rel != current_file:
                current_file = rel
                body.append(rel)
                current_owner = None
            owner = owner_of(marks, i)
            if owner != current_owner:
                current_owner = owner
                body.append('       %s' % owner)
            body.append('  %-5d %s' % (i, line.strip()[:FIND_LINE_MAX]))
            shown += 1
    head = ('find %r  files=%d  hits=%d  shown=%d  globs=%s'
            % (pattern, len(files), total_hits, shown, ','.join(globs or FIND_GLOBS)))
    if total_hits > shown:
        # 截断必须看得见：静默截断会让「还有更多」看起来像「就这些」。
        body.append('NOTE: 命中 %d 条，只打了前 %d 条 —— 收紧 pattern 或 --in='
                    % (total_hits, shown))
    if out_path:
        write_out(out_path, '\n'.join([head] + body) + '\n')
        print('find written: %s (%d hits -> %d lines)'
              % (out_path, total_hits, len(body)))
        return 0
    say(head)
    for line in body:
        say(line)
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

    # ---- outline / find 的纯函数：干净样本 + 负样本 + 空转样本 + 控制组 ----
    doc = '# T\n\nintro\n\n## A\n\nalpha\n\n### A1\n\nsub\n\n## B\n\nbeta\n'
    doc_lines = doc.split('\n')
    if doc_lines and doc_lines[-1] == '':
        doc_lines.pop()
    secs = heading_sections(doc_lines, 2)
    # 干净样本：depth=2 只出 #/##，`### A1` 不单独成行
    check('CLEAN-outline-titles', [s[2] for s in secs] == ['T', 'A', 'B'],
          'titles=%s' % [s[2] for s in secs])
    check('CLEAN-outline-starts', [s[0] for s in secs] == [1, 5, 13],
          'starts=%s' % [s[0] for s in secs])
    # 「## A」的节止必须跨过它的 ### A1 —— 否则「这一节多少 token」会低报
    check('CLEAN-outline-parent-covers-child', secs[1][4] == 12,
          'A ends at %d (want 12)' % secs[1][4])
    # 负样本：一个标题都没有的文件 ⇒ 目录为空（既不是异常，也不是静默的成功）
    check('NEG-no-headings', heading_sections(['plain', 'text'], 2) == [],
          'rows=%d' % len(heading_sections(['plain', 'text'], 2)))
    # 🔴 围栏代码块里的 `# 注释` 不是标题。这条样本是**在真产物上先踩到**才补的：
    # 第一版提取器把数据中心契约 L72-73 的 Python 注释当成一级标题，目录里凭空多出一个
    # 47772 tok 的假节并吞掉了真正的附录 B —— 报告看着完全合理。
    fenced = ['# T', '', '```python', '# not a heading', 'x = 1', '```', '', '## A', '']
    check('CLEAN-fenced-code-not-headings',
          [s[2] for s in heading_sections(fenced, 2)] == ['T', 'A'],
          'titles=%s' % [s[2] for s in heading_sections(fenced, 2)])
    # 同一块用 ~ 围栏、且内部再写一行 ``` 时，`~` 块不算闭合 ⇒ 里面的 ``` 仍是内容
    tilde = ['# T', '~~~', '# still code', '```', '# also code', '~~~', '', '## A', '']
    check('CLEAN-tilde-fence-not-headings',
          [s[2] for s in heading_sections(tilde, 2)] == ['T', 'A'],
          'titles=%s' % [s[2] for s in heading_sections(tilde, 2)])
    # 空转样本：「读不到」必须是 None，与「空文件」（[]）分得开
    check('EMPTY-missing-file-is-None',
          read_lines(os.path.join(tempfile.gettempdir(), 'no-such-dev-file.md')) is None)
    empty_path = os.path.join(tempfile.gettempdir(), 'dev-selftest-empty.md')
    with open(empty_path, 'wb') as handle:
        handle.write(b'')
    try:
        check('EMPTY-empty-file-is-list', read_lines(empty_path) == [],
              'got=%r' % (read_lines(empty_path),))
    finally:
        os.remove(empty_path)
    # CRLF 规范化：本项目实测过的那个坑（裸 \n 匹配在 CRLF 文件上静默失配）
    crlf_path = os.path.join(tempfile.gettempdir(), 'dev-selftest-crlf.md')
    with open(crlf_path, 'wb') as handle:
        handle.write('# T\r\n\r\n## A\r\n\r\nalpha\r\n'.encode('utf-8'))
    try:
        crlf = read_lines(crlf_path)
        check('CLEAN-crlf-normalised',
              crlf is not None and not any('\r' in item for item in crlf)
              and [s[2] for s in heading_sections(crlf, 2)] == ['T', 'A'],
              'titles=%s' % (crlf and [s[2] for s in heading_sections(crlf, 2)]))
    finally:
        os.remove(crlf_path)
    # 控制组：token 估计只要在「中文 vs ASCII」上量级对即可（它本来就不是真值）
    check('CONTROL-est-tokens-order',
          est_tokens('中文中文') == 4 and est_tokens('abcdefgh') == 2,
          'cjk=%d ascii=%d' % (est_tokens('中文中文'), est_tokens('abcdefgh')))
    # 真实仓库那一次：本文件自己的目录要能打出来，且 find 只回命中行
    rc, out = run(['tools/dev.py', 'outline', 'tools/dev.py'])
    check('REAL-outline-own-source', rc == 0 and 'TOTAL' in out,
          'rc=%d bytes=%d' % (rc, len(out)))
    rc, out = run(['tools/dev.py', 'find', 'def cmd_find', '--in=tools/dev.py'])
    check('REAL-find-own-source', rc == 0 and 'tools/dev.py' in out,
          'rc=%d bytes=%d' % (rc, len(out)))

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
  python tools/dev.py outline FILE       read-only: heading map + per-section ~tokens
  python tools/dev.py find REGEX         read-only: matching lines only, not whole files
  python tools/dev.py --selftest         prove this runner can report FAIL

Use .venv\\Scripts\\python.exe. The verdict/status output is ASCII on purpose; the
Chinese detail lives in tools/gates-report.txt (UTF-8) -- read it, do not re-run.
outline/find print the matched text verbatim (ASCII-ising it would drop the payload)
and fall back to backslash escapes with a NOTE only when the console cannot encode.
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
    if head == 'outline':
        return cmd_outline(rest)
    if head == 'find':
        return cmd_find(rest)
    print('unknown command: %s' % head)
    print(USAGE)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
