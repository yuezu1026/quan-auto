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
    python tools/verify_skeleton.py --selftest  # 自证：每个探测器各自都能红
    python tools/verify_skeleton.py <root>      # 检查别的目录（用于触发测试）

Exit codes: 0 = PASS, 1 = FAIL, 2 = selftest 自身的断言失败（说明这个门禁坏了，不是产物坏了）

`--selftest` 的退出码**只报自测**（0 = 自测过，2 = 自测自己失败）。真实仓库的结论照常
打印（`FINDING` 行 + `verdict:` 行）但不进退出码 —— 门禁框架的契约是「self-test argv 必须
打印 `SELFTEST OK` 标记并 exit 0」（`tools/run_all_gates.py` 注册表说明），它另有**不带
`--selftest`** 的那一次运行专门判真实产物。2026-09-25 之前这里返回真实仓库的结论，于是仓库
一红，框架的 `rc != 0` 分支就把「真产物红了」写成 `selftest=FAIL` +「这个门禁没证明自己有牙」
—— 一句假话，而且专挑你最需要「自测还好不好」的时刻（真仓库红的时候）出现。
"""

from __future__ import annotations

import ast
import glob
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
DETECTORS = ('PYPROJECT', 'VERSION', 'PACKAGE', 'TESTS', 'CI', 'CI-SWALLOW',
             'GATE-COUNT', 'IMPL-STATUS', 'MAP-PATHS', 'DRIVER-EXTRA')

# ── 状态陈述探测器（为什么需要它们） ────────────────────────────────────────
#
# `CONTEXT.md` 是 L0 地图、是全仓库的导航入口，而在 2026-09-23 I1 落地之前，
# **没有任何门禁读它**。后果实测到了：I1 把 9 个实现模块和 2 个新门禁写完之后，
# CONTEXT.md 里三处状态陈述同时过期（「真正的实现一行都还没写」「只有 __version__,
# 没有任何实现」「6 个门禁」），而当时 8 个门禁**全绿** —— 因为没有任何判据看过那份文件。
# 规范 §6 的清单第 10 项「新增产物后回填 CONTEXT.md」一直是纯自觉项，
# 而纯自觉项一定会漂。这两个探测器就是把它变成判据。
#
# 判据的选择：**禁掉会过期的写法**，而不是「核对数字对不对」。理由三条：
#   1. 「核对数字」要求本门禁去解析 `run_all_gates.py` 的注册表 —— 让门禁互相解析
#      等于把另一个门禁的输出也变成契约（规范 §3.6 记过这个漂移源）。
#   2. 在文档里写死计数本来就是要禁的写法：CONTEXT.md §6 早已对「提交数」立了同样的
#      规矩（「一律现取 git log --oneline，不要写死数字」），只是那条一直没有判据。
#   3. 带日期的写法是**证据快照**（「2026-09-23 实测 8 个门禁」），它过期是正常的，
#      故予豁免。要禁的是不带日期的「当前状态」断言。
# 这份清单原本只喂 `MAP-PATHS`（引用必须存在），`GATE-COUNT` 也吃它；2026-09-25 起
# `GATE-COUNT` 与 `IMPL-STATUS` 的实际扫描面还要加上 `.github/**/*.md`，见 `GITHUB_MD_GLOB`。
STATE_FILES = ('CONTEXT.md',)

# 实现状态陈述要同时管四个面：地图、包元数据、包入口，以及 `.github/**/*.md`
# （前三处都写过同一句错话；第四个面是 2026-09-25 扩的，理由见 `GITHUB_MD_GLOB`）。
IMPL_STATUS_TARGETS = ('CONTEXT.md', PYPROJECT, os.path.join(PKG_NAME, '__init__.py'))

# 这些句子描述「现在有没有实现」。实现一旦存在，它们就从「当时正确」变成永久错误。
OBSOLETE_STATUS_PHRASES = (
    '没有任何实现',
    '尚无可运行实现',
    '一行都还没写',
    '一行未写',
    '一行实现都没有',
    '只有版本号的空包',
)

GATE_COUNT_RE = re.compile(r'\d+\s*个门禁')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')

# ── 扫描面：固定三处 + `.github/**/*.md`（2026-09-25 扩面） ──────────────────
#
# 扩面之前 `.github/` 是**唯一的零判据区**，而它恰恰是**自动加载层**：每次会话都会进上下文。
# 过期前提在这一层最贵。实测代价：`.github/copilot-instructions.md` 里那句
# 「I0 空骨架、没有任何业务实现」一直活到 I3b 交付之后才被人发现，期间所有门禁全绿
# （上面 IMPL_STATUS_TARGETS 的注释记着同一件事，但当时只修了三个写死的文件）。
# 2026-09-25 追加 `.github/skills/`（渐进式披露层）之后这一层变大了，再不扫就等于
# 把最贵的一块继续放在无判据区。
#
# **只扩「会过期的措辞」这一类判据，不扩 `MAP-PATHS`。** 理由：路径存在性在散文上的
# 假阳性率已在 `check_map_paths` 上方实测记过（132 个含 `/` 的 token 里就有时区
# `Asia/Shanghai` 与字段对 `effective_from/effective_to`），而 skill 正文里必然出现
# `db/**`、`applyTo`、`2>&1` 这类非地址 token ⇒ 扩过去只会把正确的文档判红十几次，
# 最后一定被人用「放宽」的方式关掉。**边界：这条路只看措辞，看不见 skill 的结构是否合法。**
GITHUB_MD_GLOB = os.path.join('.github', '**', '*.md')


def _status_targets(root, base):
    """`base` 里的固定文件 + `.github/**/*.md`（相对仓库根、稳定排序、不重复）。

    排成稳定顺序是为了让 FINDING 的次序可复现（报告要能逐字节对拍）。
    路径统一成 `/` 分隔：Windows 上 `os.path.relpath` 给的是反斜杠，会让报告随平台变。
    """
    found = list(base)
    for path in sorted(glob.glob(os.path.join(root, GITHUB_MD_GLOB), recursive=True)):
        rel = os.path.relpath(path, root).replace(os.sep, '/')
        if rel not in found:
            found.append(rel)
    return found

# ── 地图幽灵路径探测器 ─────────────────────────────────────────────────────
#
# `CONTEXT.md` 的职责是导航：它写下的每个文件路径都是一句「去这里看」。路径一旦
# 改名或被删，这句话就把人送到空地址，而**其它九个探测器全都看不见**（它们不读
# CONTEXT.md 里的路径）。这与 `verify_iteration_plan.py` 的 C4（证据路径必须存在）
# 是同一类判据 —— 那边守「已交付的证据」，这边守「地图的指路」。
#
# 覆盖面**只有 `CONTEXT.md`**，这是实测出来的边界而不是偷懒：把同一套判定扫到
# `docs/*.md` 上，132 个含 `/` 的 token 里就有 `Asia/Shanghai`（IANA 时区）、
# `effective_from/effective_to`（字段对）这种假阳性，裸 token 更是有 50 多个
# （`DataCenter.as_of()`、`pd.read_csv`、`0.10`、`common.proto`…）。一个会把正确
# 文档判红十几次的探测器，最后一定会被人用「放宽」的方式关掉。
KNOWN_EXTS = ('.py', '.md', '.json', '.toml', '.yml', '.yaml', '.sql', '.txt',
              '.csv', '.docx', '.cfg', '.ini', '.proto', '.html')
# 遍历时跳过的目录 —— 它们都是**本机 / 工具产物**，不是仓库内容（`.gitignore` 忽略，
# 或由 pytest / npm 随时生成）。这一份清单同时被两处用，成员故意完全相同：
#   - `_repo_basenames`：遍历时不看它们（既慢，又会把「同名文件恰好存在」变成巧合）；
#   - `_classify_ref`：地图提到它们**不算仓库地址**（见 LOCAL_ONLY_DIRS）。
SKIP_WALK_DIRS = frozenset(('.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules'))
LOCAL_ONLY_DIRS = SKIP_WALK_DIRS
BACKTICK_RE = re.compile(r'`([^`\n]+)`')


def read_text(path):
    """返回规范化后的文本（BOM 去掉、CRLF -> LF），读不到返回 None。

    必须先规范化再匹配：本仓库的 md/sql 可能是 CRLF，裸 `\\n` 正则对 CRLF 会静默失配，
    于是所有探测器一起空转却打印 PASS（规范 §3.5 第 3 条：提取为空必须判 FAIL）。
    （原文写的是「§六 案例一」，而规范 §6 是改动清单、里面没有案例 —— 引用指向了不存在的小节。）
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


