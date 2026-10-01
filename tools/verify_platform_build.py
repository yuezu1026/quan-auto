#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台层运行侧：在**仓库外**的沙箱里真跑一次后端用例与前端构建，并核对两侧的接缝。

为什么要有这条门禁
------------------
`docs/开发工作流规范.md` §7 曾挂着一条欠账。下面是**当时的原话**（2026-09-30 起它的
前半句已由本脚本关闭，§7 与 `CONTEXT.md` 同批改成「运行侧已由 `platform-runtime`
覆盖」—— 引用旧措辞时必须带上这个日期，否则读的人会以为它还在挂账）：
「展示文本只有手动脚本 (`platform/check_text_parity.py`)，`mvn test`、`npm run build`、
页面本身**零覆盖**」。挂着的账不会自己消失 —— 只有把它变成机器判据才会。本脚本把
其中两件（后端用例、前端构建）变成一条能自动跑、会红、也能被证伪的判据；
「两侧展示文本是否一致」这句**已经不成立**
（2026-10-01：`platform-text-parity` 门禁 + `FormatParityTest` 把两侧的显示规则逐条对拍
—— 订正记录见 `platform/README.md`「对拍脚本」一节，那里有差分出来的量级）。

⚠️ 订正（2026-10-01 晚）：上面那句「**页面本身**仍然没有门禁」也**已经过期**。
第二步 `npm run test` 在沙箱里用 vitest + jsdom 把 `platform/web/src/App.jsx` **真的挂起来**
（`platform/web/test/render.test.jsx`），于是「页面上印了什么」第一次有了机器判据。
**仍然没有判据的是两件**：① **真浏览器里的布局 / 视觉**（jsdom 没有布局引擎，
任何元素的 rect 恒为 0×0 ⇒ 「没有元素溢出视口」「没有元素重叠」这类断言恒真，是假绿
—— 所以那条测试文件里**不许**出现矩形判据，它自己带一条 witness 用例写着这件事）；
② **活着的服务端到底下发了什么**（那仍然只有手动脚本 `platform/check_text_parity.py`）。
本脚本仍然不启动 JVM 之外的任何进程，也仍然不打开浏览器。

为什么在沙箱里跑，而不是原地跑
------------------------------
`tools/run_all_gates.py` 有一条**副作用指纹**判据：「门禁不许写仓库，untracked 的
垃圾也算」。原地跑的话三条路都会撞上它：`mvn test` 写 `platform/**/target/`（27 个
文件）、`npm ci` 重造 `platform/web/node_modules/`（364 个文件）、`vite build` 写
`platform/api/src/main/resources/static/`。要让原地跑变绿，就得把它们加进
`FINGERPRINT_SKIP_DIRS` 的豁免里 —— 而「判据一红就放宽判据」正是本仓库明令禁止的
动作（`docs/开发工作流规范.md` 纪律一）。所以这里反过来做：把 `platform/` 复制到
%TEMP% 下的沙箱，在沙箱里跑，**仓库一个字节都不写**，那条副作用判据一个字都不用改。

沙箱里要多放三样东西（三样都是实测换来的，不是猜的）
--------------------------------------------------
1. `platform/`，但**去掉** `node_modules/` `target/` `dist/` 与静态产物目录 ——
   那三个正是本判据要**重新生成**的东西，带过去等于让判据看着上一次的产物通过。
2. `.rounds/i1/`（仓库根目录下那三个**已入库**的报告样本，`git ls-files` 可见）。
3. 一个**空的 `.git` 目录**。这一条最不直觉，但它是必须的：
   `ReportCatalog.resolve()` 的注释写着它「从工作目录往上找 `.git`」，再以**仓库根**
   解析相对路径 —— 也就是说 `ReportCatalog(".rounds/i1")` 在仓库里读的是
   `<repo>/.rounds/i1`，**不是** `<repo>/platform/api/.rounds/i1`。沙箱里没有 `.git`
   时它退回「相对工作目录」的兜底分支，于是两个用例会以
   `ReportNotFoundException（目录 <sandbox>/api/.rounds/i1）` 变红。第一次实测就是
   这么红的，这个占位目录是那次红换来的。
   副产物：这条假设一旦被改掉（比如 `resolve()` 改成读环境变量），本判据会**响亮地**
   红在 `PB-MVN-TESTS-RED` 上，不会静默通过。

判据（每条都配样本，见 `--selftest`）
------------------------------------
* `PB-STEP-NOT-RUN`      四步里有一步没跑（空转守卫：少跑一步不能读成通过）
* `PB-SANDBOX-EMPTY`     沙箱是空的（复制失败 ⇒ 后面所有断言都在真空里跑）
* `PB-SANDBOX-MISSING`   沙箱里缺必需文件（pom / package.json / lock / vite 配置 / 渲染夹具）
* `PB-SUREFIRE-NOT-PARSED`  maven 输出里没有 surefire 汇总行 ⇒ 提取为空，后面的
                             测试计数全部形同虚设（这条必须判 FAIL，不能判「0 个测试」
                             以外的东西，否则「没提取到」与「提取到了 0」长得一样）
* `PB-MVN-TESTS-ZERO`     surefire 跑了 0 个用例
* `PB-MVN-TESTS-RED`      failures/errors 非 0
* `PB-MVN-TESTS-COUNT`    surefire 跑的用例**少于**源码里声明的 `@Test` 个数
                          （防「悄悄只跑了一部分」）
* `PB-DECLARED-UNREADABLE` 数不出源码里的 `@Test` ⇒ 上面那条比较失去分母，判 FAIL
* `PB-MVN-FAIL`           maven 退出码非 0（与用例红是两件事，分开报）
* `PB-NPM-CI-FAIL`        `npm ci` 非 0（依赖装不上，后面 build 的绿没有意义）
* `PB-NPM-BUILD-FAIL`     `npm run build` 非 0
* `PB-VITEST-NOT-PARSED`  vitest 输出里没有 `Tests` 汇总行 ⇒ 提取为空（与 surefire
                          那条同形的空转守卫，理由一样）
* `PB-NPM-TESTS-ZERO`     vitest 报的总用例数是 0
* `PB-NPM-TESTS-COUNT`    vitest 跑的总数**少于** `platform/web/test` 里声明的 `it(` 个数
* `PB-JS-DECLARED-UNREADABLE`  数不出 JS 用例 ⇒ 上面那条失去分母，判 FAIL
* `PB-NPM-TEST-FAIL`      `npm run test` 非 0（渲染测试红了）
* `PB-STATIC-EMPTY`       构建说成功，却没落下 `index.html` / `assets/` 里的产物
                          （「构建成功」与「产出了东西」是两件事）
