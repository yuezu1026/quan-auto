#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""enum-members —— 契约里的枚举成员表 vs `quanauto/` 的枚举成员表。

**为什么需要它（B9.1 的还债方式）**：I1~I4 期间唯一比对**枚举成员名**的判据是
`tools/verify_contract_appendix.py`，而它只审主契约**附录 G 节**。三份契约正文里的
枚举块都没有成员级判据：`tools/contract-signature-manifest.json` 的 `classes` /
`impl_only` 里一个枚举都没有，`tools/verify_contract_signature.py` 因而也看不见它们。
2026-09-25 实测：往 `quanauto/enums.py` 的 `MarketStatus` 里插一个假成员
（`SUSPENDED_MUT`），`python tools/run_all_gates.py` 仍 **13/13 全绿** —— 没有任何判据
会看见这件事，而 `MarketStatus` 的成员表当时确实与数据中心契约 §3.1.3 不一致。

**比什么**（顺序敏感：枚举块里成员顺序就是文档给出的顺序）：
  ① 成员名表（含成员数）；② 成员取值；③ 成员顺序。

**登记两类「名字对不上」的豁免**，两种都反向自查（登记项失效即报红）：
  * `PENDING`    —— 契约有块、实现里**故意还没有**（归因/适配等模块按迭代计划往后排）。
  * `IMPL_LOCAL` —— 实现里有、三份契约都没有这块（S1 会话模式、风控内部严重度分级）。

**边界（它证明不了什么）**：
  * 只比**枚举**。契约里的 `class Foo:` / `@dataclass` / `NewType` 不归它管
    （主契约侧由 `contract-appendix` / `contract-signature` 管，本门禁不重复判）。
  * 只比成员名与取值，**不比 docstring、不比 `__str__` 之类的行为**，也不判断「某个成员
    该不该存在」——`HOLIDAY` 该不该并进 `CLOSED` 是人的裁决，不是这个扫描器的结论。
  * 无法解析的契约块里若出现 `class X(Enum)` 字样，它**拒绝通过**（`EM-UNPARSABLE-ENUM`）：
    否则「块坏了 → 提取为空 → 全绿」就是最隐蔽的假绿。不含枚举字样的坏块只在报告里
    计数（它们属于别的门禁的职责，不在这里重复判）。
  * 扫描范围固定 `quanauto/*.py`（文件系统，不用 git 清单 —— 那会随提交漂移）；
    同名枚举出现在多个文件即报红，因为比对对象不唯一。
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
IMPL_REL = 'quanauto'

# 三份契约都要读：核心（10 个枚举）、数据中心（6 个）、风控（7 个）。
CONTRACTS = (
    ('core', os.path.join('docs', '智能量化交易平台-核心模块接口契约文档.md')),
    ('dc', os.path.join('docs', '智能量化交易平台-数据中心接口契约文档.md')),
    ('risk', os.path.join('docs', '智能量化交易平台-风控层接口契约文档.md')),
)

PY_BLOCK_RE = re.compile(r'```python\r?\n(.*?)```', re.S)
# 只用来判断「坏块里有没有枚举」，不做解析 —— 坏块解析不了，正则才看得见。
ENUM_HINT_RE = re.compile(r'^[ \t]*class[ \t]+\w+[ \t]*\([^)]*\bEnum\b[^)]*\)[ \t]*:', re.M)

# 契约先行：契约里有块、实现里故意还没有。归因/适配/报表模块按迭代计划排在实盘之前。
PENDING = {
    'DataQualityFlag': '数据中心契约 §3.1.3；§7.5 的每日完整性检查尚未实现（I2 只落了 '
                       '`dc_quality_issue` 表与 CHECK，取值域由 DDL 与 smoke 守着）',
    'ReportType': '数据中心契约 §3.1.3；财务报表服务（FinancialDataService）未实现',
}