# ── 驱动声明探测器 ──────────────────────────────────────────────────────────
#
# 2026-09-25 裁决：`psycopg` 写进 `[project.optional-dependencies] postgres`。但
# 「怎么装驱动」这件事有**两处手写副本**：pyproject 里的声明下限，与
# `quanauto/pgstore.py` 报错文案里教用户敲的那条命令。两处漂开不会有任何东西报红，
# 而用户照着提示装出来的东西与声明的不是一回事。`[datasources]` extra 至今也是这个
# 状态（没有判据），这条探测器至少把驱动那一半钉住。
#
# 判据形状三条，缺一即 FAIL（第三条是空转守卫）：
#   1. `[project.optional-dependencies].postgres` 存在，且里面有需求名**恰好**是
#      `psycopg` 的条目 —— 不能用子串命中，否则 `notpsycopg` 也会被当成声明；
#   2. `quanauto/pgstore.py` 里能提取到安装提示（提取为空时下面那条比较永远不开火）；
#   3. 两处的版本下限是同一串。
# 名字解析故意不引 `packaging`：`tools/` 只用标准库（规范 §5 红线清单），而 extra 的写法是
# 本仓库自己定的、形状可控，正则够用。
DRIVER_EXTRA = 'postgres'
DRIVER_REQUIREMENT = 'psycopg'
DRIVER_MODULE = os.path.join(PKG_NAME, 'pgstore.py')
HINT_RE = re.compile(r"pip install '([A-Za-z0-9_.\-]+)>=([0-9][0-9.]*)'")
FLOOR_RE = re.compile(r'>=[ \t]*([0-9][0-9.]*)')


