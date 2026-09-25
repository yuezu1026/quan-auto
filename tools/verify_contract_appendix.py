#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""契约补全节门禁：主契约 G 节里新登记的类，必须与 quanauto/ 的实现逐条一致。

===== 这个门禁在还哪笔账 =====

B7 欠账（主契约引用了 37 个类型/异常却从未定义）是一次性补齐的：补法是往
`docs/智能量化交易平台-核心模块接口契约文档.md` **追加**一节 G，把实现里真实存在的类
逐字登记进文档（正文由 .docx 派生，只许追加，不许原地补）。追加的好处是「一个 agent 能
照着抄」；代价是文档从此多了一份**实现的副本**，而副本会漂移 —— 改一个默认值、加一个
字段、换一个枚举取值、动一个错误码，文档不会自己知道，读它的人也没法看出来。

===== 它挡的是哪几种假绿 =====

1) **抄错**。写文档时看错一行（`count: int = 0` 抄成 `= 1`）就会让「规范」变成
   「看起来像规范的错答案」：此后照着文档写的新适配器与已有实现不一致，而全部门禁
   都绿 —— 因为没有任何判据拿文档与实现对过账。
2) **契约先行的登记腐化**。有几个名字是「契约已约定、实现还没做」（归因/适配引擎）。
   它们被登记在 `CONTRACT_FIRST` 里免检；一旦实现补上了，登记必须当场作废并改为逐条
   比对，否则它就成了永久免检的挡箭牌。