# 实现侧独有：三份契约都没有这个枚举块，登记「为什么没有」。
# 注意这**不是** manifest 登记的重复品：manifest 的 `impl_only` 登记的是
# 「成员签名/字段」，这里登记的是「这个枚举为什么没有契约块」。
IMPL_LOCAL = {
    'SessionMode': '契约 §3.3 的 `DataCenter.as_of(...)` 没有会话参数，而 D6/D7 的判据是'
                   '「回测会话 vs 实盘会话」⇒ S1 在实现侧引入；同一名字也在 '
                   '`contract-signature-manifest.json` 的 `impl_only` 里登记过'
                   '（那份登记盯的是成员消失，这份盯的是它没有契约来源）',
    'SeverityEnum': '风控契约 §3.2.6 与 proto 注释把 `severity` 写成 `str`'
                    '（WARNING / ERROR / CRITICAL），没有枚举块 ⇒ 实现侧引入。'
                    '⚠️ 同名词在数据中心是**另一个域**：`ck_dc_quality_severity` 允许 '
                    'CRITICAL / WARNING / INFO（没有 ERROR）⇒ 两者词汇相近但不是同一件事，'
                    '合并前必须先有裁决',
}

# 提取为空 / 提取变薄 ⇒ 拒绝通过。实测值：契约 23 个、实现 23 个（2026-09-25）。
MIN_CONTRACT_ENUMS = 20
MIN_IMPL_ENUMS = 20


def harden_stdout():
    """控制台是 cp936：一个 GBK 之外的字符就能让整份报告在打印途中抛
    UnicodeEncodeError。降级成 '?' 而不是崩掉，否则结论会整个丢掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def read_text(path):
    """统一按 UTF-8 + LF 读。CRLF 会让裸 \\n 的正则**静默**失配（提取 0 却全绿）。"""
    with open(path, 'r', encoding='utf-8-sig') as handle:
        return handle.read().replace('\r\n', '\n')


def write_text(path, text):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


# ── 枚举形状 ─────────────────────────────────────────────────────────
def is_enum_class(node):
    for base in node.bases:
        try:
            if re.search(r'\bEnum\b', ast.unparse(base)):
                return True
        except Exception:  # pragma: no cover - ast.unparse 对语法树不会抛
            pass
    return False


def enum_members(node):
    """枚举体的成员，按源码顺序。只收「大写名字 = 字面量」这一种形状。

    刻意**不**收 `_ignore_` / `_order_` 之类的下划线名，也不收方法 —— 它们不是成员表
    的一部分，收进来会让「实现里多一个私有辅助」变成假红。
    """
    members = []
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            value = ast.literal_eval(stmt.value)
        except Exception:
            value = ast.unparse(stmt.value)
        members.append((target.id, value))
    return members


def enums_of(src, line_offset):
    """一份 python 源码 → ({枚举名: shape}, [问题])。解析不了就报问题，不静默丢。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {}, ['不可解析：%s（块内第 %s 行）' % (exc.msg, exc.lineno)]
    found = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and is_enum_class(node):
            # lineno 是块内行号（第一行 = 1）；line_offset 传的是围栏那一行。
            found[node.name] = {'members': enum_members(node),
                                'line': line_offset + node.lineno}
    return found, []


def blocks_of(text):
    """全文的 ```python 块 → [(源码, 文档内起始行号)]。"""
    out = []
    for match in PY_BLOCK_RE.finditer(text):
        out.append((match.group(1), text.count('\n', 0, match.start()) + 1))
    return out