* `PB-STITCH-PREEXISTING`  跑 maven **之前** `target/classes/static/` 就存在 ⇒ 下面那条
                          接缝判据会看着旧产物通过（守卫的守卫）
* `PB-STITCH-MISSING`     前端产物没有出现在后端**真的会托管**的那个目录
                          （`platform/api/target/classes/static/`）。这一条钉的是
                          `platform/README.md` 与 `platform/web/vite.config.js` 里
                          写着的那个**构建顺序陷阱**：先 `npm run build` 再 `mvn`，
退出码：0 = PASS，1 = FAIL（有 ISSUE），**3 = SKIPPED（工具链不全，没跑）**。
3 与 1 必须分开：`run_all_gates.py` 把 3 读成「这条门禁没跑」，写进报告的汇总结论是
`INCOMPLETE`、退出码非 0 —— 也就是说 **SKIPPED 不算绿**，只是它不是「判据红了」而已。

用法：
  python tools/verify_platform_build.py             # 真跑（沙箱）
  python tools/verify_platform_build.py --selftest  # 纯函数 + 自造 fixture 的样本
  python tools/verify_platform_build.py --keep      # 留着沙箱，便于肉眼查
  python tools/verify_platform_build.py --repo-root=PATH   # 只给自测用：对着 fixture 跑
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLATFORM = 'platform'
WEB = os.path.join('platform', 'web')
ROUNDS = os.path.join('.rounds', 'i1')
STATIC_REL = os.path.join('platform', 'api', 'src', 'main', 'resources', 'static')
SERVED_REL = os.path.join('platform', 'api', 'target', 'classes', 'static')
JAVA_TEST_REL = os.path.join('platform', 'api', 'src', 'test', 'java')
JS_TEST_REL = os.path.join('platform', 'web', 'test')

SKIP_RC = 3
FAIL_RC = 1
SELFTEST_FAIL_RC = 2

# 顺序是判据的一部分：前端必须先构建，见 PB-STITCH-MISSING。
# `npm-test` 排在 `npm-build` **之前**：渲染测试（vitest + jsdom）不需要构建产物，
# 它自己会转译源码 —— 放在前面，一旦红就能立刻看出是「测试挂了」而不是「产物没出来」。
STEPS = ('npm-ci', 'npm-test', 'npm-build', 'mvn-test')

# 复制沙箱时**故意不带**的东西：它们正是本判据要重新生成的产物。
# 注意这里只按目录**名**跳过（与 run_all_gates.fingerprint 同一套思路），
# 因为带过去的旧产物会让判据看着上一次的结果通过。
# ⚠️ `static` 那条**别删**：它被 .gitignore 而不入库，但本机磁盘上常年留着上一次
# `npm run build` 的产物 ⇒ 不跳就等于允许「npm build 一个文件都没产出」也报绿
# （判据只查"有没有少"、产物又能被旧字节顶替 —— 假绿的经典形状）。
COPY_SKIP_DIRS = frozenset(('node_modules', 'target', 'dist', 'static', '__pycache__'))

REQUIRED_IN_SANDBOX = (
    os.path.join(PLATFORM, 'pom.xml'),
    os.path.join(PLATFORM, 'api', 'pom.xml'),
    os.path.join(WEB, 'package.json'),
    os.path.join(WEB, 'package-lock.json'),
    os.path.join(WEB, 'vite.config.js'),
    # 渲染测试的输入。缺了它 vitest 会在**解析模块**那一层报一个看不懂的错，
    # 与「页面印错了」混成一团 —— 所以单独列出来，缺了就报 PB-SANDBOX-MISSING。
    os.path.join(JS_TEST_REL, 'report-view.fixture.json'),
)

MIN_JAVA_MAJOR = 21
MIN_NODE_MAJOR = 20

MVN_TIMEOUT = 1800
NPM_TIMEOUT = 900

# surefire 每个测试类印一行，末尾再印一行汇总 —— 取**最后**一条才是汇总。
# 实测形态：`[INFO] Tests run: N, Failures: 0, Errors: 0, Skipped: 0`
#           `[ERROR] Tests run: N, Failures: 1, Errors: 1, Skipped: 0`（失败时走 stderr）
#           （N 只写形态不写数：真实 N 会随用例增删变，上面的用例数由本门禁自己数出来对拍。）
SUREFIRE_RE = re.compile(
    r'Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)')
TEST_ANNOTATION_RE = re.compile(r'^\s*@Test\b', re.M)
# vitest 的汇总行（前面带缩进，实测形态）：
#   `      Tests  13 passed (13)`
#   `      Tests  1 failed | 12 passed (13)`
#   `      Tests  2 skipped | 11 passed (13)`
# 另一行 `Test Files  1 passed (1)` **不含** `^\s*Tests ` 前缀，不会被误取。
VITEST_TESTS_RE = re.compile(r'^[ \t]*Tests[ \t]+(.+)$', re.M)
VITEST_COUNT_RE = re.compile(r'(\d+)\s+(passed|failed|skipped|todo)')
VITEST_TOTAL_RE = re.compile(r'\((\d+)\)\s*$')
# 只在带颜色的终端里出现；非 TTY 时 vitest 自己关掉颜色，但别把结论压在这上面。
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# JS 侧声明的用例：`it(` / `test(`，允许 `it.skip(` / `it.only(` 这类后缀。
# 数与 vitest 报的**总数**对拍 —— 与 `@Test` 那条同形，防「悄悄只跑了一部分」。
JS_TEST_CALL_RE = re.compile(r'^\s*(?:it|test)(?:\.\w+)?\s*\(', re.M)
JAVA_VERSION_RE = re.compile(r'version\s+"([0-9]+)(?:[._]([0-9]+))?')
NODE_VERSION_RE = re.compile(r'v(\d+)\.')
# `mvn -v` 第一行：Apache Maven 3.9.16 (...)
MVN_VERSION_RE = re.compile(r'Apache Maven\s+([0-9][0-9.]*)')

# `run_all_gates.verdict_of` 只认 `verdict: <WORD>`；SKIPPED 是本脚本自己的词，
# 括号里写出「缺什么」供报告读者直接看懂，不必回来跑一遍。
VERDICT_SKIP = 'SKIPPED'