3) **空转**。找不到 G 节 / 找不到 ```python 块 / 解析出 0 个声明 / 扫到 0 个实现类 ——
   这四种情况下所有探测器都在空转，却会打印「0 问题 PASS」。四种各有一条守卫，
   且守卫命中时**跳过比对**并在报告里写明「比对未执行」。
4) **T1 的重抄被删掉**。主契约有两个 python 块在 .docx 转换中损坏（`ast` 解析不了），
   所以 `core-contract-refs` 的 T1 会一直报它们 —— 本节把它们重抄成可复制的副本。
   删掉重抄 = 悄悄丢掉「可复制」这个性质，而 T1 的条数不会变（原文块还在）。

===== 边界（这个门禁不证明什么）=====

- 只比 **基类 / 字段（名字+类型+默认值，按序）/ 枚举取值表 / 异常的 code**。不比方法、
  不比 docstring、不比注释。所以「文档里这些类能跑起来」它没有证明。
- `CONTRACT_FIRST` 里的名字只证明**它还在文档里、且实现里仍然没有**。它的字段该长什么
  样，本门禁**没有**独立判据（没有第二份来源可比）—— 靠人工评审，是**已知缺口**，
  写在这里而不是留白。
- 只证明「本节 = 实现」。**不证明这些类应该长这样**：权威在 §1/§2 正文，以及
  `tools/verify_contract_signature.py` 用清单管的那 9 个契约类（那个门禁比的是参数
  名/字段名，不比类型与默认值；本节这批类连类型与默认值一起比）。
- 扫描面是 `quanauto/*.py`，不含测试与工具；`quanauto` 里同名的类只按文件名字典序取第一个，
  第二个会被当成歧义报出来。
"""

import ast
import os
import re
import sys
import tempfile

CORE_REL = os.path.join('docs', '智能量化交易平台-核心模块接口契约文档.md')
IMPL_REL = 'quanauto'

# G 节的标题：`### G. ...` / `### G、...` / `### G: ...`
SECTION_RE = re.compile(r'^###[ \t]*G[.、:：][ \t]*(\S.*)$', re.M)
NEXT_H3_RE = re.compile(r'^###[ \t]', re.M)
PY_BLOCK_RE = re.compile(r'```python\r?\n(.*?)```', re.S)

# 契约先行：本节登记了、但 `quanauto/` 里**故意还没有**的类（归因/适配引擎按迭代计划
# 推到实盘之前）。免检是有条件的：一旦实现里出现同名类，AX-PENDING-DRIFT 当场红。
CONTRACT_FIRST = {
    'AdaptabilityRule': '§1.3.1 add_adaptability_rule 的入参；适配引擎未实现（迭代计划裁决）',
    'SectorAttribution': '§2.6.2 BrinsonResult.sector_breakdown 的元素类型；归因引擎未实现',
    'AdaptabilityCalculationError': '§1.3.1 calculate_adaptability 的 Raises；对应错误码 STRATEGY_007',
    'MarketStateFetchError': '§1.3.1 get_market_state 的 Raises；对应错误码 STRATEGY_008',
    'CheckpointError': '§2.3 检查点保存/恢复失败时抛出；对应错误码 BACKTEST_003 / BACKTEST_004',
    'ResultSerializer': '§2.8.1（T1 损坏块）的重抄；结果序列化尚未实现',
}

# T1 重抄：这两个块的原文不可解析（.docx 转换损坏），本节给出可复制的副本。
T1_REQUIRED = ('Account', 'ResultSerializer')

# 实现里出现这些名字不算漂移（扫描时会跳过，避免把无关类当成本节声明）。
EXPECTED_CODE = 'AX'


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


# ── 文档侧：找 G 节、取块、取声明 ────────────────────────────────────
def section_of(text):
    """返回 (section_text, start_offset, heading_line_no, end_offset)。"""
    m = SECTION_RE.search(text)
    if m is None:
        return None, -1, 0, -1
    start = m.start()
    nxt = NEXT_H3_RE.search(text, m.end())
    end = nxt.start() if nxt else len(text)
    return text[start:end], start, text.count('\n', 0, start) + 1, end


def blocks_of(text, start, end):
    """G 节里的 ```python 块，返回 [(源码, 文档内行号)]。"""
    out = []
    for m in PY_BLOCK_RE.finditer(text, start, end):
        out.append((m.group(1), text.count('\n', 0, m.start()) + 1))
    return out


def shape_of_class(node):
    bases = [ast.unparse(b) for b in node.bases]
    fields = []
    members = []
    code = None
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append((stmt.target.id, ast.unparse(stmt.annotation),
                           ast.unparse(stmt.value) if stmt.value is not None else None))
        elif (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            try:
                value = ast.literal_eval(stmt.value)
            except Exception:
                continue
            if not isinstance(value, str):
                continue
            if stmt.targets[0].id == 'code':
                code = value
            else:
                members.append((stmt.targets[0].id, value))
    return {'kind': 'enum' if 'Enum' in bases else 'class', 'bases': bases,
            'fields': fields, 'members': members, 'code': code}


def decls_of(src, line_no):
    """一个 python 块 → ({名字: shape}, [问题])。解析不了就报问题，不静默丢。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {}, ['文档 %d 行的块不可解析：%s（块内第 %s 行）'
                    % (line_no, exc.msg, exc.lineno)]
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out[node.name] = shape_of_class(node)
            continue
        made = newtype_of(node)
        if made is not None:
            out[made[0]] = {'kind': 'newtype', 'bases': [], 'fields': [], 'members': [],
                            'code': None, 'newtype': made[1]}
    return out, []


# ── 实现侧 ────────────────────────────────────────────────────────────
def newtype_of(node):
    """`X = NewType('X', str)` → ('X', 'str')；不是就返回 None。"""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
        return None
    args = node.value.args
    if getattr(node.value.func, 'id', '') != 'NewType' or len(args) != 2:
        return None
    return target.id, ast.unparse(args[1])


def scan_impl(root):
    """扫 `quanauto/*.py` → ({名字: shape（含 source）}, 文件数, [问题])。"""
    directory = os.path.join(root, IMPL_REL)
    if not os.path.isdir(directory):
        return {}, 0, []
    files = sorted(name for name in os.listdir(directory) if name.endswith('.py'))
    out = {}
    for name in files:
        path = os.path.join(directory, name)
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError as exc:
            return out, len(files), ['实现 %s 不可解析：%s' % (name, exc.msg)]
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                key = node.name
                shape = shape_of_class(node)
            else:
                made = newtype_of(node)
                if made is None:
                    continue
                key, base = made
                shape = {'kind': 'newtype', 'bases': [], 'fields': [], 'members': [],
                         'code': None, 'newtype': base}
            shape['source'] = '%s:%d' % (name, node.lineno)
            if key in out:
                out[key].setdefault('duplicates', []).append(shape['source'])
                continue
            out[key] = shape
    return out, len(files), []


# ── 比对 ──────────────────────────────────────────────────────────────
def field_msg(name, dec_fields, impl_fields):
    dec_names = [item[0] for item in dec_fields]
    impl_names = [item[0] for item in impl_fields]
    out = []
    if len(dec_names) != len(impl_names):
        out.append('字段数不一致 %s：文档 %d 个 %s / 实现 %d 个 %s'
                   % (name, len(dec_names), dec_names, len(impl_names), impl_names))
        return out
    if dec_names != impl_names:
        out.append('字段顺序/名字不一致 %s：文档 %s / 实现 %s'
                   % (name, dec_names, impl_names))
        return out
    for (dn, dt, dd), (inn, it, idd) in zip(dec_fields, impl_fields):
        if dt != it:
            out.append('字段类型不一致 %s.%s：文档 %s / 实现 %s' % (name, dn, dt, it))
        if (dd is None) != (idd is None) or (dd is not None and dd != idd):
            out.append('字段默认值不一致 %s.%s：文档 %s / 实现 %s'
                       % (name, dn, dd, idd))
    return out


def compare_one(name, dec, impl):
    source = impl.get('source', '?')
    out = []
    if dec['bases'] != impl['bases']:
        out.append('基类不一致 %s：文档 %s / 实现 %s（%s）'
                   % (name, dec['bases'], impl['bases'], source))
    if dec['kind'] == 'newtype' or impl['kind'] == 'newtype':
        if dec.get('newtype') != impl.get('newtype'):
            out.append('NewType 基底不一致 %s：文档 %s / 实现 %s（%s）'
                       % (name, dec.get('newtype'), impl.get('newtype'), source))
        return out
    if dec['fields'] or impl['fields']:
        out.extend(field_msg(name, dec['fields'], impl['fields']))
    if 'Enum' in dec['bases'] or 'Enum' in impl['bases']:
        if dec['members'] != impl['members']:
            out.append('枚举取值表不一致 %s：文档 %s / 实现 %s（%s）'
                       % (name, dec['members'], impl['members'], source))
    if dec.get('code') != impl.get('code'):
        out.append('code 不一致 %s：文档 %r / 实现 %r（%s）'
                   % (name, dec.get('code'), impl.get('code'), source))
    return out


def audit(root, contract_first=None, t1_required=None):
    """返回 (summary, findings)。findings = [(code, message)]，已排序去重。"""
    contract_first = CONTRACT_FIRST if contract_first is None else contract_first
    t1_required = T1_REQUIRED if t1_required is None else t1_required
    findings = []
    summary = {'root': root, 'section_line': 0, 'blocks': 0, 'decls': 0,
               'impl_files': 0, 'impl_classes': 0, 'impl_newtypes': 0,
               'matched': 0, 'first': 0, 'skipped': False}

    core_path = os.path.join(root, CORE_REL)
    if not os.path.isfile(core_path):
        return summary, [('AX-NO-SECTION', '找不到主契约 %s（文件缺失 ⇒ 本门禁无法判，'
                                           '拒绝通过）' % CORE_REL)]
    text = read_text(core_path)
    section, start, line_no, end = section_of(text)
    if section is None:
        return summary, [('AX-NO-SECTION',
                          '主契约里找不到 `### G. ...` 这一节（标题改了或整节被删 ⇒ '
                          '后面的比对一条都不会跑，拒绝通过）')]
    summary['section_line'] = line_no

    blocks = blocks_of(text, start, end)
    summary['blocks'] = len(blocks)
    if not blocks:
        return summary, [('AX-NO-BLOCKS', 'G 节（文档 %d 行起）里没有一个 ```python 块 '
                                          '⇒ 提取为空，比对形同虚设' % line_no)]

    decls = {}
    for src, block_line in blocks:
        got, problems = decls_of(src, block_line)
        for problem in problems:
            findings.append(('AX-UNPARSABLE', problem))
        decls.update(got)
    summary['decls'] = len(decls)
    if not decls:
        return summary, findings + [('AX-NO-DECLARATIONS',
                                     'G 节有 %d 个 python 块但解析出 0 个类/新类型 '
                                     '⇒ 提取为空，比对形同虚设' % len(blocks))]

    impl, impl_files, impl_problems = scan_impl(root)
    summary['impl_files'] = impl_files
    summary['impl_classes'] = len(impl)
    # 这张表把 `NewType` 别名也当一个可比对对象收进来了 ⇒ 计数必须拆开打印。
    # 否则报告里的 `classes=N` 会比真实的类数多出「别名」那部分（2026-09-25 实测：
    # 145 = 144 个类 + 1 个 `OrderId`），与缺口清单 B9.2 的 144 看似矛盾。
    summary['impl_newtypes'] = sum(
        1 for shape in impl.values() if shape.get('kind') == 'newtype')
    for problem in impl_problems:
        findings.append(('AX-IMPL-SCAN', problem))
    for name, shape in sorted(impl.items()):
        if shape.get('duplicates'):
            findings.append(('AX-IMPL-AMBIGUOUS',
                             'quanauto 里有 %d 个同名类 %s（%s）—— 比对对象不唯一'
                             % (len(shape['duplicates']) + 1, name,
                                ', '.join([shape.get('source', '?')]
                                          + shape['duplicates']))))
    if not impl:
        return summary, findings + [('AX-NO-IMPL',
                                     '扫 %s/*.py 得到 0 个类 ⇒ 没有可比对象，'
                                     '比对形同虚设' % IMPL_REL)]

    for name in sorted(contract_first):
        if name not in decls:
            findings.append(('AX-UNUSED-REGISTRY',
                             'CONTRACT_FIRST 登记了 %s，但 G 节里没有它的声明 —— '
                             '登记项本身失效' % name))
        if name in impl:
            findings.append(('AX-PENDING-DRIFT',
                             '%s 已被实现（%s）—— 契约先行的免检登记当场作废：'
                             '把它从 CONTRACT_FIRST 移除并逐条比对'
                             % (name, impl[name].get('source', '?'))))

    for name in t1_required:
        if name not in decls:
            findings.append(('AX-T1-COPY',
                             'T1 重抄 %s 不在 G 节里 —— 损坏块的可复制副本没了'
                             % name))

    for name in sorted(decls):
        dec = decls[name]
        if name in contract_first:
            summary['first'] += 1
            continue
        if name not in impl:
            findings.append(('AX-NOT-REGISTERED',
                             'G 节的 %s 在 %s 里不存在，也没登记进 CONTRACT_FIRST —— '
                             '要么实现补上它，要么登记它是契约先行' % (name, IMPL_REL)))
            continue
        summary['matched'] += 1
        for message in compare_one(name, dec, impl[name]):
            findings.append(('AX-IMPL-MISMATCH', message))
    summary['skipped'] = summary['matched'] == 0
    return summary, sorted(set(findings))


def report(summary, findings):
    print('root:            %s' % summary['root'])
    print('contract:        %s' % CORE_REL)
    print('G section:       line %d' % summary['section_line'])
    print('python blocks:   %d' % summary['blocks'])
    print('declarations:    %d' % summary['decls'])
    print('impl scan:       %s/*.py files=%d classes=%d newtypes=%d'
          % (IMPL_REL, summary['impl_files'],
             summary['impl_classes'] - summary['impl_newtypes'],
             summary['impl_newtypes']))
    print('compared:        matched=%d contract-first=%d'
          % (summary['matched'], summary['first']))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    if summary['skipped']:
        print('NOTE: 一个声明都没比过 —— 上面的结论只覆盖了守卫与登记项')
    print('verdict: %s (%d finding(s))'
          % ('PASS' if not findings else 'DIRTY', len(findings)))
    return 1 if findings else 0


# ── 自测：每个探测器一个样本 ──────────────────────────────────────────
HEAD = '# 测试契约\n\n正文一句。\n\n## 附录\n\n### F. 假附录\n\n正文。\n\n'

BASE_DOC = HEAD + '''### G. 补全登记（2026-09-25 追加）

#### G1. 数据类

```python
from dataclasses import dataclass


@dataclass
class Thing:
    """假的。"""

    name: str
    count: int = 0
```

#### G2. 异常与契约先行

```python
class BaseError(Exception):
    code = "X_000"


class ThingError(BaseError):
    code = "X_001"


class Pending:
    """契约先行：实现里故意还没有。"""

    note: str = ""


ThingId = NewType("ThingId", str)
```
'''

BASE_IMPL = '''from dataclasses import dataclass
from typing import NewType


@dataclass
class Thing:
    name: str
    count: int = 0


class BaseError(Exception):
    code = "X_000"


class ThingError(BaseError):
    code = "X_001"


ThingId = NewType("ThingId", str)
'''

CLEAN_KW = {'contract_first': {'Pending': '自测样本'}, 't1_required': ('Thing',)}

SANDBOX = {'root': None}


def sample(name, doc, impl, expect, marker='', **kw):
    """一个样本：写沙箱 → 跑 audit → 断言命中的代码与消息片段。

    断言到「报出来的正是那个检查码」为止；只断言「有 FINDING」会让「变异没打到分支」
    与「探测器不存在」长得一模一样。
    """
    root = os.path.join(SANDBOX['root'], name)
    write_text(os.path.join(root, CORE_REL), doc)
    if impl is not None:
        for fname, src in impl.items():
            write_text(os.path.join(root, IMPL_REL, fname), src)
    else:
        os.makedirs(os.path.join(root, IMPL_REL), exist_ok=True)
    summary, findings = audit(root, **dict(CLEAN_KW, **kw))
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
    SANDBOX['root'] = tempfile.mkdtemp(prefix='contract-appendix-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    impl = {'fixture.py': BASE_IMPL}
    ok = True

    ok &= sample('clean-control', BASE_DOC, impl, ())

    ok &= sample('no-section', BASE_DOC.replace('### G. 补全登记（2026-09-25 追加）',
                                                '### H. 别的节'), impl,
                 'AX-NO-SECTION')
    ok &= sample('no-blocks', HEAD + '### G. 补全登记（2026-09-25 追加）\n\n只有散文，'
                                     '没有代码块。\n', impl, 'AX-NO-BLOCKS')
    ok &= sample('no-declarations',
                 HEAD + '### G. 补全登记（2026-09-25 追加）\n\n```python\nx = 1\n```\n',
                 impl, 'AX-NO-DECLARATIONS')
    ok &= sample('no-impl', BASE_DOC, None, 'AX-NO-IMPL')
    ok &= sample('unparsable', BASE_DOC + '\n```python\nclass Broken(:\n```\n', impl,
                 'AX-UNPARSABLE')

    extra = BASE_DOC.replace('ThingId = NewType("ThingId", str)',
                             'ThingId = NewType("ThingId", str)\n\n\n'
                             'class MysteryThing:\n    note: str = ""')
    ok &= sample('not-registered', extra, impl, 'AX-NOT-REGISTERED')

    ok &= sample('base-mismatch', BASE_DOC.replace('class ThingError(BaseError):',
                                                   'class ThingError(Exception):'),
                 impl, 'AX-IMPL-MISMATCH', marker='基类')
    ok &= sample('type-mismatch', BASE_DOC.replace('    name: str',
                                                   '    name: bytes'),
                 impl, 'AX-IMPL-MISMATCH', marker='字段类型')
    ok &= sample('default-mismatch', BASE_DOC.replace('    count: int = 0',
                                                      '    count: int = 1'),
                 impl, 'AX-IMPL-MISMATCH', marker='默认值')
    ok &= sample('field-dropped', BASE_DOC.replace('    count: int = 0', ''),
                 impl, 'AX-IMPL-MISMATCH', marker='字段数')
    ok &= sample('field-order', BASE_DOC.replace('    name: str\n    count: int = 0',
                                                 '    count: int = 0\n    name: str'),
                 impl, 'AX-IMPL-MISMATCH', marker='字段顺序')
    ok &= sample('code-mismatch', BASE_DOC.replace('    code = "X_001"',
                                                   '    code = "X_002"'),
                 impl, 'AX-IMPL-MISMATCH', marker='code')
    ok &= sample('newtype-mismatch',
                 BASE_DOC.replace('NewType("ThingId", str)', 'NewType("ThingId", int)'),
                 impl, 'AX-IMPL-MISMATCH', marker='NewType')

    enum_doc = BASE_DOC.replace('class Pending:', 'class Mode(Enum):\n'
                                                    '    ON = "ON"\n'
                                                    '    OFF = "OFF"\n\n\n'
                                                    'class Pending:')
    enum_impl = dict(impl)
    enum_impl['enums.py'] = 'from enum import Enum\n\n\nclass Mode(Enum):\n' \
                            '    ON = "ON"\n    OFF = "ON"\n'
    ok &= sample('enum-mismatch', enum_doc, enum_impl, 'AX-IMPL-MISMATCH',
                 marker='枚举取值')

    ok &= sample('pending-drift', BASE_DOC, impl, 'AX-PENDING-DRIFT',
                 contract_first={'Thing': '自测样本'})
    ok &= sample('registry-unused', BASE_DOC, impl, 'AX-UNUSED-REGISTRY',
                 contract_first={'Pending': '自测样本', 'NeverDeclared': '自测样本'})
    ok &= sample('t1-copy-missing', BASE_DOC, impl, 'AX-T1-COPY',
                 t1_required=('Thing', 'Gone'))
    ok &= sample('emptied-doc', '', impl, 'AX-NO-SECTION')

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
    root = os.path.abspath(root or os.path.join(os.path.dirname(__file__), '..'))
    summary, findings = audit(root)
    return report(summary, findings)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