def scan_contracts(root, contracts):
    """读三份契约 → (declared, summary, findings)。"""
    findings = []
    summary = {'blocks': 0, 'unparsable': 0, 'enum_hint': 0, 'per_doc': {}}
    declared = {}
    for tag, rel in contracts:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            findings.append(('EM-NO-CONTRACT',
                             '找不到契约 %s（%s）—— 少读一份契约会让它的枚举全部退出比对，'
                             '拒绝通过' % (tag, rel)))
            summary['per_doc'][tag] = None
            continue
        blocks = blocks_of(read_text(path))
        summary['blocks'] += len(blocks)
        if not blocks:
            findings.append(('EM-NO-BLOCKS',
                             '契约 %s（%s）里一个 ```python 块都没有 ⇒ 提取为空，'
                             '比对形同虚设' % (tag, rel)))
            summary['per_doc'][tag] = []
            continue
        names = []
        for src, line in blocks:
            found, problems = enums_of(src, line)
            if problems:
                summary['unparsable'] += 1
                if ENUM_HINT_RE.search(src):
                    summary['enum_hint'] += 1
                    findings.append(('EM-UNPARSABLE-ENUM',
                                     '契约 %s 文档 %d 行的块里有 `class ...(Enum)` '
                                     '但整块解析不了（%s）—— 这个枚举退出了比对，拒绝通过'
                                     % (tag, line, '; '.join(problems))))
                continue
            for name, shape in found.items():
                names.append(name)
                record = dict(shape)
                record['contract'] = tag
                record['where'] = '%s:%d' % (rel, shape['line'])
                declared.setdefault(name, []).append(record)
        summary['per_doc'][tag] = names
    return declared, summary, findings


def scan_impl(root):
    """扫 `quanauto/*.py` → ({枚举名: shape}, 文件数, [问题])。"""
    findings = []
    directory = os.path.join(root, IMPL_REL)
    if not os.path.isdir(directory):
        return {}, 0, [('EM-NO-IMPL-DIR', '%s/ 不存在 ⇒ 没有可比对象' % IMPL_REL)]
    impl = {}
    files = 0
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith('.py'):
            continue
        files += 1
        # 文件首行就是第 1 行 ⇒ 偏移 0（与契约块的 fence 行偏移不同）
        found, problems = enums_of(read_text(os.path.join(directory, fname)), 0)
        for problem in problems:
            findings.append(('EM-IMPL-SCAN', '%s/%s %s' % (IMPL_REL, fname, problem)))
        for name, shape in found.items():
            record = dict(shape)
            record['source'] = '%s/%s:%d' % (IMPL_REL, fname, shape['line'])
            impl.setdefault(name, []).append(record)
    return impl, files, findings


def compare_one(name, dec, impl):
    """名字相同的一份契约声明 vs 一份实现 —— 返回不一致的描述（空 = 一致）。"""
    want = dec['members']
    got = impl['members']
    want_names = [m[0] for m in want]
    got_names = [m[0] for m in got]
    out = []
    if len(want_names) != len(got_names):
        out.append('成员数不一致 %s：契约 %d / 实现 %d（契约 %s / 实现 %s，%s vs %s）'
                   % (name, len(want_names), len(got_names), want_names, got_names,
                      dec['where'], impl['source']))
    if set(want_names) != set(got_names):
        out.append('成员名不一致 %s：契约多出 %s，实现多出 %s（%s vs %s）'
                   % (name, sorted(set(want_names) - set(got_names)),
                      sorted(set(got_names) - set(want_names)),
                      dec['where'], impl['source']))
    elif want_names != got_names:
        out.append('成员顺序不一致 %s：契约 %s / 实现 %s（%s vs %s）'
                   % (name, want_names, got_names, dec['where'], impl['source']))
    if [m[1] for m in want] != [m[1] for m in got]:
        out.append('成员取值不一致 %s：契约 %s / 实现 %s（%s vs %s）'
                   % (name, want, got, dec['where'], impl['source']))
    return out