def _requirement_name(spec):
    """从 PEP 508 片段里抠出需求名：`psycopg[binary]>=3.1` -> `psycopg`。"""
    m = re.match(r'[ \t]*([A-Za-z0-9_.\-]+)', str(spec))
    if not m:
        return ''
    return m.group(1).replace('_', '-').lower()


def check_driver_extra(root):
    """驱动既要有声明位置，也要有一条和它下限一致的安装提示。"""
    findings = []
    shown = DRIVER_MODULE.replace('\\', '/')
    text = read_text(os.path.join(root, DRIVER_MODULE))
    _, data = _load_pyproject(root)
    declared = []
    if data is None:
        findings.append(('DRIVER-EXTRA',
                         '%s 读不到或不可解析 —— 驱动声明的下限无从核对（不是「一致」）'
                         % PYPROJECT))
    else:
        extras = data.get('project', {}).get('optional-dependencies') or {}
        specs = extras.get(DRIVER_EXTRA)
        if not specs:
            findings.append(('DRIVER-EXTRA',
                             '%s 的 [project.optional-dependencies] 里没有 %r —— %s 惰性导入的 '
                             '%s 没有声明位置，文案教的装法无法从依赖图复现'
                             % (PYPROJECT, DRIVER_EXTRA, shown, DRIVER_REQUIREMENT)))
        else:
            for spec in specs:
                if _requirement_name(spec) == DRIVER_REQUIREMENT:
                    m = FLOOR_RE.search(str(spec))
                    declared.append(m.group(1) if m else '')
            if not declared:
                findings.append(('DRIVER-EXTRA',
                                 '%s 的 %r extra=%r 里没有需求名恰好是 %r 的条目'
                                 % (PYPROJECT, DRIVER_EXTRA, specs, DRIVER_REQUIREMENT)))
    if text is None:
        findings.append(('DRIVER-EXTRA',
                         '%s 不存在 —— 取不到安装提示，与 %s 的声明无从互相印证'
                         % (shown, PYPROJECT)))
        return findings
    hints = HINT_RE.findall(text)
    if not hints:
        # 空转守卫：提取为空时「下限一致」那一半永远不会开火，却会打印 PASS。
        findings.append(('DRIVER-EXTRA',
                         '%s 里提取不到安装提示（期望 `pip install \'%s>=X.Y\'` 这种形状）—— '
                         '提取为空时下限比较形同虚设，拒绝通过'
                         % (shown, DRIVER_REQUIREMENT)))
        return findings
    hinted = sorted({floor for name, floor in hints if name == DRIVER_REQUIREMENT})
    if not hinted:
        findings.append(('DRIVER-EXTRA',
                         '%s 的安装提示提到的是别的包（%s），没有 %r'
                         % (shown, ', '.join(sorted({n for n, _ in hints})), DRIVER_REQUIREMENT)))
    elif declared and hinted != sorted(set(declared)):
        findings.append(('DRIVER-EXTRA',
                         '版本下限两处不一致：%s 的 %r extra 写 %s，而 %s 教用户装的写 %s'
                         % (PYPROJECT, DRIVER_EXTRA, ', '.join(sorted(set(declared))), shown,
                            ', '.join(hinted))))
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


def _impl_modules(root):
    """包目录里除 `__init__.py` 之外的 `.py` —— 它们是「已经有实现」的凭证。"""
    pkg_dir = os.path.join(root, PKG_NAME)
    if not os.path.isdir(pkg_dir):
        return []
    try:
        entries = os.listdir(pkg_dir)
    except OSError:
        return []
    return sorted(f for f in entries if f.endswith('.py') and f != '__init__.py')