# ---------------------------------------------------------------------------
# 纯函数：判据本身。--selftest 只测这一段，所以样本不需要 JDK / Node。
# ---------------------------------------------------------------------------
def parse_surefire(text):
    """maven 输出 -> 汇总计数，或 None（提取为空）。

    None 与 `run == 0` 是**两件事**，必须分开：前者是「没提取到」（正则失配、
    `-q` 把 INFO 吞了、插件换了措辞），后者是真的一个用例都没跑。把它们混成一个
    「0」正是本项目最贵的那类假绿。
    """
    last = None
    for m in SUREFIRE_RE.finditer(text or ''):
        last = m
    if last is None:
        return None
    return {'run': int(last.group(1)), 'failures': int(last.group(2)),
            'errors': int(last.group(3)), 'skipped': int(last.group(4))}


def strip_java_comments(text):
    """剥掉 `//`、`/* */` 与字符串字面量，免得注释里的 `@Test` 被算成用例。

    正则做不到这件事（`"//"` 这种字面量会把裸正则带进沟里），所以这里走一个小状态机。
    """
    out = []
    i, n = 0, len(text or '')
    while i < n:
        if text.startswith('"""', i):                      # text block
            j = text.find('"""', i + 3)
            i = n if j < 0 else j + 3
            continue
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        elif c == '"':
            out.append(c)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == '\\':
                    if i + 1 < n:
                        out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def count_declared_tests(texts):
    """源码里声明的 `@Test` 个数。分母 —— 没有它，`PB-MVN-TESTS-COUNT` 就无从比较。"""
    return sum(len(TEST_ANNOTATION_RE.findall(strip_java_comments(t))) for t in texts)


def strip_ansi(text):
    """去掉 ANSI 颜色码。提取器不许依赖「调用方没开颜色」这种约定。"""
    return ANSI_RE.sub('', text or '')


def parse_vitest(text):
    """vitest 输出 -> 汇总计数，或 None（提取为空）。

    与 `parse_surefire` 同一条纪律：None（没提取到）与 `total == 0`（真跑 0 个）
    是**两件事**，混成一个「0」就是最贵的那类假绿。
    """
    last = None
    for m in VITEST_TESTS_RE.finditer(strip_ansi(text)):
        last = m
    if last is None:
        return None
    body = last.group(1)
    out = {'passed': 0, 'failed': 0, 'skipped': 0, 'total': None}
    for count, word in VITEST_COUNT_RE.findall(body):
        if word in ('passed', 'failed', 'skipped'):
            out[word] += int(count)
    m = VITEST_TOTAL_RE.search(body)
    out['total'] = int(m.group(1)) if m else (out['passed'] + out['failed']
                                              + out['skipped'])
    # 计数放在括号里的是总数（vitest 自己印的 `(N)`）。
    return out


def count_declared_js_tests(texts):
    """`platform/web/test` 里声明的 `it(` 个数。分母 —— 没有它，
    `PB-NPM-TESTS-COUNT` 就无从比较。"""
    return sum(len(JS_TEST_CALL_RE.findall(t)) for t in texts)


def toolchain_reasons(tools):
    """纯：工具链不全的理由清单（空表 = 可以跑）。

    `tools` = {'mvn','npm','java_major','java_from','node_major'}，值可以是 None。
    """
    reasons = []
    if not tools.get('mvn'):
        reasons.append('mvn not found on PATH')
    if not tools.get('npm'):
        reasons.append('npm not found on PATH')
    if tools.get('node_major') is None:
        reasons.append('node not found on PATH')
    elif tools['node_major'] < MIN_NODE_MAJOR:
        reasons.append('node %d < required %d' % (tools['node_major'], MIN_NODE_MAJOR))
    if tools.get('java_major') is None:
        reasons.append('no JDK found (JAVA_HOME and PATH both unusable)')
    elif tools['java_major'] < MIN_JAVA_MAJOR:
        reasons.append('JDK %d < required %d (pom pins java.version=%d)'
                       % (tools['java_major'], MIN_JAVA_MAJOR, MIN_JAVA_MAJOR))
    return reasons


def exit_code(issues, skip_reasons):
    """纯：唯一的退出码定义。三个出口按「有没有跳过」优先 —— 跳过了就不该论红绿。"""
    if skip_reasons:
        return SKIP_RC
    return FAIL_RC if issues else 0