def audit(root, pending=None, local=None, contracts=None, min_contract=None,
          min_impl=None):
    """返回 (summary, findings)。findings = [(code, message)]，已排序去重。"""
    pending = PENDING if pending is None else pending
    local = IMPL_LOCAL if local is None else local
    contracts = CONTRACTS if contracts is None else contracts
    min_contract = MIN_CONTRACT_ENUMS if min_contract is None else min_contract
    min_impl = MIN_IMPL_ENUMS if min_impl is None else min_impl

    findings = []
    summary = {'root': root, 'blocks': 0, 'contract_enums': 0, 'shared': 0,
               'unparsable': 0, 'enum_hint': 0, 'impl_files': 0, 'impl_enums': 0,
               'matched': 0, 'pending': 0, 'local': 0, 'per_doc': {}, 'skipped': False}

    declared, doc_summary, doc_findings = scan_contracts(root, contracts)
    findings.extend(doc_findings)
    summary.update({k: doc_summary[k] for k in
                    ('blocks', 'unparsable', 'enum_hint', 'per_doc')})
    summary['contract_enums'] = len(declared)
    if declared and len(declared) < min_contract:
        findings.append(('EM-THIN-CONTRACT',
                         '三份契约只提取出 %d 个枚举（少于下限 %d）—— 提取器可能已经失配，'
                         '这个数在变薄本身就是线索' % (len(declared), min_contract)))
    if not declared:
        findings.append(('EM-NO-CONTRACT-ENUMS',
                         '三份契约共 %d 个 ```python 块，却一个枚举都没提取到 —— '
                         '提取为空，后面的比对一条都不会跑，拒绝通过' % summary['blocks']))
        return summary, sorted(set(findings))

    # 同名枚举被两份契约各声明一次：表格相同只算 NOTE（计数），不同则拒绝判，
    # 因为「哪一侧是规范」不是这个扫描器能知道的。
    for name in sorted(declared):
        records = declared[name]
        if len(records) < 2:
            continue
        first = records[0]
        for other in records[1:]:
            if other['members'] != first['members']:
                findings.append(('EM-CONTRACT-AMBIGUOUS',
                                 '枚举 %s 被两份契约各声明一次且不一致：%s %s vs %s %s '
                                 '—— 先裁决哪一侧是规范'
                                 % (name, first['contract'], first['members'],
                                    other['contract'], other['members'])))
            else:
                summary['shared'] += 1

    impl, impl_files, impl_findings = scan_impl(root)
    findings.extend(impl_findings)
    summary['impl_files'] = impl_files
    summary['impl_enums'] = len(impl)
    for name in sorted(impl):
        shape = impl[name]
        if len(shape) > 1:
            findings.append(('EM-IMPL-AMBIGUOUS',
                             'quanauto 里有 %d 个同名枚举 %s（%s）—— 比对对象不唯一'
                             % (len(shape), name,
                                ', '.join(r['source'] for r in shape))))
    if not impl:
        findings.append(('EM-NO-IMPL-ENUMS',
                         '扫 %s/*.py 得到 0 个枚举 ⇒ 没有可比对象，比对形同虚设，'
                         '拒绝通过' % IMPL_REL))
        return summary, sorted(set(findings))
    if len(impl) < min_impl:
        findings.append(('EM-THIN-IMPL',
                         '%s/*.py 只提取出 %d 个枚举（少于下限 %d）—— 提取器可能已经失配'
                         % (IMPL_REL, len(impl), min_impl)))

    for name in sorted(pending):
        if name not in declared:
            findings.append(('EM-PENDING-UNUSED',
                             'PENDING 登记了 %s，但三份契约里都没有这个枚举块 —— '
                             '登记项本身失效' % name))
        if name in impl:
            findings.append(('EM-PENDING-DRIFT',
                             '%s 已被实现（%s）—— 契约先行的免检登记当场作废：'
                             '把它从 PENDING 移除并逐条比对'
                             % (name, impl[name][0]['source'])))
    for name in sorted(local):
        if name in declared:
            findings.append(('EM-LOCAL-DRIFT',
                             '%s 已经在契约里有了枚举块（%s）—— IMPL_LOCAL 的免检登记'
                             '必须删掉，改由逐条比对' % (name, declared[name][0]['where'])))
        if name not in impl:
            findings.append(('EM-LOCAL-STALE',
                             'IMPL_LOCAL 登记了 %s，但 quanauto/*.py 里没有这个枚举 —— '
                             '登记项失效' % name))

    for name in sorted(declared):
        if name in pending:
            summary['pending'] += 1
            continue
        if name not in impl:
            findings.append(('EM-MISSING-IMPL',
                             '契约声明的枚举 %s 在 %s/*.py 里不存在，也没登记进 PENDING '
                             '—— 要么实现补上它，要么登记它是契约先行'
                             % (name, IMPL_REL)))
            continue
        summary['matched'] += 1
        for message in compare_one(name, declared[name][0], impl[name][0]):
            findings.append(('EM-MEMBER-MISMATCH', message))
    summary['local'] = len(local)
    for name in sorted(impl):
        if name in declared or name in local:
            continue
        findings.append(('EM-UNREGISTERED-IMPL',
                         'quanauto 的枚举 %s（%s）不在三份契约里，也没登记进 IMPL_LOCAL '
                         '—— 要么补契约，要么登记「它为什么没有契约来源」'
                         % (name, impl[name][0]['source'])))

    summary['skipped'] = summary['matched'] == 0
    if summary['skipped']:
        findings.append(('EM-NO-COMPARE',
                         '同一名字的契约声明与实现一个都没比过（matched=0）—— '
                         '下面唯一的结论只覆盖了守卫与登记项，拒绝通过'))
    return summary, sorted(set(findings))


