#!/usr/bin/env python
"""skeleton -- I0 的工程骨架是否还在（pyproject / 包 / 测试 / CI 接线）。

Why this gate exists
--------------------
I0 的产物（`pyproject.toml`、包目录、可跑的 pytest、CI 接线）是**后面每一轮迭代的地基**，
而它们全都是「存在即用、删掉也不报错」的东西：
删掉 `pyproject.toml`、把包目录改名、把 CI 里那一行 `run: python tools/run_all_gates.py`
注释掉 —— 上面没有任何东西会变红。尤其最后一条：CI 变成只跑 pytest 之后，
**门禁再也不会被执行**，而所有门禁报告仍然是绿的（因为本地还在跑）。这正是本仓库
反复踩到的「假绿」家族的又一种。所以这里用门禁把骨架钉住。

Boundary -- what this gate does NOT prove
-----------------------------------------
**它不执行 pytest，也不接受 pytest 的退出码。** 门禁只证明「CI 里确实会跑 pytest」，
不证明测试真的通过。理由：让门禁去 `import pytest` 会把 `tools/` 拖上第三方依赖，
并且在没有装 pytest 的解释器下变成环境性 FAIL（噪声，不是信号）。
`pytest` 的真实退出码记录在 `tools/ci-dryrun-report.txt` 里，和 SQL 冒烟一样属于
**运行时快照**，要连同当时的命令与版本一起读。

Usage
-----
    python tools/verify_skeleton.py             # 检查仓库根
    python tools/verify_skeleton.py --selftest  # 自证：6 个探测器各自都能红
    python tools/verify_skeleton.py <root>      # 检查别的目录（用于触发测试）

Exit codes: 0 = PASS, 1 = FAIL, 2 = selftest 自身的断言失败（说明这个门禁坏了，不是产物坏了）
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sys
import tempfile
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PKG_NAME = 'quanauto'
PYPROJECT = 'pyproject.toml'
CI_PATH = os.path.join('.github', 'workflows', 'ci.yml')

# CI 里**必须**逐字出现的两条命令。改这里就等于改判据，别为了让某次改动通过而放宽。
REQUIRED_CI_RUNS = ('python -m pytest -q', 'python tools/run_all_gates.py')

# YAML 里 run 既可能是映射键也可能作为列表项（`- run:`），两种都要认得 ——
# 第一版漏掉 `- ` 前缀，于是干净样本上一个 run: 都提取不到、空转守卫开火。
RUN_RE = re.compile(r'^[ \t]*(?:-[ \t]*)?run:[ \t]*(.*?)[ \t]*$', re.M)
SWALLOW_RE = re.compile(r'continue-on-error:[ \t]*true|\|\|[ \t]*true\b')

# 本门禁一共几个探测器。用来在报告里打出分母 —— 分母为 0 的报告不许被读成通过。
DETECTORS = ('PYPROJECT', 'VERSION', 'PACKAGE', 'TESTS', 'CI', 'CI-SWALLOW')


def read_text(path):
    """返回规范化后的文本（BOM 去掉、CRLF -> LF），读不到返回 None。

    必须先规范化再匹配：本仓库的 md/sql 可能是 CRLF，裸 `\\n` 正则对 CRLF 会静默失配，
    于是所有探测器一起空转却打印 PASS（规范 §六 案例一）。
    """
    try:
        with open(path, 'r', encoding='utf-8-sig') as fp:
            return fp.read().replace('\r\n', '\n')
    except OSError:
        return None


def _load_pyproject(root):
    text = read_text(os.path.join(root, PYPROJECT))
    if text is None:
        return None, None
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text, None


def check_pyproject(root):
    findings = []
    text, data = _load_pyproject(root)
    if text is None:
        findings.append(('PYPROJECT', '%s 不存在 —— 后面的依赖清单/打包配置全都没了' % PYPROJECT))
    elif data is None:
        findings.append(('PYPROJECT', '%s 无法被 tomllib 解析（语法坏了就等于不存在）' % PYPROJECT))
    else:
        if not data.get('project', {}).get('name'):
            findings.append(('PYPROJECT', '%s 缺 [project].name' % PYPROJECT))
        if not data.get('project', {}).get('requires-python'):
            findings.append(('PYPROJECT', '%s 缺 [project].requires-python（解释器下限没有声明）'
                             % PYPROJECT))
    return findings


def _package_version(root):
    """用 ast 读包里的 __version__，不 import（import 需要 sys.path 恰好正确）。

    返回 (found_bool, value_or_None, note)。
    """
    init = os.path.join(root, PKG_NAME, '__init__.py')
    text = read_text(init)
    if text is None:
        return False, None, '%s/%s/__init__.py 不存在' % (root, PKG_NAME)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return False, None, '%s 语法错误：%s' % (init, exc.msg)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == '__version__':
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return True, node.value.value, ''
    return False, None, '%s/%s/__init__.py 里找不到字面量 __version__' % (root, PKG_NAME)


def check_version(root):
    """版本号有两个来源（pyproject 与包里），必须一致 —— 这是已知的漂移源。"""
    findings = []
    _, data = _load_pyproject(root)
    found, version, note = _package_version(root)
    if not found:
        findings.append(('VERSION', note))
        return findings
    if data is None:
        findings.append(('VERSION', 'pyproject 不可解析，无法核对版本号（不是「一致」）'))
        return findings
    declared = data.get('project', {}).get('version')
    if declared is None:
        findings.append(('VERSION', '%s 里没有 [project].version' % PYPROJECT))
    elif declared != version:
        findings.append(('VERSION', '版本号两处不一致：%s=%r vs %s/__init__.py=%r'
                         % (PYPROJECT, declared, PKG_NAME, version)))
    return findings


def check_package(root):
    findings = []
    _, data = _load_pyproject(root)
    pkg_dir = os.path.join(root, PKG_NAME)
    init = os.path.join(pkg_dir, '__init__.py')
    if not os.path.isdir(pkg_dir):
        findings.append(('PACKAGE', '包目录 %s/ 不存在' % PKG_NAME))
    elif read_text(init) is None:
        findings.append(('PACKAGE', '%s/__init__.py 不存在（目录存在不等于包存在）' % PKG_NAME))
    elif not read_text(init).strip():
        findings.append(('PACKAGE', '%s/__init__.py 是空的' % PKG_NAME))
    if data is not None:
        declared = data.get('tool', {}).get('setuptools', {}).get('packages')
        if declared is None:
            findings.append(('PACKAGE', '%s 缺 [tool.setuptools].packages（装出来的包和目录会不一致）'
                             % PYPROJECT))
        elif PKG_NAME not in declared:
            findings.append(('PACKAGE', '[tool.setuptools].packages=%r 里没有 %r'
                             % (declared, PKG_NAME)))
    return findings


def check_tests(root):
    findings = []
    tests_dir = os.path.join(root, 'tests')
    if not os.path.isdir(tests_dir):
        findings.append(('TESTS', 'tests/ 目录不存在 —— 「可运行」那一半没有任何红网'))
        return findings
    test_files = sorted(f for f in os.listdir(tests_dir)
                        if f.startswith('test_') and f.endswith('.py'))
    if not test_files:
        findings.append(('TESTS', 'tests/ 下没有任何 test_*.py（pytest 会以退出码 5 收场）'))
    _, data = _load_pyproject(root)
    if data is None:
        findings.append(('TESTS', 'pyproject 不可解析，无法核对 pytest 配置（不是「已配置」）'))
        return findings
    ini = data.get('tool', {}).get('pytest', {}).get('ini_options', {})
    testpaths = ini.get('testpaths') or []
    if 'tests' not in testpaths:
        findings.append(('TESTS', '[tool.pytest.ini_options].testpaths=%r 不含 "tests"'
                         % (testpaths,)))
    pythonpath = ini.get('pythonpath') or []
    if '.' not in pythonpath:
        # 扁平布局 + 包未安装：少了这条，`import quanauto` 在 CI 上会因 sys.path 不含仓库根而失败。
        findings.append(('TESTS', '[tool.pytest.ini_options].pythonpath=%r 不含 "."'
                         % (pythonpath,)))
    return findings


def check_ci(root):
    findings = []
    text = read_text(os.path.join(root, CI_PATH))
    if text is None:
        findings.append(('CI', '%s 不存在 —— 门禁和测试不会在 CI 里跑' % CI_PATH.replace('\\', '/')))
        return findings
    runs = [r.split('#')[0].strip() for r in RUN_RE.findall(text)]
    runs = [re.sub(r'\s+', ' ', r) for r in runs if r.split('#')[0].strip()]
    if not runs:
        # 空转守卫：一条 run: 都提取不到时，下面的「包含」判断会全部落空而报告仍然干净。
        findings.append(('CI', '%s 里一条 run: 都没提取到 —— 后续检查形同虚设，拒绝通过'
                         % CI_PATH.replace('\\', '/')))
        return findings
    for required in REQUIRED_CI_RUNS:
        if required not in runs:
            findings.append(('CI', '%s 里没有 `run: %s`（提取到 %d 条 run:）'
                             % (CI_PATH.replace('\\', '/'), required, len(runs))))
    return findings


def strip_yaml_comments(text):
    """剥掉整行与行内注释（引号内的 # 不算注释）。

    没有这一步，注释里写一句「不要用 `|| true` 吞退出码」就会自己把门禁撞红 ——
    实测过的假阳性。判据必须只看**生效配置**。
    """
    out = []
    for line in text.split('\n'):
        quote = None
        for i, ch in enumerate(line):
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '#':
                line = line[:i]
                break
        out.append(line)
    return '\n'.join(out)


def check_ci_swallow(root):
    """CI 里不许出现会吞掉退出码的写法 —— 那会让红变绿。"""
    findings = []
    text = read_text(os.path.join(root, CI_PATH))
    if text is None:
        return findings  # 文件缺失已由 CI 探测器报出，不重复计数
    body = strip_yaml_comments(text)
    for m in SWALLOW_RE.finditer(body):
        line_no = body[:m.start()].count('\n') + 1
        findings.append(('CI-SWALLOW', '%s:%d 出现 %r —— 退出码被吞掉，红会变成绿'
                         % (CI_PATH.replace('\\', '/'), line_no, m.group(0))))
    return findings


DETECTOR_FUNCS = {
    'PYPROJECT': check_pyproject,
    'VERSION': check_version,
    'PACKAGE': check_package,
    'TESTS': check_tests,
    'CI': check_ci,
    'CI-SWALLOW': check_ci_swallow,
}


def verify(root):
    findings = []
    for name in DETECTORS:
        findings.extend(DETECTOR_FUNCS[name](root))
    return findings


# ---------------------------------------------------------------------------------
# Selftest: 每个探测器一个样本，另加一个干净样本（防误报）与一个空目录样本。
#
# 只跑「全绿」等于没测：这里每一个样本都必须让**指定的那个**探测器开火，
# 并且干净样本必须一条都不报。
# ---------------------------------------------------------------------------------
CLEAN_PYPROJECT = '''\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "quan-auto"
version = "1.2.3"
requires-python = ">=3.11"
dependencies = []

[tool.setuptools]
packages = ["quanauto"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
'''

CLEAN_CI = '''\
name: ci
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m pip install -e ".[dev]"
      - run: python -m pytest -q
      - run: python tools/run_all_gates.py
'''


def _sandbox(tmp, *, pyproject=CLEAN_PYPROJECT, version='"1.2.3"', ci=CLEAN_CI,
             packages='"quanauto"', make_pkg=True, make_tests=True):
    """造一个最小仓库。每个样本只动一处，其余保持干净 —— 这样报出来的必定是那一处。"""
    root = tempfile.mkdtemp(dir=tmp)
    with open(os.path.join(root, PYPROJECT), 'w', encoding='utf-8', newline='\n') as fp:
        fp.write(pyproject.replace('packages = ["quanauto"]', 'packages = [%s]' % packages))
    if make_pkg:
        os.makedirs(os.path.join(root, PKG_NAME))
        with open(os.path.join(root, PKG_NAME, '__init__.py'), 'w',
                  encoding='utf-8', newline='\n') as fp:
            fp.write('"""fake package."""\n\n__version__ = %s\n' % version)
    if make_tests:
        os.makedirs(os.path.join(root, 'tests'))
        with open(os.path.join(root, 'tests', 'test_x.py'), 'w',
                  encoding='utf-8', newline='\n') as fp:
            fp.write('def test_ok():\n    assert True\n')
    if ci is not None:
        d = os.path.join(root, '.github', 'workflows')
        os.makedirs(d)
        with open(os.path.join(d, 'ci.yml'), 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(ci)
    return root


def _codes(findings):
    return sorted({code for code, _ in findings})


def selftest():
    tmp = tempfile.mkdtemp(prefix='skeleton-selftest-')
    failures = []
    checked = 0

    def expect(tag, root, want_codes):
        nonlocal checked
        checked += 1
        got = _codes(verify(root))
        want = sorted(want_codes)
        ok = (got == want)
        print('  %-28s want=%-34s got=%s %s' % (tag, ','.join(want) or '(none)',
                                                ','.join(got) or '(none)',
                                                'OK' if ok else 'MISMATCH'))
        if not ok:
            failures.append('%s: want %r got %r' % (tag, want, got))

    try:
        # 0) 干净样本：必须一条都不报（防误报 —— 没有它，负样本全红也证明不了什么）
        expect('CLEAN (no findings)', _sandbox(tmp), ())
        # 1) pyproject 整个消失 -> PYPROJECT（VERSION/TESTS 也会连带报，因为它们的
        #    判据依赖 pyproject 里的配置 —— 「读不到」必须报红，不得静默当成干净）
        root = _sandbox(tmp)
        os.remove(os.path.join(root, PYPROJECT))
        expect('MUT-no-pyproject', root, ('PYPROJECT', 'TESTS', 'VERSION'))
        # 2) pyproject 语法坏掉 -> PYPROJECT（解析失败不得被读成「检查通过」；
        #    依赖它的 VERSION/TESTS 也必须报红而不是沉默）
        expect('MUT-pyproject-unparseable',
               _sandbox(tmp, pyproject=CLEAN_PYPROJECT + 'this is not toml\n'),
               ('PYPROJECT', 'TESTS', 'VERSION'))
        # 3) 版本号只改一处 -> VERSION（两个来源的漂移）
        expect('MUT-version-drift', _sandbox(tmp, version='"9.9.9"'), ('VERSION',))
        # 4) [tool.setuptools].packages 不含包名 -> PACKAGE
        expect('MUT-packages-mismatch', _sandbox(tmp, packages='"other"'), ('PACKAGE',))
        # 5) tests/ 没有测试文件 -> TESTS
        expect('MUT-no-test-files', _sandbox(tmp, make_tests=False), ('TESTS',))
        # 6) 包目录整个没有 -> PACKAGE（连 __init__.py 也没了）
        expect('MUT-no-package-dir', _sandbox(tmp, make_pkg=False), ('PACKAGE', 'VERSION'))
        # 7) CI 里少掉门禁那一行 -> CI（CI 变成只跑 pytest = 门禁再也不执行）
        expect('MUT-ci-drops-gates',
               _sandbox(tmp, ci=CLEAN_CI.replace('      - run: python tools/run_all_gates.py\n', '')),
               ('CI',))
        # 8) CI 里一条 run: 都没有 -> CI 的空转守卫（提取为空必须判 FAIL）
        expect('MUT-ci-no-run-lines',
               _sandbox(tmp, ci='name: ci\non: [push]\njobs:\n  check:\n    steps:\n      - uses: actions/checkout@v4\n'),
               ('CI',))
        # 9) CI 里 run: 全部被注释掉 -> 同样落入空转守卫
        expect('MUT-ci-commented-out',
               _sandbox(tmp, ci='name: ci\njobs:\n  check:\n    steps:\n'
                                '#      - run: python -m pytest -q\n'
                                '#      - run: python tools/run_all_gates.py\n'),
               ('CI',))
        # 10) CI 吞退出码 -> CI-SWALLOW
        expect('MUT-ci-swallows-exit',
               _sandbox(tmp, ci=CLEAN_CI + '      - run: python tools/run_all_gates.py || true\n'),
               ('CI-SWALLOW',))
        # 10b) 干净样本（同一探测器）：注释里提到 `|| true` 不算违规，必须保持安静
        expect('CLEAN-ci-comment-mentions',
               _sandbox(tmp, ci=CLEAN_CI + '# 注意：不要写 `|| true` 或者 continue-on-error: true\n'),
               ())
        # 11) 完全没有产物的目录 -> 六个里至少 PYPROJECT/PACKAGE/TESTS/CI/VERSION 都要报
        expect('MUT-empty-root', _sandbox(tmp, pyproject='', make_pkg=False, make_tests=False,
                                          ci=None), ('CI', 'PACKAGE', 'PYPROJECT', 'TESTS',
                                                     'VERSION'))
        # 12) 输入目录根本不存在时也必须判红（而不是「没找到问题」）
        expect('MUT-nonexistent-root', os.path.join(tmp, 'no-such-root'),
               ('CI', 'PACKAGE', 'PYPROJECT', 'TESTS', 'VERSION'))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('  detectors=%d samples=%d' % (len(DETECTORS), checked))
    if failures:
        print('SELFTEST FAIL: %d sample(s) did not behave as expected' % len(failures))
        for f in failures:
            print('  - %s' % f)
        return 2
    print('SELFTEST OK: %d detector(s) all fire on their own sample, clean sample stays quiet'
          % len(DETECTORS))
    return 0


def main(argv):
    args = [a for a in argv[1:] if a != '--selftest']
    if '--selftest' in argv[1:]:
        rc = selftest()
        if rc:
            return rc
        # 自测通过后仍然要在真实仓库上跑一遍，否则「SELFTEST OK」会被读成产物也是好的。
    root = os.path.abspath(args[0]) if args else ROOT
    findings = verify(root)
    for code, msg in findings:
        print('FINDING [%s] %s' % (code, msg))
    print('root: %s' % root)
    print('denominator: %d detector(s) ran: %s' % (len(DETECTORS), ', '.join(DETECTORS)))
    print('verdict: %s (%d issue(s))' % ('PASS' if not findings else 'FAIL', len(findings)))
    return 0 if not findings else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