def check_gate_count(root):
    """文档里不许写死**不带日期**的门禁数量 —— 那个数字每轮都会过期。"""
    findings = []
    for name in _status_targets(root, STATE_FILES):
        text = read_text(os.path.join(root, name))
        if text is None:
            # 文件不存在不是这个探测器的职责（CONTEXT.md 缺失自有别的判据去管），
            # 但静默跳过要看得见：下面 main() 会打出探测器分母。
            continue
        for i, line in enumerate(text.split('\n'), 1):
            m = GATE_COUNT_RE.search(line)
            if m and not DATE_RE.search(line):
                findings.append(
                    ('GATE-COUNT',
                     '%s:%d 写死了门禁数量（%r）—— 门禁数每轮都会变，这个数字必然过期，'
                     '而且没人会在加门禁时想起它。正确写法是「门禁数现取 '
                     'python tools/run_all_gates.py --list」；若要记一次实测结果，必须带上日期。'
                     % (name, i, m.group(0))))
    return findings


def check_impl_status(root):
    """实现已经存在时，状态陈述里不许再说「没有任何实现」。"""
    findings = []
    impl = _impl_modules(root)
    if not impl:
        return findings  # 此刻说「还没有实现」是**对的**，报红反而是假阳性
    for name in _status_targets(root, IMPL_STATUS_TARGETS):
        text = read_text(os.path.join(root, name))
        if text is None:
            continue
        for i, line in enumerate(text.split('\n'), 1):
            # 一行只报第一个命中的短语：同一行里「一行都还没写」与「只有版本号的空包」
            # 常常连写，逐短语报会得到两条同源 FINDING，看着像两个问题。
            hit = next((p for p in OBSOLETE_STATUS_PHRASES if p in line), None)
            if hit is not None:
                findings.append(
                    ('IMPL-STATUS',
                     '%s:%d 写着「%s」，但 %s/ 下已有 %d 个实现模块（%s）—— '
                     '状态陈述已过期，属于文档漂移'
                     % (name, i, hit, PKG_NAME, len(impl), ', '.join(impl[:4]))))
    return findings


def _repo_basenames(root):
    """仓库里所有**文件名**（不含目录），用来解析散文里那种裸文件名。

    跳过大目录（`.git` / `.venv` / `__pycache__` 等）：它们既慢，又会让「同名文件恰好
    存在」变成廉价的巧合。判定要落在人写的文件上。
    """
    found = set()
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_WALK_DIRS]
        found.update(files)
    return found


def _classify_ref(tok):
    """把一个反引号 token 判成 glob / slash / bare / skip。

    判据必须在**文件系统之外**就能定下来：如果「像不像路径」取决于「它存不存在」，
    坏掉的输入就会自己决定检查范围（存在 ⇒ 不是路径 ⇒ 不用查），这是循环论证。

    这里用 `os.path.splitext` 而不是 `str.endswith`：`splitext('.md') == ('.md', '')` ——
    前导点被当作隐藏文件名，于是**纯扩展名**（`| .md（产物，勿原地改） | .docx（源） |`
    这种表头）自动落进 skip。用 `endswith` 会把两个表头当成两条幽灵路径（实测踩到）。
    代价是隐藏文件（`.gitignore`）也不在覆盖内，这是明知的选择。

    首段落在 `LOCAL_ONLY_DIRS` 时同样 skip：地图里的 `.venv/` 是「在你机器的哪里」，
    不是「仓库里的地址」—— CONTEXT.md 自己就写着它**不入库**，本探测器的遍历也把它
    排除在外。要求它必须在仓库里，等于同一个探测器同时断言「不是仓库内容」与
    「必须在仓库里」。2026-09-24 首次真跑 GitHub CI 时正是 `.venv/` 把 skeleton 判红：
    本机绿、克隆红 —— **判据依赖环境就是判据的缺陷**。
    豁免范围只能是这一份本机产物清单（样本 `MUT-map-ghost-slash` 守着它没被放宽）。
    """
    if not tok or ' ' in tok or '\t' in tok or tok.startswith('-'):
        return 'skip'
    if any(c in tok for c in '*<{'):
        return 'glob'
    if '/' in tok:
        return 'skip' if tok.split('/')[0] in LOCAL_ONLY_DIRS else 'slash'
    return 'bare' if os.path.splitext(tok)[1].lower() in KNOWN_EXTS else 'skip'