def report(summary, findings):
    print('root:            %s' % summary['root'])
    for tag, rel in CONTRACTS:
        names = summary['per_doc'].get(tag)
        if names is None:
            print('contract[%-4s]   %s  —— 缺失' % (tag, rel))
        else:
            print('contract[%-4s]   %s  enums=%d %s'
                  % (tag, rel, len(names), sorted(names)))
    print('python blocks:   %d (unparsable=%d, of which enum-like=%d)'
          % (summary['blocks'], summary['unparsable'], summary['enum_hint']))
    print('contract enums:  %d (identical duplicates across docs=%d)'
          % (summary['contract_enums'], summary['shared']))
    print('impl scan:       %s/*.py files=%d enums=%d'
          % (IMPL_REL, summary['impl_files'], summary['impl_enums']))
    print('compared:        matched=%d pending=%d impl-local=%d'
          % (summary['matched'], summary['pending'], summary['local']))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    if summary['skipped']:
        print('NOTE: 一个枚举都没比过 —— 上面的结论只覆盖了守卫与登记项')
    print('verdict: %s (%d finding(s))'
          % ('PASS' if not findings else 'DIRTY', len(findings)))
    return 1 if findings else 0


# ── 自测：每个探测器一个样本 ──────────────────────────────────────────
CORE_DOC = ('# 核心契约\n\n正文一句。\n\n```python\nfrom enum import Enum\n\n\n'
            'class Alpha(Enum):\n    A = "A"\n    B = "B"\n```\n')
DC_DOC = ('# 数据中心契约\n\n```python\nfrom enum import Enum\n\n\n'
          'class Beta(Enum):\n    X = "X"\n```\n')
RISK_DOC = ('# 风控契约\n\n```python\nfrom enum import Enum\n\n\n'
            'class Gamma(Enum):\n    G = "G"\n```\n')
CLEAN_IMPL = ('from enum import Enum\n\n\nclass Alpha(Enum):\n    A = "A"\n'
              '    B = "B"\n\n\nclass Beta(Enum):\n    X = "X"\n\n\n'
              'class Gamma(Enum):\n    G = "G"\n\n\nclass Local(Enum):\n    L = "L"\n')
CLEAN_KW = {'pending': {}, 'local': {'Local': '自测样本'}, 'min_contract': 3,
            'min_impl': 4}
# 干净样本用的三份契约（每个 tag 一个枚举，且与 CLEAN_IMPL 逐条一致）。
DEFAULT_DOCS = {'core': CORE_DOC, 'dc': DC_DOC, 'risk': RISK_DOC}


def patched(**kw):
    """在「三份全写」的基础上打补丁。

    样本里只传一份文档会让另两份根本不存在 ⇒ 声明数骤降、歧义检测也不可能触发，
    于是「样本没打中分支」会被误读成「探测器不存在」。
    """
    docs = dict(DEFAULT_DOCS)
    docs.update(kw)
    return docs

SANDBOX = {'root': None}