def judge(obs):
    """纯：观测 -> (issues, stats)。**没有**这一步之外的红绿判断。"""
    issues = []

    def bad(code, msg):
        issues.append((code, msg))

    ran = tuple(obs.get('steps_ran') or ())
    missing_steps = [s for s in STEPS if s not in ran]
    if missing_steps:
        bad('PB-STEP-NOT-RUN',
            'step(s) never ran: %s (ran=%s) -- a step that did not run cannot be read '
            'as a passing one' % (', '.join(missing_steps), list(ran) or 'none'))

    if int(obs.get('sandbox_files') or 0) <= 0:
        bad('PB-SANDBOX-EMPTY',
            'the sandbox holds 0 file(s): the copy failed, so every assertion below is '
            'running on nothing')
    for rel in obs.get('sandbox_missing') or ():
        bad('PB-SANDBOX-MISSING', 'the sandbox is missing %s -- the build would either '
            'fail for the wrong reason or test the wrong thing' % rel)

    declared = obs.get('declared_tests')
    if not declared:
        bad('PB-DECLARED-UNREADABLE',
            'could not count a single @Test in %s (found %s test file(s)): the '
            'declared-vs-run comparison loses its denominator, so a truncation would '
            'pass unnoticed' % (JAVA_TEST_REL, obs.get('test_files')))

    sf = parse_surefire(obs.get('maven_text'))
    if sf is None:
        bad('PB-SUREFIRE-NOT-PARSED',
            'no surefire summary line in the maven output -- extraction came back empty '
            '(ran with -q? plugin wording changed?), so the test counters below are '
            'vacuous; refusing to pass on an empty extraction')
    else:
        if sf['run'] == 0:
            bad('PB-MVN-TESTS-ZERO', 'surefire ran 0 test(s) -- a green build that ran '
                'nothing is the shape this project keeps getting bitten by')
        if sf['failures'] or sf['errors']:
            bad('PB-MVN-TESTS-RED', 'surefire: Tests run=%d Failures=%d Errors=%d'
                % (sf['run'], sf['failures'], sf['errors']))
        if declared and sf['run'] and sf['run'] < declared:
            bad('PB-MVN-TESTS-COUNT',
                'surefire ran %d test(s) but the sources declare %d @Test -- some tests '
                'were not executed' % (sf['run'], declared))
    if obs.get('maven_rc') != 0:
        bad('PB-MVN-FAIL', 'mvn test exited %s (a non-zero build is not the same fact as '
            'a failing test; both are reported separately)' % obs.get('maven_rc'))

    if obs.get('npm_ci_rc') != 0:
        bad('PB-NPM-CI-FAIL', 'npm ci exited %s -- the dependency set in '
            'package-lock.json could not be installed, so a green build below would be '
            'green about the wrong tree' % obs.get('npm_ci_rc'))
    if obs.get('npm_build_rc') != 0:
        bad('PB-NPM-BUILD-FAIL', 'npm run build exited %s' % obs.get('npm_build_rc'))

    # ---- 渲染测试（vitest + jsdom）。与 surefire 那一段逐条同形 --------------
    declared_js = obs.get('declared_js_tests')
    if not declared_js:
        bad('PB-JS-DECLARED-UNREADABLE',
            'could not count a single it()/test() in %s (found %s test file(s)): the '
            'declared-vs-run comparison loses its denominator, and 0 declared tests '
            'would also mean the render coverage is gone' % (JS_TEST_REL,
                                                            obs.get('js_test_files')))
    vt = parse_vitest(obs.get('npm_test_text'))
    if vt is None:
        bad('PB-VITEST-NOT-PARSED',
            'no vitest summary line ("Tests  N passed") in the npm run test output -- '
            'extraction came back empty, so the counters below would be vacuous; '
            'refusing to pass on an empty extraction')
    else:
        if vt['total'] == 0:
            bad('PB-NPM-TESTS-ZERO', 'vitest reported 0 test(s) in total -- a green run '
                'that ran nothing is the shape this project keeps getting bitten by')
        if declared_js and vt['total'] and vt['total'] < declared_js:
            bad('PB-NPM-TESTS-COUNT',
                'vitest ran %d test(s) but %s declares %d it()/test() -- some tests '
                'were not executed' % (vt['total'], JS_TEST_REL, declared_js))
    if obs.get('npm_test_rc') != 0:
        bad('PB-NPM-TEST-FAIL', 'npm run test exited %s (vitest: %s)'
            % (obs.get('npm_test_rc'),
               ('passed=%d failed=%d skipped=%d total=%d' % (vt['passed'], vt['failed'],
                                                             vt['skipped'], vt['total']))
               if vt else 'no summary line'))

    static_files = obs.get('static_files') or []
    names = [n for n, _ in static_files]
    total_bytes = sum(s for _, s in static_files)
    if 'index.html' not in names or not any(n.replace('\\', '/').startswith('assets/')
                                            for n in names):
        bad('PB-STATIC-EMPTY',
            'the build reported success but left %d file(s) in %s (%s) -- "the build '
            'succeeded" and "something was produced" are different facts'
            % (len(static_files), STATIC_REL, ', '.join(names) or 'nothing'))

    if obs.get('served_before'):
        bad('PB-STITCH-PREEXISTING',
            '%s already existed before maven ran -- the stitch assertion below would '
            'pass on a stale copy' % SERVED_REL)
    served = [n for n, _ in (obs.get('served_files') or [])]
    if 'index.html' not in served:
        bad('PB-STITCH-MISSING',
            'the frontend bundle is not under %s, which is the directory Spring '
            'actually serves: either npm run build ran BEFORE... no, after mvn test '
            '(the documented build-order trap, platform/README.md), or the '
            'web -> api/src/main/resources/static seam is broken' % SERVED_REL)

    stats = {
        'sandbox_files': obs.get('sandbox_files'),
        'mvn_tests_run': sf['run'] if sf else None,
        'mvn_declared': declared,
        'mvn_failures': sf['failures'] if sf else None,
        'mvn_errors': sf['errors'] if sf else None,
        'js_total': vt['total'] if vt else None,
        'js_declared': declared_js,
        'static_files': len(static_files),
        'static_bytes': total_bytes,
        'served_files': len(served),
    }
    return issues, stats


# ---------------------------------------------------------------------------
# 不纯的部分：探工具链、造沙箱、跑三步。
# ---------------------------------------------------------------------------
def harden_stdout():
    """cp936 控制台打印 GBK 之外的字符会抛 UnicodeEncodeError，把整趟结论一起丢掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def run(cmd, cwd, timeout, env=None):
    """跑一个子进程，返回 {'rc','text','seconds'}。stderr 合并进 stdout。

    合并 stderr 不是可选项：`mvn` 的失败摘要走 stderr，`npm` 的进度走 stderr ——
    不合并就会出现「解析不到任何东西」而**看起来像**构建成功。
    """
    started = time.time()
    try:
        proc = subprocess.run(list(cmd), cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {'rc': None, 'text': 'TIMEOUT after %ds' % timeout,
                'seconds': round(time.time() - started, 1)}
    except OSError as exc:
        return {'rc': None, 'text': 'OSError: %s' % exc,
                'seconds': round(time.time() - started, 1)}
    return {'rc': proc.returncode,
            'text': proc.stdout.decode('utf-8', 'replace'),
            'seconds': round(time.time() - started, 1)}


def java_major_of(version_text):
    m = JAVA_VERSION_RE.search(version_text or '')
    if not m:
        return None
    major = int(m.group(1))
    # '1.8.0_392' 这种老式串：真正的版本号在第二段
    if major == 1 and m.group(2):
        major = int(m.group(2))
    return major


def probe_toolchain(env=None):
    """探「maven 会用的那个 JDK」而不是 PATH 上的 `java`。

    实测教训（pom 头部也写着）：本机 PATH 上的 `java` 是 25，而 Maven 用的是
    `JAVA_HOME`（Zulu 21）。只看 `java -version` 会得到「编译得过的假象」。
    """
    env = os.environ if env is None else env
    tools = {'mvn': shutil.which('mvn'), 'npm': shutil.which('npm'),
             'java_major': None, 'java_from': '', 'node_major': None,
             'mvn_version': '', 'node_version': '', 'npm_version': ''}
    java = None
    java_home = env.get('JAVA_HOME')
    if java_home:
        cand = os.path.join(java_home, 'bin', 'java')
        for ext in ('', '.exe'):
            if os.path.isfile(cand + ext):
                java = cand + ext
                tools['java_from'] = 'JAVA_HOME=%s' % java_home
                break
    if java is None:
        java = shutil.which('java')
        if java:
            tools['java_from'] = 'PATH'
    if java:
        out = run([java, '-version'], ROOT, 120)
        tools['java_major'] = java_major_of(out['text'])
        tools['java_version'] = (out['text'].splitlines() or [''])[0].strip()
    node = shutil.which('node')
    if node:
        out = run([node, '--version'], ROOT, 120)
        tools['node_version'] = out['text'].strip()
        m = NODE_VERSION_RE.search(tools['node_version'])
        tools['node_major'] = int(m.group(1)) if m else None
    if tools['npm']:
        out = run([tools['npm'], '--version'], ROOT, 120)
        tools['npm_version'] = out['text'].strip().splitlines()[0] if out['text'].strip() else ''
    if tools['mvn']:
        # `mvn -v` 每次约 0.6s：值在于把「Maven 实际用的 JDK」打进证据里
        out = run([tools['mvn'], '-v'], ROOT, 300)
        m = MVN_VERSION_RE.search(out['text'])
        tools['mvn_version'] = m.group(1) if m else 'unknown'
        if tools['java_major'] is None:
            tools['java_major'] = java_major_of(out['text'])
            tools['java_from'] = 'mvn -v'
            tools['java_version'] = next((l for l in out['text'].splitlines()
                                          if 'Java version' in l), '').strip()
    return tools


def copy_tree(src, dst, skip_dirs):
    n = 0
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel = os.path.relpath(base, src)
        target = dst if rel == '.' else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in files:
            shutil.copy2(os.path.join(base, fn), os.path.join(target, fn))
            n += 1
    return n


GIT_PLACEHOLDER = """\
This is not a git repository. It is a placeholder DIRECTORY named .git, created by
tools/verify_platform_build.py so that the Java side can resolve its relative report
directory the same way it does inside a real checkout.