def check_map_paths(root):
    """L0 地图里的每个文件引用都必须真的存在 —— 防「地图把人送到空地址」。

    扫描面**故意只用 `STATE_FILES`**（= `CONTEXT.md`），不吃 `_status_targets` 加的
    `.github/**/*.md`：理由见 `GITHUB_MD_GLOB` 上方那段实测记录（散文里满是时区名、
    字段对、命令行片段这类非地址 token，当路径查会十几次误报）。
    `MUT-map-*` 样本守着这条没被放宽。
    """
    findings = []
    for name in STATE_FILES:
        text = read_text(os.path.join(root, name))
        if text is None:
            continue  # 文件不存在不是这个探测器的职责（同 GATE-COUNT）
        toks = BACKTICK_RE.findall(text)
        refs = sorted({t for t in toks if _classify_ref(t) != 'skip'})
        if not refs:
            # 空转守卫：一个文件引用都提取不到时，下面所有判定都会落空而报告仍然干净。
            # 推论：**每个样本的 CONTEXT.md 都得带至少一个真引用**，否则别的探测器
            # 的样本会连带报两条码，看不出到底是谁命中。
            findings.append(('MAP-PATHS',
                             '%s 里没提取到任何文件引用（反引号 %d 个）—— '
                             '路径检查形同虚设，拒绝通过' % (name, len(toks))))
            continue
        basenames = _repo_basenames(root)
        for tok in refs:
            kind = _classify_ref(tok)
            if kind == 'slash':
                if not os.path.exists(os.path.join(root, *tok.split('/'))):
                    findings.append(('MAP-PATHS',
                                     '%s 指向 `%s`，但仓库里没有这个路径' % (name, tok)))
            elif kind == 'bare':
                if tok not in basenames:
                    findings.append(('MAP-PATHS',
                                     '%s 指向 `%s`，但仓库里没有任何同名文件' % (name, tok)))
            else:  # glob
                if not (glob.glob(os.path.join(root, tok))
                        or glob.glob(os.path.join(root, '**', tok), recursive=True)):
                    findings.append(('MAP-PATHS',
                                     '%s 里的通配 `%s` 匹配不到任何文件' % (name, tok)))
    return findings