def sample(name, docs, impl, expect, marker='', **kw):
    """一个样本：写沙箱 → 跑 audit → 断言命中的代码与消息片段。

    断言到「报出来的正是那个检查码」为止；只断言「有 FINDING」会让「变异没打到分支」
    与「探测器不存在」长得一模一样。
    """
    root = os.path.join(SANDBOX['root'], name)
    used = DEFAULT_DOCS if docs is None else docs
    contracts = []
    for tag in ('core', 'dc', 'risk'):
        if tag not in used:
            continue
        rel = os.path.join('docs', 'contract-%s.md' % tag)
        write_text(os.path.join(root, rel), used[tag])
        contracts.append((tag, rel))
    contracts.extend(kw.pop('extra_contracts', ()))
    if impl is not None:
        for fname, src in impl.items():
            write_text(os.path.join(root, IMPL_REL, fname), src)
    else:
        os.makedirs(os.path.join(root, IMPL_REL), exist_ok=True)
    summary, findings = audit(root, contracts=contracts, **dict(CLEAN_KW, **kw))
    codes = [code for code, _ in findings]
    problems = []
    if expect == ():
        if findings:
            problems.append('期望 0 报错（干净样本防误报），实际 %s' % findings[:2])
    else:
        if expect not in codes:
            problems.append('期望命中 %s，实际 %s' % (expect, codes))
        elif marker and not any(marker in msg for code, msg in findings
                                if code == expect):
            problems.append('期望 %s 的消息里含 %r，实际 %s'
                            % (expect, marker,
                               [m for c, m in findings if c == expect]))
    for code, message in findings[:2]:
        print('    [%s] %s %s' % (name, code, message[:110]))
    if problems:
        print('  SAMPLE FAIL: %s' % '; '.join(problems))
        return False
    print('  sample ok: %s (expect=%s)' % (name, expect or 'clean'))
    return True