ReportCatalog.resolve() walks up from the working directory looking for a `.git`
directory and then resolves `.rounds/i1` from that root. Without this placeholder the
sandbox resolves it under <sandbox>/api/, the two report-reading tests fail with
ReportNotFoundException, and the gate goes red for a reason that has nothing to do
with the code under test. Measured 2026-09-30.
"""


def build_sandbox(root, dest):
    """复制一个可跑的 checkout 形状：platform/ + .rounds/i1/ + 空的 .git/。"""
    n = copy_tree(os.path.join(root, PLATFORM), os.path.join(dest, PLATFORM),
                  COPY_SKIP_DIRS)
    rounds_src = os.path.join(root, ROUNDS)
    if os.path.isdir(rounds_src):
        n += copy_tree(rounds_src, os.path.join(dest, ROUNDS), COPY_SKIP_DIRS)
    git_dir = os.path.join(dest, '.git')
    os.makedirs(git_dir, exist_ok=True)
    with open(os.path.join(git_dir, 'quanauto-build-sandbox'), 'w',
              encoding='utf-8', newline='\n') as f:
        f.write(GIT_PLACEHOLDER)
    return n


def scan_files(root):
    """目录下所有文件 [(相对路径, 字节数)]。目录不存在 -> 空表。"""
    out = []
    for base, dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(base, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            out.append((os.path.relpath(p, root), size))
    return sorted(out)


def read_declared_tests(root):
    base = os.path.join(root, JAVA_TEST_REL)
    texts = []
    for b, _d, fs in os.walk(base):
        for fn in fs:
            if fn.endswith('.java'):
                with open(os.path.join(b, fn), encoding='utf-8', errors='replace') as f:
                    texts.append(f.read())
    return count_declared_tests(texts), len(texts)


def read_declared_js_tests(root):
    base = os.path.join(root, JS_TEST_REL)
    texts = []
    for b, _d, fs in os.walk(base):
        for fn in fs:
            if fn.endswith('.js') or fn.endswith('.jsx'):
                with open(os.path.join(b, fn), encoding='utf-8', errors='replace') as f:
                    texts.append(f.read())
    return count_declared_js_tests(texts), len(texts)


def observe(root, tools, keep=False, verbose=True):
    """把三步真跑一遍，返回 obs。**不在仓库里写任何东西。**

    顺序就是判据的一部分：前端先、maven 后（见 PB-STITCH-MISSING）。
    """
    sandbox = tempfile.mkdtemp(prefix='quanauto-platform-build-')
    obs = {'steps_ran': [], 'sandbox_missing': [], 'sandbox_files': 0,
           'maven_rc': None, 'maven_text': '', 'declared_tests': None, 'test_files': 0,
           'npm_ci_rc': None, 'npm_ci_text': '', 'npm_test_rc': None,
           'npm_test_text': '', 'npm_build_rc': None,
           'npm_build_text': '', 'declared_js_tests': None, 'js_test_files': 0,
           'static_files': [], 'served_before': False,
           'served_files': []}
    try:
        obs['sandbox_files'] = build_sandbox(root, sandbox)
        obs['sandbox_missing'] = [rel for rel in REQUIRED_IN_SANDBOX
                                 if not os.path.isfile(os.path.join(sandbox, rel))]
        declared, nfiles = read_declared_tests(root)
        obs['declared_tests'] = declared if (nfiles and declared) else None
        obs['test_files'] = nfiles
        declared_js, js_files = read_declared_js_tests(root)
        obs['declared_js_tests'] = declared_js if (js_files and declared_js) else None
        obs['js_test_files'] = js_files
        if verbose:
            print('SANDBOX %s (%d file(s) copied)' % (sandbox, obs['sandbox_files']))

        web_dir = os.path.join(sandbox, WEB)
        platform_dir = os.path.join(sandbox, PLATFORM)

        def step(name, cmd, cwd, timeout):
            out = run(cmd, cwd, timeout)
            obs['steps_ran'].append(name)
            if verbose:
                print('STEP %-16s rc=%s (%.1fs) %s'
                      % (name, out['rc'], out['seconds'],
                         'OK' if out['rc'] == 0 else 'FAILED'))
            return out

        out = step('npm-ci', [tools['npm'], 'ci', '--no-audit', '--no-fund'],
                   web_dir, NPM_TIMEOUT)
        obs['npm_ci_rc'], obs['npm_ci_text'] = out['rc'], out['text']

        # 渲染测试：vitest + jsdom，把 App.jsx 真的挂起来。它不需要构建产物，
        # 所以排在 build 前面（见 STEPS 上方的注释）。
        out = step('npm-test', [tools['npm'], 'run', 'test'], web_dir, NPM_TIMEOUT)
        obs['npm_test_rc'], obs['npm_test_text'] = out['rc'], out['text']

        out = step('npm-build', [tools['npm'], 'run', 'build'], web_dir, NPM_TIMEOUT)
        obs['npm_build_rc'], obs['npm_build_text'] = out['rc'], out['text']

        obs['static_files'] = scan_files(os.path.join(sandbox, STATIC_REL))
        # 守卫的守卫：跑 maven 之前这个目录必须还不存在，否则下面那条接缝判据
        # 会看着旧产物通过。
        obs['served_before'] = os.path.isdir(os.path.join(sandbox, SERVED_REL))

        out = step('mvn-test', [tools['mvn'], '-B', 'test'], platform_dir,
                   MVN_TIMEOUT)
        obs['maven_rc'], obs['maven_text'] = out['rc'], out['text']

        obs['served_files'] = scan_files(os.path.join(sandbox, SERVED_REL))
    finally:
        if keep:
            print('SANDBOX KEPT %s' % sandbox)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
    return obs


def report(obs, issues, stats):
    print('STATS sandbox_files=%s mvn_tests_run=%s mvn_declared=%s mvn_failures=%s '
          'mvn_errors=%s js_tests_total=%s js_tests_declared=%s static_files=%s '
          'static_bytes=%s served_files=%s'
          % (stats['sandbox_files'], stats['mvn_tests_run'], stats['mvn_declared'],
             stats['mvn_failures'], stats['mvn_errors'], stats['js_total'],
             stats['js_declared'], stats['static_files'], stats['static_bytes'],
             stats['served_files']))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    if issues:
        print('verdict: FAIL (%d issue(s))' % len(issues))
    else:
        print('verdict: PASS (0 issue(s))')


# ---------------------------------------------------------------------------
# 自测：只测纯函数 + 自造 fixture。**不碰真产物，也不需要 JDK / Node。**
# ---------------------------------------------------------------------------
def selftest():
    ok = True

    def check(tag, hit, detail=''):
        nonlocal ok
        print('  [%s] %s%s' % (tag, 'OK' if hit else 'MISSED',
                               (' ' + detail) if (detail and not hit) else ''))
        ok = ok and hit

    def codes_of(obs):
        return sorted(c for c, _m in judge(obs)[0])

    def base_obs(**over):
        """干净观测。样本默认从这个基准出发，只把要测的那一处改坏。"""
        obs = {'steps_ran': list(STEPS), 'sandbox_files': 26, 'sandbox_missing': [],
               'declared_tests': 8, 'test_files': 1,
               'maven_rc': 0, 'maven_text': 'Tests run: 8, Failures: 0, Errors: 0, '
                                            'Skipped: 0\nBUILD SUCCESS\n',
               'npm_ci_rc': 0, 'npm_ci_text': 'added 19 packages\n',
               'npm_test_rc': 0, 'npm_test_text': ' Test Files  1 passed (1)\n'
                                                '      Tests  13 passed (13)\n'
                                                '   Duration  1.57s\n',
               'declared_js_tests': 13, 'js_test_files': 1,
               'npm_build_rc': 0, 'npm_build_text': 'built in 108ms\n',
               'static_files': [('index.html', 443), ('assets/a.js', 224286)],
               'served_before': False,
               'served_files': [('index.html', 443), ('assets/a.js', 224286)]}
        obs.update(over)
        return obs

    sampled = set()

    def expect(tag, obs, want, detail=''):
        got = codes_of(obs)
        sampled.update(got)          # 所有样本跑出来的码并集，供最后那条双向核对
        check(tag, got == sorted(want), detail or ('got=%s want=%s' % (got, sorted(want))))

    # ---- 干净样本（防误报）：不改一处，就必须一条都不报 ------------------------
    expect('clean-yields-no-issue', base_obs(), [])

    # ---- 每个检查码一个负样本，且断言的是**恰好**那些码 ------------------------
    expect('pb-sandbox-empty', base_obs(sandbox_files=0), ['PB-SANDBOX-EMPTY'])
    expect('pb-sandbox-missing', base_obs(sandbox_missing=['platform/pom.xml']),
           ['PB-SANDBOX-MISSING'])
    expect('pb-step-not-run', base_obs(steps_ran=['npm-ci', 'npm-build', 'mvn-test']),
           ['PB-STEP-NOT-RUN'])
    expect('pb-declared-unreadable', base_obs(declared_tests=None, test_files=0),
           ['PB-DECLARED-UNREADABLE'])
    expect('pb-js-declared-unreadable', base_obs(declared_js_tests=None, js_test_files=0),
           ['PB-JS-DECLARED-UNREADABLE'])
    # 空转守卫：maven 输出里没有汇总行 ⇒ 只能报「没提取到」，**不许**报成「跑了 0 个」
    expect('pb-surefire-not-parsed',
           base_obs(maven_text='[INFO] BUILD SUCCESS\n'),
           ['PB-SUREFIRE-NOT-PARSED'])
    expect('pb-mvn-tests-zero',
           base_obs(maven_text='Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\n'),
           ['PB-MVN-TESTS-ZERO'])
    expect('pb-mvn-tests-red',
           base_obs(maven_rc=1,
                    maven_text='Tests run: 8, Failures: 1, Errors: 1, Skipped: 0\n'),
           ['PB-MVN-FAIL', 'PB-MVN-TESTS-RED'])
    expect('pb-mvn-tests-count',
           base_obs(maven_text='Tests run: 7, Failures: 0, Errors: 0, Skipped: 0\n'),
           ['PB-MVN-TESTS-COUNT'])
    expect('pb-npm-ci-fail', base_obs(npm_ci_rc=1), ['PB-NPM-CI-FAIL'])
    expect('pb-npm-build-fail', base_obs(npm_build_rc=1), ['PB-NPM-BUILD-FAIL'])
    # 空转守卫（与 surefire 那条同形）：没有汇总行时**只能**报「没提取到」，
    # 不许报成「跑了 0 个」—— 两者在报告里长得一模一样。
    expect('pb-vitest-not-parsed',
           base_obs(npm_test_text='No test files found, exiting with code 1\n'),
           ['PB-VITEST-NOT-PARSED'])
    expect('pb-npm-tests-zero',
           base_obs(npm_test_text='      Tests  0 passed (0)\n'),
           ['PB-NPM-TESTS-ZERO'])
    expect('pb-npm-tests-count',
           base_obs(npm_test_text='      Tests  7 passed (7)\n'),
           ['PB-NPM-TESTS-COUNT'])
    # 红了但要报得准：总数仍是 13（与声明一致）⇒ 只报退出码那一条，
    # 不许连带报 COUNT（那会把「有用例红了」说成「有用例没跑」）。
    expect('pb-npm-test-fail',
           base_obs(npm_test_rc=1,
                    npm_test_text='      Tests  1 failed | 12 passed (13)\n'),
           ['PB-NPM-TEST-FAIL'])
    expect('pb-static-empty', base_obs(static_files=[]), ['PB-STATIC-EMPTY'])
    expect('pb-stitch-missing', base_obs(served_files=[]), ['PB-STITCH-MISSING'])
    expect('pb-stitch-preexisting', base_obs(served_before=True),
           ['PB-STITCH-PREEXISTING'])

    # 每个码都必须有样本：少一个，「这条判据没人守」与「这条判据不会出事」在报告里
    # 长得一模一样。这里把上面样本**跑出来的码的并集**与源码里 `bad()` 用到的码
    # **双向**核一遍：新加一个码忘了配样本会红，样本报了一个源码里没有的码也会红。
    # （并集是在 expect() 里攒的，不是另抄一份清单 —— 两份清单一定會漂。）
    used = set(re.findall(r"bad\('(PB-[A-Z-]+)'", open(os.path.abspath(__file__),
                                                      encoding='utf-8').read()))
    check('every-code-has-a-sample', used == sampled,
          'unsampled=%s unknown=%s' % (sorted(used - sampled), sorted(sampled - used)))

    # ---- 工具链不全：不是红，是 SKIP，而 SKIP **不算绿** ----------------------
    check('toolchain-ok-no-reason',
          toolchain_reasons({'mvn': 'm', 'npm': 'n', 'node_major': 20,
                             'java_major': 21}) == [])
    reasons = toolchain_reasons({'mvn': None, 'npm': None, 'node_major': None,
                                'java_major': None})
    check('toolchain-all-missing', len(reasons) == 4, 'reasons=%s' % reasons)
    old_jdk = toolchain_reasons({'mvn': 'm', 'npm': 'n', 'node_major': 20,
                                 'java_major': 17})
    check('toolchain-old-jdk-names-21',
          len(old_jdk) == 1 and '21' in old_jdk[0], 'reasons=%s' % old_jdk)
    old_node = toolchain_reasons({'mvn': 'm', 'npm': 'n', 'node_major': 18,
                                  'java_major': 21})
    check('toolchain-old-node', len(old_node) == 1 and '20' in old_node[0],
          'reasons=%s' % old_node)
    check('skip-is-not-green', exit_code([], reasons) == SKIP_RC == 3
          and exit_code([], []) == 0 and exit_code([('PB-X', 'y')], []) == FAIL_RC)
    # 不变式：探到工具链不全时**绝不**既报 SKIP 又报 ISSUE（那会让「没跑」与「跑红了」
    # 在报告里混成一个词）。真实路径里 observe() 根本不会被调用，这里断言的是出口码
    # 的优先级：有理由就必须是 3。
    check('skip-wins-over-issues', exit_code([('PB-X', 'y')], reasons) == SKIP_RC)

    # ---- 提取器：surefire 取最后一条（前面每类各一条，只有最后一条是汇总）----
    two = ('Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 -- in A\n'
           'Tests run: 5, Failures: 2, Errors: 1, Skipped: 0\n')
    sf = parse_surefire(two)
    check('surefire-takes-the-aggregate',
          sf == {'run': 5, 'failures': 2, 'errors': 1, 'skipped': 0}, 'got=%s' % sf)
    check('surefire-none-on-empty', parse_surefire('') is None
          and parse_surefire('Tests run: x') is None)

    # ---- 提取器：vitest 汇总行 -------------------------------------------------
    vt = parse_vitest(' Test Files  1 passed (1)\n      Tests  13 passed (13)\n'
                      '   Duration  1.57s\n')
    check('vitest-parses-all-green',
          vt == {'passed': 13, 'failed': 0, 'skipped': 0, 'total': 13}, 'got=%s' % vt)
    vt = parse_vitest('      Tests  1 failed | 12 passed (13)\n')
    check('vitest-parses-a-red-run',
          vt == {'passed': 12, 'failed': 1, 'skipped': 0, 'total': 13}, 'got=%s' % vt)
    vt = parse_vitest('\x1b[32m      Tests  2 skipped | 11 passed (13)\x1b[0m\n')
    check('vitest-tolerates-ansi-colour',
          vt == {'passed': 11, 'failed': 0, 'skipped': 2, 'total': 13}, 'got=%s' % vt)
    # `Test Files` 那一行**不许**被错当成 `Tests` 行（它也是 "N passed (N)" 的形状，
    # 而且：单文件时两个数相等，错了也看不出来；多文件时会把文件数当成用例数）。
    vt = parse_vitest(' Test Files  2 passed (2)\n')
    check('vitest-does-not-read-the-Test-Files-line', vt is None, 'got=%s' % vt)
    check('vitest-none-on-empty', parse_vitest('') is None
          and parse_vitest('Tests  no tests\n') is not None)

    # ---- `it(` 计数器 -----------------------------------------------------------
    js = ('it("a", () => {});\ntest("b", () => {});\n'
          '  it.skip("c", () => {});\n// it("d", () => {});\n'
          'expect(limits(1)).toBe(1);\n')
    check('declared-js-counter', count_declared_js_tests([js]) == 3,
          'got=%s' % count_declared_js_tests([js]))
    check('declared-js-counter-empty', count_declared_js_tests(['const x = 1;']) == 0)

    # ---- `@Test` 计数器：注释里的不算（裸正则会把它算进去，分母就虚高）--------
    src = ('// @Test\n/* @Test\n*/\n@Test\nvoid a() { String s = "// @Test"; }\n'
           '@Test\nvoid b() {}\n')
    check('declared-counter-ignores-comments', count_declared_tests([src]) == 2,
          'got=%s' % count_declared_tests([src]))
    check('declared-counter-empty', count_declared_tests(['interface X {}']) == 0)

    # ---- java 版本串：老的 '1.8.0_392' 不能读成 1 ----------------------------
    check('java-major-modern', java_major_of('openjdk version "21.0.12" 2025-07-15') == 21)
    check('java-major-legacy', java_major_of('java version "1.8.0_392"') == 8)
    check('java-major-garbage-is-none', java_major_of('no version here') is None)

    # ---- 沙箱：自造 fixture，**不从真产物出发** ------------------------------
    # 教训（2026-09-24）：自测样本若从真产物出发，产物一变样本就跟着变，红绿都说明不
    # 了什么。这里全部现场造，并且故意放两个**必须被丢掉**的产物目录进去。
    fixture = tempfile.mkdtemp(prefix='quanauto-build-fixture-')
    dst = tempfile.mkdtemp(prefix='quanauto-build-copy-')
    try:
        for rel, body in (
                (os.path.join(PLATFORM, 'pom.xml'), '<project/>'),
                (os.path.join(PLATFORM, 'api', 'pom.xml'), '<project/>'),
                (os.path.join(WEB, 'package.json'), '{}'),
                (os.path.join(WEB, 'package-lock.json'), '{}'),
                (os.path.join(WEB, 'vite.config.js'), 'export default {};'),
                (os.path.join(JS_TEST_REL, 'report-view.fixture.json'), '{}'),
                (os.path.join(JS_TEST_REL, 'render.test.jsx'), 'it("x", () => {});'),
                (os.path.join(WEB, 'node_modules', 'junk.js'), 'x'),
                (os.path.join(PLATFORM, 'api', 'target', 'junk.class'), 'x'),
                (os.path.join(ROUNDS, 'report-x.json'), '{}')):
            path = os.path.join(fixture, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(body)
        n = build_sandbox(fixture, dst)
        kept = scan_files(dst)
        names = sorted(p.replace('\\', '/') for p, _s in kept)
        check('sandbox-git-placeholder-exists',
              os.path.isdir(os.path.join(dst, '.git')),
              'no .git placeholder -> the Java tests fail on a ReportNotFound that has '
              'nothing to do with the code under test (measured 2026-09-30)')
        check('sandbox-copies-required-files',
              all(os.path.isfile(os.path.join(dst, r)) for r in REQUIRED_IN_SANDBOX),
              'names=%s' % names)
        check('sandbox-rounds-i1-copied',
              any(p.startswith(ROUNDS.replace('\\', '/')) for p in names),
              'names=%s' % names)
        # 渲染测试的输入与用例文件必须跟着进沙箱：少了夹具，vitest 会在**解析模块**那
        # 一层报一个看不懂的错，与「页面印错了」混成一团。
        check('sandbox-copies-js-test-inputs',
              any(p.startswith(JS_TEST_REL.replace('\\', '/')) for p in names),
              'names=%s' % names)
        # 分母要能从沙箱形状里数出来，而且 `.json` 夹具**不许**被算成用例文件。
        check('declared-js-reader-counts-only-code',
              read_declared_js_tests(fixture) == (1, 1),
              'got=%s' % (read_declared_js_tests(fixture),))
        check('sandbox-drops-build-outputs',
              not any('node_modules' in p or 'target/' in p for p in names),
              'copied build outputs -> the gate would judge a stale tree: %s' % names)
        check('sandbox-count-reported',
              n == sum(1 for p, _s in kept
                       if not p.replace('\\', '/').startswith('.git/')) and n > 0,
              'reported n=%s but the copy holds %s' % (n, names))
        # 目录不存在必须回空表（交给判据去报 PB-STATIC-EMPTY），而不是抛异常把整趟打断。
        check('scan-missing-dir-is-empty',
              scan_files(os.path.join(dst, 'no-such-dir')) == [])
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)

    # ---- 端到端：缺工具链必须退 3、且**不许**印 PASS --------------------------
    # `skip-is-not-green` 只证明纯函数算得对，证明不了 main() 把结论接到了退出码上 ——
    # 而「纯函数全对、调用点接错」正是本项目反复踩的坑。所以这里真起一个子进程，把
    # PATH 清空（`shutil.which` 全部返回 None），看**真实的**输出与退出码。
    # 这一跑在「探工具链」阶段就停了，不碰真 JDK / Node，所以自测仍然与工具链无关。
    work = tempfile.mkdtemp(prefix='quanauto-build-emptypath-')
    try:
        env = dict(os.environ)
        env['PATH'] = ''
        env.pop('JAVA_HOME', None)
        blank = run([sys.executable, '-X', 'utf8', os.path.abspath(__file__)], work,
                    600, env=env)
        check('no-toolchain-exits-3-and-says-SKIPPED',
              blank['rc'] == SKIP_RC and 'verdict: SKIPPED' in blank['text']
              and 'verdict: PASS' not in blank['text'],
              'rc=%s tail=%s' % (blank['rc'], blank['text'][-300:]))
        check('no-toolchain-names-what-is-missing',
              'mvn not found on PATH' in blank['text'] and 'node' in blank['text'],
              'tail=%s' % blank['text'][-300:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else SELFTEST_FAIL_RC


def main(argv=None):
    harden_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--keep', action='store_true',
                    help='keep the sandbox on disk (debugging only)')
    ap.add_argument('--repo-root', default=ROOT,
                    help='run against another checkout (used by --selftest fixtures)')
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    root = os.path.abspath(args.repo_root)
    tools = probe_toolchain()
    print('TOOLCHAIN mvn=%s java=%s (%s, from %s) node=%s npm=%s'
          % (tools.get('mvn_version') or tools.get('mvn'), tools.get('java_major'),
             tools.get('java_version', ''), tools.get('java_from'),
             tools.get('node_version') or tools.get('node_major'),
             tools.get('npm_version')))
    reasons = toolchain_reasons(tools)
    if reasons:
        print('SKIP ' + '; '.join(reasons))
        print('NOTE this gate did not run. tools/run_all_gates.py reads exit=%d as '
              'SKIPPED: it is NOT counted as green, and the run is reported as '
              'INCOMPLETE with a non-zero exit code.' % SKIP_RC)
        print('verdict: %s (%s)' % (VERDICT_SKIP, '; '.join(reasons)))
        return SKIP_RC

    obs = observe(root, tools, keep=args.keep)
    issues, stats = judge(obs)
    report(obs, issues, stats)
    return exit_code(issues, [])


if __name__ == '__main__':
    sys.exit(main())