DETECTOR_FUNCS = {
    'PYPROJECT': check_pyproject,
    'VERSION': check_version,
    'PACKAGE': check_package,
    'TESTS': check_tests,
    'CI': check_ci,
    'CI-SWALLOW': check_ci_swallow,
    'GATE-COUNT': check_gate_count,
    'IMPL-STATUS': check_impl_status,
    'MAP-PATHS': check_map_paths,
    'DRIVER-EXTRA': check_driver_extra,
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

[project.optional-dependencies]
postgres = ["psycopg[binary]>=3.1"]

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

# 驱动安装提示的替身：真文件里那句话长得多，这里只要能被 HINT_RE 提取到就够。
# 故意写成 ASCII，免得样例输出依赖控制台编码。
DRIVER_HINT_OK = ('def _import_psycopg():\n'
                  '    raise InvalidConfigError("driver: pip install \'psycopg>=3.1\'")\n')


def _sandbox(tmp, *, pyproject=CLEAN_PYPROJECT, version='"1.2.3"', ci=CLEAN_CI,
             packages='"quanauto"', make_pkg=True, make_tests=True,
             context_md=None, impl_modules=(), driver_hint=DRIVER_HINT_OK,
             github_md=None):
    """造一个最小仓库。每个样本只动一处，其余保持干净 —— 这样报出来的必定是那一处。

    `github_md` 是 `{'.github' 下的相对路径: 文本}`，用来构造 `.github/**/*.md` 扫描面的样本。
    """
    root = tempfile.mkdtemp(dir=tmp)
    with open(os.path.join(root, PYPROJECT), 'w', encoding='utf-8', newline='\n') as fp:
        fp.write(pyproject.replace('packages = ["quanauto"]', 'packages = [%s]' % packages))
    if make_pkg:
        os.makedirs(os.path.join(root, PKG_NAME))
        with open(os.path.join(root, PKG_NAME, '__init__.py'), 'w',
                  encoding='utf-8', newline='\n') as fp:
            fp.write('"""fake package."""\n\n__version__ = %s\n' % version)
        for mod in impl_modules:
            with open(os.path.join(root, PKG_NAME, mod), 'w',
                      encoding='utf-8', newline='\n') as fp:
                fp.write('"""fake impl."""\n')
        if driver_hint is not None:
            with open(os.path.join(root, PKG_NAME, os.path.basename(DRIVER_MODULE)), 'w',
                      encoding='utf-8', newline='\n') as fp:
                fp.write(driver_hint)
    if context_md is not None:
        with open(os.path.join(root, 'CONTEXT.md'), 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(context_md)
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
    for rel, text in (github_md or {}).items():
        path = os.path.join(root, '.github', *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(text)
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
        # 1) pyproject 整个消失 -> PYPROJECT（VERSION/TESTS/DRIVER-EXTRA 也会连带报，
        #    因为它们）的判据都依赖 pyproject 里的声明 —— 「读不到」必须报红，不得
        #    静默当成干净）
        root = _sandbox(tmp)
        os.remove(os.path.join(root, PYPROJECT))
        expect('MUT-no-pyproject', root,
               ('DRIVER-EXTRA', 'PYPROJECT', 'TESTS', 'VERSION'))
        # 2) pyproject 语法坏掉 -> PYPROJECT（解析失败不得被读成「检查通过」；
        #    依赖它的 VERSION/TESTS 也必须报红而不是沉默）
        expect('MUT-pyproject-unparseable',
               _sandbox(tmp, pyproject=CLEAN_PYPROJECT + 'this is not toml\n'),
               ('DRIVER-EXTRA', 'PYPROJECT', 'TESTS', 'VERSION'))
        # 3) 版本号只改一处 -> VERSION（两个来源的漂移）
        expect('MUT-version-drift', _sandbox(tmp, version='"9.9.9"'), ('VERSION',))
        # 4) [tool.setuptools].packages 不含包名 -> PACKAGE
        expect('MUT-packages-mismatch', _sandbox(tmp, packages='"other"'), ('PACKAGE',))
        # 5) tests/ 没有测试文件 -> TESTS
        expect('MUT-no-test-files', _sandbox(tmp, make_tests=False), ('TESTS',))
        # 6) 包目录整个没有 -> PACKAGE（连 __init__.py 也没了）
        expect('MUT-no-package-dir', _sandbox(tmp, make_pkg=False),
               ('DRIVER-EXTRA', 'PACKAGE', 'VERSION'))
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
                                          ci=None), ('CI', 'DRIVER-EXTRA', 'PACKAGE',
                                                     'PYPROJECT', 'TESTS', 'VERSION'))
        # 12) 输入目录根本不存在时也必须判红（而不是「没找到问题」）
        expect('MUT-nonexistent-root', os.path.join(tmp, 'no-such-root'),
               ('CI', 'DRIVER-EXTRA', 'PACKAGE', 'PYPROJECT', 'TESTS', 'VERSION'))
        # 13) 实现已存在，CONTEXT.md 还写着「一行都还没写」-> IMPL-STATUS
        #     ⚠️ 每个样本的 CONTEXT.md 都必须带**至少一个能解析的文件引用**，否则
        #     MAP-PATHS 的空转守卫会一起开火；那样报出来是两个码，就看不出 IMPL-STATUS
        #     到底有没有真的命中（「变异没打到分支」与「探测器不存在」长得一样）。
        expect('MUT-impl-status-stale',
               _sandbox(tmp, impl_modules=('broker.py',),
                        context_md='契约、DDL、门禁 —— 真正的实现一行都还没写。见 `pyproject.toml`。\n'),
               ('IMPL-STATUS',))
        # 13b) 同一句话，但**还没有实现模块** -> 此刻它是对的，必须保持安静（防误报）
        #      ⚠️ 这里必须显式 `driver_hint=None`：默认沙箱为了 DRIVER-EXTRA 会放一个
        #      `pgstore.py`，而它本身就是「一个实现模块」⇒ 不拿掉就构造不出「零实现模块」
        #      这个前置条件。拿掉之后 DRIVER-EXTRA 会**理应**开火（那个文件确实不见了），
        #      本条真正断言的是 IMPL-STATUS **没有**跟着开火。
        expect('CLEAN-impl-status-accurate',
               _sandbox(tmp, driver_hint=None,
                        context_md='契约、DDL、门禁 —— 真正的实现一行都还没写。见 `pyproject.toml`。\n'),
               ('DRIVER-EXTRA',))
        # 13c) 同一个过期陈述写在 pyproject description 里 -> 同样被抓（三处都要管）
        expect('MUT-impl-status-in-description',
               _sandbox(tmp, impl_modules=('broker.py',),
                        pyproject=CLEAN_PYPROJECT.replace(
                            'version = "1.2.3"',
                            'version = "1.2.3"\ndescription = "骨架阶段，尚无可运行实现"')),
               ('IMPL-STATUS',))
        # 14) CONTEXT.md 写死门禁数量 -> GATE-COUNT
        expect('MUT-gate-count-hardcoded',
               _sandbox(tmp, context_md='现在有 6 个门禁，5 个 tier-A 全绿。见 `pyproject.toml`。\n'),
               ('GATE-COUNT',))
        # 14b) 带日期的实测快照 -> 豁免（它是证据不是状态断言，过期属正常）
        expect('CLEAN-gate-count-dated',
               _sandbox(tmp, context_md='2026-09-23 实测 6 个门禁。见 `pyproject.toml`。\n'),
               ())
        # 15) 地图指向不存在的路径 -> MAP-PATHS（先验「根相对路径」这一支）
        expect('MUT-map-ghost-slash',
               _sandbox(tmp, context_md='细节见 `tools/verify_nonexistent.py`。\n'),
               ('MAP-PATHS',))
        # 15b) 同一个幽灵写成**裸文件名**（散文里的 `xxx.py`）-> 走「仓库里有同名文件」那一支
        expect('MUT-map-ghost-bare',
               _sandbox(tmp, context_md='细节见 `verify_nonexistent.py`。\n'),
               ('MAP-PATHS',))
        # 15c) 通配符什么都匹配不到 -> MAP-PATHS（第三支）
        expect('MUT-map-glob-empty',
               _sandbox(tmp, context_md='实现都在 `quanauto/*.zig` 里。\n'),
               ('MAP-PATHS',))
        # 15d) 空转守卫：有反引号但**一个文件引用都没有** -> MAP-PATHS 拒绝通过
        expect('GATE-map-no-refs',
               _sandbox(tmp, context_md='跑 `--list` 看注册表，或读 `git log --oneline`。\n'),
               ('MAP-PATHS',))
        # 15e) 干净样本：真引用能解析、纯扩展名表头（`.md` / `.docx`）不算路径
        #      —— 后半个断言是实测踩过的假阳性：`str.endswith` 会把两个表头
        #      当成两条幽灵路径，所以改用 `os.path.splitext`。
        expect('CLEAN-map-paths-ok',
               _sandbox(tmp, context_md='表头 `.md` / `.docx` 不是路径；见 `pyproject.toml`、'
                                        '`quanauto/*.py` 与 `quanauto/__init__.py`。\n'),
               ())
        # 15f) 干净样本：地图提到**本机产物**（`.venv/`）不算幽灵路径。
        #      ⚠️ 这条样本是 2026-09-24 首次真跑 GitHub CI 抓到的：本机有 `.venv/`，
        #      克隆里没有 ⇒ 同一个 commit 在同一份判据下本机绿、CI 红。判据依赖环境
        #      就是判据的缺陷（CONTEXT.md 自己写着 `.venv/` **不入库**，本探测器的
        #      SKIP_WALK_DIRS 也把它排除在遍历外 —— 两处都说「不是仓库内容」，
        #      只有存在性那一支要求它在仓库里，是自相矛盾）。
        expect('CLEAN-map-local-machinery',
               _sandbox(tmp, context_md='本机虚拟环境在 `.venv/`（不入库）；'
                                        '依赖清单见 `pyproject.toml`。\n'),
               ())
        # 16) 「一行实现都没有」是同族里第 6 种写法 -> 措辞表要收得住变体
        expect('MUT-impl-status-no-line-impl',
               _sandbox(tmp, impl_modules=('broker.py',),
                        context_md='依赖清单是空的：现在一行实现都没有。见 `pyproject.toml`。\n'),
               ('IMPL-STATUS',))
        # 17) 驱动声明 ↔ 安装提示（2026-09-25 裁决后补）
        #     17a) extra 整个消失 -> DRIVER-EXTRA（声明位置没了）
        expect('MUT-driver-extra-missing',
               _sandbox(tmp, pyproject=CLEAN_PYPROJECT.replace(
                   'postgres = ["psycopg[binary]>=3.1"]\n', '')),
               ('DRIVER-EXTRA',))
        #     17b) 两处下限漂开（声明 3.2 / 提示 3.1）—— 照提示装出来的与声明的不是一回事
        expect('MUT-driver-floor-drift',
               _sandbox(tmp, pyproject=CLEAN_PYPROJECT.replace(
                   'psycopg[binary]>=3.1', 'psycopg[binary]>=3.2')),
               ('DRIVER-EXTRA',))
        #     17c) 空转守卫：源码里提取不到安装提示 -> 拒绝通过（而不是「比较通过」）
        expect('GATE-driver-hint-gone',
               _sandbox(tmp, driver_hint='def _import_psycopg():\n    raise RuntimeError("boom")\n'),
               ('DRIVER-EXTRA',))
        #     17d) extra 里声明的是别的包（`notpsycopg`）-> DRIVER-EXTRA。
        #         本条是「名字要恰好相等」的主人：子串命中（`'psycopg' in spec`）会把
        #         `notpsycopg>=3.1` 当成合法声明而放行，所以它必须报红。
        expect('MUT-driver-extra-wrong-pkg',
               _sandbox(tmp, pyproject=CLEAN_PYPROJECT.replace(
                   'psycopg[binary]>=3.1', 'notpsycopg>=3.1')),
               ('DRIVER-EXTRA',))
        # 18) `.github/**/*.md` 也在扫描面内（2026-09-25 扩面）
        #     这一层是**自动加载层**，原先零判据：写过期状态陈述、写死门禁数量都不会被抓。
        #     18a) skill 正文里写死**不带日期**的门禁数量 -> GATE-COUNT
        expect('MUT-github-md-gate-count',
               _sandbox(tmp, github_md={'skills/foo/SKILL.md':
                                        '# foo\n\n当前共 6 个门禁。\n'}),
               ('GATE-COUNT',))
        #     18b) skill 正文里写着过期的实现状态 -> IMPL-STATUS
        expect('MUT-github-md-impl-status',
               _sandbox(tmp, github_md={'skills/foo/SKILL.md':
                                        '# foo\n\n注意：真正的实现一行都还没写。\n'}),
               ('IMPL-STATUS',))
        #     18c) 二级文件（skill 的 references/）也必须扫到 -> IMPL-STATUS。
        #         本条守着通配的**递归**那一段：若写成 `.github/*/*.md`，这个样本会静默
        #         变绿，而“探测器不在”与“变异没打到”在报告里长得一模一样。
        expect('MUT-github-md-nested',
               _sandbox(tmp, github_md={'skills/foo/references/traps.md':
                                        '# traps\n\n真正的实现一行都还没写。\n'}),
               ('IMPL-STATUS',))
        #     18d) 干净样本（防误报）：带日期的实测快照 + 「现取」写法必须保持安静
        expect('CLEAN-github-md-dated',
               _sandbox(tmp, github_md={'skills/foo/SKILL.md':
                                        '# foo\n\n2026-09-23 实测 6 个门禁；'
                                        '现在的数量现取 `--list`。\n'}),
               ())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 退出码归属：`--selftest` 只报自测（框架契约）。这条不能用 expect() 测 ——
    # 它管的是**进程退出码**，而 samples 全在内存里。真实仓库那一次 e2e 实测记在
    # `selftest_exit_code` 的注释里。
    rc_cases = (((0, 1, True), 0), ((0, 1, False), 1), ((0, 0, True), 0), ((2, 0, True), 2))
    for rc_args, rc_want in rc_cases:
        rc_got = selftest_exit_code(*rc_args)
        if rc_got != rc_want:
            failures.append('selftest_exit_code%s -> %s, want %s'
                            % (rc_args, rc_got, rc_want))
    print('  exit-code-owner: %d case(s) checked' % len(rc_cases))

    print('  detectors=%d samples=%d' % (len(DETECTORS), checked))
    if failures:
        print('SELFTEST FAIL: %d sample(s) did not behave as expected' % len(failures))
        for f in failures:
            print('  - %s' % f)
        return 2
    print('SELFTEST OK: %d detector(s) all fire on their own sample, clean sample stays quiet'
          % len(DETECTORS))
    return 0


def selftest_exit_code(selftest_rc, real_rc, selftest_mode):
    """谁是退出码的主人。

    自测模式（`--selftest`）下退出码只报自测结果，真实仓库的结论不进退出码。
    非自测模式下退出码就是真实仓库的结论。

    e2e 实测（2026-09-25，手工一次）：把 `CONTEXT.md` 拷到临时根里制造一条
    `MAP-PATHS` FINDING，再跑 `python tools/verify_skeleton.py --selftest <该根>` ——
    输出里有 `SELFTEST OK` + `FINDING [MAP-PATHS]`（那个根里只有 CONTEXT.md，
    所以另有 7 条缺文件类 FINDING）+ `verdict: FAIL (8 issue(s))` 而 `rc=0`；
    不带 `--selftest` 跑同一个根则 `rc=1`。这正是框架需要的形状。
    """
    return selftest_rc if selftest_mode else real_rc


def main(argv):
    args = [a for a in argv[1:] if a != '--selftest']
    selftest_mode = '--selftest' in argv[1:]
    if selftest_mode:
        rc = selftest()
        if rc:
            return rc
        # 自测通过后仍然要在真实仓库上跑一遍，否则「SELFTEST OK」会被读成产物也是好的。
        # 但退出码只报自测 —— 见 selftest_exit_code。
    root = os.path.abspath(args[0]) if args else ROOT
    findings = verify(root)
    for code, msg in findings:
        print('FINDING [%s] %s' % (code, msg))
    print('root: %s' % root)
    print('denominator: %d detector(s) ran: %s' % (len(DETECTORS), ', '.join(DETECTORS)))
    # 扩面后的可见分母：这一层一个文件都没扫到时，上面的 PASS 只覆盖了固定三处。
    # 不报 FAIL 的理由：`.github/` 下没有任何 markdown 的仓库是合法的（固定三处仍在扫），
    # 而本仓库一定有（copilot-instructions.md）—— 所以打印出来就够，写死不写死的样本
    # （MUT-github-md-*）才是守着这个通配真的生效的人。
    gh_md = _status_targets(root, ())
    print('status scan: fixed 3 + %d .github markdown file(s): %s'
          % (len(gh_md), ', '.join(gh_md) if gh_md else '(none)'))
    print('verdict: %s (%d issue(s))' % ('PASS' if not findings else 'FAIL', len(findings)))
    real_rc = 0 if not findings else 1
    if selftest_mode:
        print('note: --selftest 的退出码只报自测；上面真实仓库的结论是 %s，'
              '由不带 --selftest 的那一次运行负责判。'
              % ('PASS' if real_rc == 0 else 'FAIL'))
    return selftest_exit_code(0, real_rc, selftest_mode)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