def selftest():
    """每个探测器至少一个会失败的样本；外加一个必须 0 报错的干净样本。"""
    SANDBOX['root'] = tempfile.mkdtemp(prefix='enum-members-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    impl = {'enums.py': CLEAN_IMPL}
    ok = True

    ok &= sample('clean-control', None, impl, ())
    # 坏块里**没有** Enum 字样时必须沉默（否则就是把别的门禁的职责重复判一遍）
    ok &= sample('unparsable-non-enum',
                 patched(core=CORE_DOC + '\n```python\nclass Broken(:\n```\n'),
                 impl, ())

    ok &= sample('no-contract', None, impl, 'EM-NO-CONTRACT',
                 extra_contracts=(('gone', os.path.join('docs', 'nope.md')),))
    ok &= sample('no-blocks', {'core': '# 核心契约\n\n只有散文，没有代码块。\n'},
                 impl, 'EM-NO-BLOCKS')
    ok &= sample('no-contract-enums',
                 {'core': '# 核心契约\n\n```python\nx = 1\n```\n',
                  'dc': '# 数据中心契约\n\n```python\ny = 2\n```\n',
                  'risk': '# 风控契约\n\n```python\nz = 3\n```\n'},
                 impl, 'EM-NO-CONTRACT-ENUMS')
    ok &= sample('thin-contract', {'core': CORE_DOC, 'dc': DC_DOC},
                 {'enums.py': CLEAN_IMPL.replace(
                     '\n\n\nclass Gamma(Enum):\n    G = "G"\n', '\n')},
                 'EM-THIN-CONTRACT', min_contract=3, min_impl=3)
    ok &= sample('no-impl-enums', None, {'enums.py': 'x = 1\n'}, 'EM-NO-IMPL-ENUMS')
    ok &= sample('thin-impl', None,
                 {'enums.py': CLEAN_IMPL.replace(
                     '\n\n\nclass Local(Enum):\n    L = "L"\n', '\n')},
                 'EM-THIN-IMPL', min_impl=4, **{'local': {}})
    ok &= sample('unparsable-enum',
                 patched(core=CORE_DOC.replace(
                     '```python\nfrom enum import Enum',
                     '```python\nfrom enum import Enum\nclass Half(Enum):\n')),
                 impl, 'EM-UNPARSABLE-ENUM', min_contract=2,
                 **{'local': {'Alpha': '自测样本', 'Local': '自测样本'}})

    ok &= sample('member-count-mismatch',
                 {'core': CORE_DOC.replace('    B = "B"\n', '')},
                 impl, 'EM-MEMBER-MISMATCH', marker='成员数')
    ok &= sample('member-name-mismatch',
                 {'core': CORE_DOC.replace('    B = "B"', '    C = "B"')},
                 impl, 'EM-MEMBER-MISMATCH', marker='成员名')
    ok &= sample('member-order-mismatch',
                 {'core': CORE_DOC.replace('    A = "A"\n    B = "B"',
                                           '    B = "B"\n    A = "A"')},
                 impl, 'EM-MEMBER-MISMATCH', marker='成员顺序')
    ok &= sample('member-value-mismatch',
                 {'core': CORE_DOC.replace('    A = "A"', '    A = "AA"')},
                 impl, 'EM-MEMBER-MISMATCH', marker='成员取值')

    ok &= sample('missing-impl',
                 patched(risk=RISK_DOC.replace('Gamma', 'Delta')),
                 impl, 'EM-MISSING-IMPL',
                 **{'local': {'Gamma': '自测样本', 'Local': '自测样本'}})
    ok &= sample('pending-drift', None, impl, 'EM-PENDING-DRIFT',
                 pending={'Alpha': '自测样本'})
    ok &= sample('pending-unused', None, impl, 'EM-PENDING-UNUSED',
                 pending={'NeverDeclared': '自测样本'})
    ok &= sample('unregistered-impl', None, impl, 'EM-UNREGISTERED-IMPL',
                 **{'local': {}})
    ok &= sample('local-drift', None, impl, 'EM-LOCAL-DRIFT',
                 **{'local': {'Local': '自测样本', 'Alpha': '自测样本'}})
    ok &= sample('local-stale', None, impl, 'EM-LOCAL-STALE',
                 **{'local': {'Local': '自测样本', 'Gone': '自测样本'}})
    ok &= sample('contract-ambiguous',
                 patched(dc=DC_DOC.replace('class Beta(Enum):\n    X = "X"',
                                           'class Alpha(Enum):\n    Z = "Z"')),
                 impl, 'EM-CONTRACT-AMBIGUOUS', min_contract=2,
                 **{'local': {'Beta': '自测样本', 'Local': '自测样本'}})
    ok &= sample('contract-shared-identical',
                 patched(dc=DC_DOC.replace(
                     'class Beta(Enum):\n    X = "X"',
                     'class Alpha(Enum):\n    A = "A"\n    B = "B"')),
                 impl, (), min_contract=2,
                 **{'local': {'Beta': '自测样本', 'Local': '自测样本'}})
    ok &= sample('impl-ambiguous', None,
                 {'enums.py': CLEAN_IMPL, 'more.py': CLEAN_IMPL},
                 'EM-IMPL-AMBIGUOUS')
    ok &= sample('impl-scan', None, {'enums.py': 'class Broken(:\n'},
                 'EM-IMPL-SCAN')
    ok &= sample('no-compare',
                 {'core': '# 核心契约\n\n```python\nfrom enum import Enum\n\n\n'
                          'class Pending(Enum):\n    P = "P"\n```\n'},
                 {'enums.py': 'from enum import Enum\n\n\nclass Local(Enum):\n'
                              '    L = "L"\n'},
                 'EM-NO-COMPARE', pending={'Pending': '自测样本'},
                 min_contract=1, min_impl=1)

    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


def main(argv):
    harden_stdout()
    if '--selftest' in argv:
        return selftest()
    root = None
    for arg in argv:
        if arg.startswith('--root='):
            root = arg.split('=', 1)[1]
        elif not arg.startswith('--'):
            root = arg
    root = os.path.abspath(root or os.path.join(ROOT, '..'))
    summary, findings = audit(root)
    return report(summary, findings)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
