#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""platform-spec-parity —— Java 侧那张「手抄的规格表」与 `quanauto/dashboard.py` 的双向比对。

**为什么需要它**：`platform/` 是 2026-09-29 追加的平台层读取侧切片（`docs/智能量化交易平台.md`
附录C）。它把 `quanauto/dashboard.py` 的 14 条指标规格（键 / 分组 / 标签 / 量纲 / 小数位）
**逐字抄**了一份到 `platform/api/src/main/java/com/quanauto/dashboard/MetricSpec.java`。
抄完之后，**没有任何东西盯着这两份** —— `MetricSpec.java` 自己的 Javadoc 写着
「这是一份手抄的副本，而目前没有任何门禁盯着它」，附录C §C.5 第 2 条把它登记成缺口。

代价是明确的：`dashboard.py` 改了小数位、标签、量纲或分组，Java 侧屏幕会**静默地**
与 I4 命令行看板不一致 —— 两侧各自都绿，缝上全错（附录C §C.5 第 4 条记的正是同一个味道）。

**比什么**（顺序敏感 —— 顺序即界面顺序）：

  ① 量纲常量 5 条（`UNIT_PERCENT` 等）与分组常量 4 条（`GROUP_RETURN` 等）的**取值**；
  ② `METRIC_SPECS` 的**行数**、逐行 `key / group / label / unit / digits`、以及**行序**；
  ③ `STRUCTURE_COUNTS` 三个结构计数的名字与顺序。

**边界（它证明不了什么）**：

  * 只比这份**声明式规格表**。REST 形状、展示文本、曲线字段、前端有没有偷偷 `toFixed`
    都不归它管 —— 「两侧打印出的串是否相同」自 2026-10-01 起归 `platform-text-parity`
    （`tools/verify_platform_text_parity.py` + Java 侧 `FormatParityTest`，由
    `platform-runtime` 的真 `mvn test` 跑）；「**活着的服务端**下发了什么」仍归
    `platform/check_text_parity.py` 那个手动脚本（它需要一个活着的 JVM（「环境没起来就
    判红」的门禁本身就是判据缺陷）⇒ 它**不是门禁**，本文件也不假装替代它）。
    本门禁**不做端到端**：它只保证两份声明表逐字段一致。
  * 不比 Java 的 Javadoc / Python 的 docstring 文案，也不判断「某条指标该不该在」。
  * 它**只解析文本与 AST**：不 import `quanauto.dashboard`、也不编译 Java。否则判据会变成
    「拿实现核对自己」，而且会给两边都引入本不该有的运行时依赖。
  * 提取变薄/为空一律**拒绝通过**（`SPEC-NO-ROWS-*` / `SPEC-THIN-*`）：提取正则一旦失配，
    下面所有比对都在空转，却会打印「0 issue(s) PASS」—— 报告看起来比真通过还干净。
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_REL = os.path.join('quanauto', 'dashboard.py')
JAVA_REL = os.path.join('platform', 'api', 'src', 'main', 'java', 'com',
                        'quanauto', 'dashboard', 'MetricSpec.java')

UNIT_NAMES = ('UNIT_PERCENT', 'UNIT_RATIO', 'UNIT_COUNT', 'UNIT_MONEY', 'UNIT_DAYS')
GROUP_NAMES = ('GROUP_RETURN', 'GROUP_RISK', 'GROUP_TRADE', 'GROUP_COST')

# 实测 14 行（2026-09-30）。这条门槛只是「正则整体失配」的守卫 —— 真少一行由
# 行数比对（SPEC-ROW-COUNT）与逐行比对抓，不靠这个数。门槛取 5 是刻意的低值：
# 它是「提取器坏了」的报警器，不是「规格表该有几条」的裁决（那由上游 `PerformanceMetrics`
# 的字段集决定，由 `tools/verify_dashboard.py` 的 C2 与 `tests/test_dashboard.py` 盯着）。
MIN_ROWS = 5

JAVA_CONST_RE = re.compile(
    r'public\s+static\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"\s*;')
JAVA_SPECS_BLOCK_RE = re.compile(r'METRIC_SPECS\s*=\s*List\.of\((.*?)\)\s*;', re.S)
JAVA_COUNTS_BLOCK_RE = re.compile(r'STRUCTURE_COUNTS\s*=\s*List\.of\((.*?)\)\s*;', re.S)
JAVA_ROW_RE = re.compile(
    r'new\s+MetricSpec\(\s*"([^"]*)"\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'
    r'"([^"]*)"\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*(\d+)\s*\)')
JAVA_STR_RE = re.compile(r'"([^"]*)"')
# 用来数「块里到底有几处行」。解析出来的行数对不上它 ⇒ 有行**没被比对**。
JAVA_ROW_TOKEN = 'new MetricSpec('

# 两侧常量对照的分组：名字 -> 检查码
CONST_GROUPS = (('量纲', UNIT_NAMES, 'SPEC-CONST-UNIT'),
                ('分组', GROUP_NAMES, 'SPEC-CONST-GROUP'))
# 行内逐字段对照：字段 -> 检查码
ROW_FIELD_CODES = (('group', 'SPEC-ROW-GROUP'), ('label', 'SPEC-ROW-LABEL'),
                   ('unit', 'SPEC-ROW-UNIT'), ('digits', 'SPEC-ROW-DIGITS'))

_MISSING = object()


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


def _show(value):
    return '(缺失)' if value is _MISSING else repr(value)


# ── Python 侧：AST，不 import ────────────────────────────────────────
def scan_python(path):
    """读 `quanauto/dashboard.py` -> (consts, rows, counts, findings)。

    rows = [{'key','group','label','unit','digits','resolved'}]，按源码顺序。
    解不出来的行**照样占一行**（键集/行数不会被悄悄改小），只是不参与逐字段比对，
    并自己报一条 `SPEC-PY-UNRESOLVED`。
    """
    findings = []
    try:
        src = read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [], [], [('SPEC-MISSING-PY', '%s 读不出来：%s' % (PY_REL, exc))]
    try:
        tree = ast.parse(src, filename=PY_REL)
    except SyntaxError as exc:
        return {}, [], [], [('SPEC-SCAN-PY', '%s 解析失败（第 %s 行）：%s'
                             % (PY_REL, exc.lineno, exc.msg))]

    def target(node):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            return node.targets[0].id, node.value
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.value is not None):
            return node.target.id, node.value
        return None, None

    consts = {}
    specs_node = None
    counts_node = None
    for node in tree.body:
        name, value = target(node)
        if name is None:
            continue
        if name == 'METRIC_SPECS':
            specs_node = value
        elif name == 'STRUCTURE_COUNTS':
            counts_node = value
        else:
            try:
                consts[name] = ast.literal_eval(value)
            except (ValueError, SyntaxError, TypeError):
                pass

    def resolve(node):
        try:
            return True, ast.literal_eval(node)
        except (ValueError, SyntaxError, TypeError):
            pass
        if isinstance(node, ast.Name) and node.id in consts:
            return True, consts[node.id]
        return False, None

    rows = []
    if isinstance(specs_node, ast.Tuple):
        for index, elt in enumerate(specs_node.elts):
            call = (isinstance(elt, ast.Call) and len(elt.args) == 5
                    and isinstance(elt.func, ast.Name)
                    and elt.func.id == 'MetricSpec')
            values = [resolve(a) for a in elt.args] if call else []
            if not call or not all(ok for ok, _ in values):
                key = values[0][1] if values and values[0][0] else '<unresolved#%d>' % index
                rows.append({'key': key, 'group': None, 'label': None, 'unit': None,
                             'digits': None, 'resolved': False})
                findings.append(('SPEC-PY-UNRESOLVED',
                                 'Python 侧第 %d 行（0 基）没能解析出 5 个字段 —— '
                                 '这一行不参与逐字段比对（行数与键集仍按原样计入）' % index))
                continue
            key, group, label, unit, digits = (value for _, value in values)
            rows.append({'key': key, 'group': group, 'label': label, 'unit': unit,
                         'digits': digits, 'resolved': True})

    counts = []
    if isinstance(counts_node, (ast.Tuple, ast.List)):
        for elt in counts_node.elts:
            ok, value = resolve(elt)
            counts.append(value if ok else None)
    return consts, rows, counts, findings


# ── Java 侧：文本，不编译 ────────────────────────────────────────────
def scan_java(path):
    """读 `MetricSpec.java` -> (consts, rows, counts, raw_rows, findings)。"""
    try:
        src = read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [], [], 0, [('SPEC-MISSING-JAVA', '%s 读不出来：%s' % (JAVA_REL, exc))]

    consts = dict(JAVA_CONST_RE.findall(src))
    findings = []
    rows = []
    raw_rows = 0
    block = JAVA_SPECS_BLOCK_RE.search(src)
    if block is not None:
        body = block.group(1)
        raw_rows = body.count(JAVA_ROW_TOKEN)
        if len(JAVA_ROW_RE.findall(body)) != raw_rows:
            rows = JAVA_ROW_RE.findall(body)
            findings.append(('SPEC-JAVA-UNPARSED',
                             'Java 侧 METRIC_SPECS 块里有 %d 处 `%s`，正则只解析出 %d 行 '
                             '—— 形状变了，**剩下的行没有被比对**'
                             % (raw_rows, JAVA_ROW_TOKEN, len(rows))))
        else:
            rows = JAVA_ROW_RE.findall(body)

    parsed = []
    for key, group_name, label, unit_name, digits in rows:
        parsed.append({
            'key': key,
            'group': consts.get(group_name, '<未定义常量:%s>' % group_name),
            'label': label,
            'unit': consts.get(unit_name, '<未定义常量:%s>' % unit_name),
            'digits': int(digits),
            'resolved': True,
        })

    counts_block = JAVA_COUNTS_BLOCK_RE.search(src)
    counts = JAVA_STR_RE.findall(counts_block.group(1)) if counts_block else []
    return consts, parsed, counts, raw_rows, findings


# ── 比对 ─────────────────────────────────────────────────────────────
def audit(root, py_rel=PY_REL, java_rel=JAVA_REL, min_rows=MIN_ROWS):
    """返回 (summary, findings)。findings = [(code, message)]，已排序去重。"""
    py_consts, py_rows, py_counts, py_findings = scan_python(os.path.join(root, py_rel))
    ja_consts, ja_rows, ja_counts, ja_raw, ja_findings = scan_java(
        os.path.join(root, java_rel))
    findings = list(py_findings) + list(ja_findings)

    summary = {
        'root': root,
        'py_rows': len(py_rows), 'ja_rows': len(ja_rows), 'ja_raw_rows': ja_raw,
        'py_units': sum(1 for n in UNIT_NAMES if n in py_consts),
        'ja_units': sum(1 for n in UNIT_NAMES if n in ja_consts),
        'py_groups': sum(1 for n in GROUP_NAMES if n in py_consts),
        'ja_groups': sum(1 for n in GROUP_NAMES if n in ja_consts),
        'py_counts': py_counts, 'ja_counts': ja_counts,
        'compared': 0,
    }

    # ── 空转守卫：提取为空 / 变薄 ⇒ 拒绝通过 ────────────────────────────
    # 顺序刻意在前：一旦提取失败，下面每一条比对都只是在空转。
    if not py_rows:
        findings.append(('SPEC-NO-ROWS-PY',
                         'Python 侧 METRIC_SPECS 提取到 0 行 —— 后面每一条比对都会在空转，'
                         '拒绝通过'))
    elif len(py_rows) < min_rows:
        findings.append(('SPEC-THIN-PY',
                         'Python 侧只提取到 %d 行（门槛 %d）—— 提取器可能坏了，拒绝通过'
                         % (len(py_rows), min_rows)))
    if not ja_rows:
        findings.append(('SPEC-NO-ROWS-JAVA',
                         'Java 侧 METRIC_SPECS 提取到 0 行 —— 后面每一条比对都会在空转，'
                         '拒绝通过'))
    elif len(ja_rows) < min_rows:
        findings.append(('SPEC-THIN-JAVA',
                         'Java 侧只提取到 %d 行（门槛 %d）—— 提取器可能坏了，拒绝通过'
                         % (len(ja_rows), min_rows)))
    if not py_rows or not ja_rows:
        return summary, sorted(set(findings))

    # ── ① 常量取值 ────────────────────────────────────────────────────
    for label, names, code in CONST_GROUPS:
        for name in names:
            py_value = py_consts.get(name, _MISSING)
            ja_value = ja_consts.get(name, _MISSING)
            if py_value is _MISSING or ja_value is _MISSING:
                findings.append((code, '%s常量 %s 只在%s侧存在（Python=%s / Java=%s）'
                                 % (label, name,
                                    'Java' if py_value is _MISSING else 'Python',
                                    _show(py_value), _show(ja_value))))
            elif py_value != ja_value:
                findings.append((code, '%s常量 %s 两侧取值不同：Python=%r / Java=%r'
                                 % (label, name, py_value, ja_value)))

    # ── ② 逐行：行数、键集、行序、字段 ────────────────────────────────
    py_keys = [row['key'] for row in py_rows]
    ja_keys = [row['key'] for row in ja_rows]
    py_set, ja_set = set(py_keys), set(ja_keys)
    py_by_key = {row['key']: row for row in py_rows if row['resolved']}
    ja_by_key = {row['key']: row for row in ja_rows if row['resolved']}

    if len(py_rows) != len(ja_rows):
        findings.append(('SPEC-ROW-COUNT', '行数不同：Python %d 行 / Java %d 行'
                         % (len(py_rows), len(ja_rows))))
    for key in py_keys:
        if key not in ja_set:
            findings.append(('SPEC-ROW-MISSING', 'Java 侧没有 `%s`（Python 侧有）' % key))
    for key in ja_keys:
        if key not in py_set:
            findings.append(('SPEC-ROW-MISSING', 'Python 侧没有 `%s`（Java 侧有）' % key))
    # 只有「键集相同、行数相同、顺序不同」才报行序 —— 否则缺行会连带报一条噪声行序
    if py_set == ja_set and len(py_keys) == len(ja_keys) and py_keys != ja_keys:
        index, py_key, ja_key = next(
            (i, x, y) for i, (x, y) in enumerate(zip(py_keys, ja_keys)) if x != y)
        findings.append(('SPEC-ROW-ORDER',
                         '两侧键集合相同但**顺序**不同（顺序即界面顺序）：第 %d 行 '
                         'Python=%s / Java=%s' % (index, py_key, ja_key)))

    compared = 0
    for key in py_keys:
        py_row, ja_row = py_by_key.get(key), ja_by_key.get(key)
        if py_row is None or ja_row is None:
            continue
        compared += 1
        for field, code in ROW_FIELD_CODES:
            if py_row[field] != ja_row[field]:
                findings.append((code, '`%s` 的 %s 不同：Python=%r / Java=%r'
                                 % (key, field, py_row[field], ja_row[field])))
    summary['compared'] = compared

    # ── ③ 结构计数 ────────────────────────────────────────────────────
    if py_counts != ja_counts:
        findings.append(('SPEC-STRUCTURE-COUNTS',
                         'STRUCTURE_COUNTS 不同（含顺序）：Python=%r / Java=%r'
                         % (py_counts, ja_counts)))

    return summary, sorted(set(findings))


def report(summary, findings):
    print('root:            %s' % summary['root'])
    print('python spec:     %s' % PY_REL)
    print('  rows=%d  units=%d/%d  groups=%d/%d  counts=%s'
          % (summary['py_rows'], summary['py_units'], len(UNIT_NAMES),
             summary['py_groups'], len(GROUP_NAMES), summary['py_counts']))
    print('java spec:       %s' % JAVA_REL)
    print('  rows=%d (块里 `%s` 处数=%d)  units=%d/%d  groups=%d/%d  counts=%s'
          % (summary['ja_rows'], JAVA_ROW_TOKEN, summary['ja_raw_rows'],
             summary['ja_units'], len(UNIT_NAMES), summary['ja_groups'],
             len(GROUP_NAMES), summary['ja_counts']))
    print('compared:        %d 行按 key 两两比过（顺序敏感）+ 常量 %d 条 + 结构计数 1 组'
          % (summary['compared'], len(UNIT_NAMES) + len(GROUP_NAMES)))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not findings else 'DIRTY', len(findings)))
    return 1 if findings else 0


# ── 自测：每个探测器一个样本 ─────────────────────────────────────────
CLEAN_KW = {'min_rows': MIN_ROWS}
SANDBOX = {'root': None}

RAW_PY_NO_ROWS = 'METRIC_SPECS: Tuple[MetricSpec, ...] = ()\n'
RAW_JAVA_NO_ROWS = ('package com.quanauto.dashboard;\n\nimport java.util.List;\n\n'
                    'public record MetricSpec(String key) {\n'
                    '    public static final List<MetricSpec> METRIC_SPECS = List.of();\n'
                    '}\n')
RAW_PY_BROKEN = 'METRIC_SPECS: Tuple[MetricSpec, ...] = ((\n'

# 干净对照样本的输入**不取自真产物**。这不是洁癖：原先它直接拿 `ROOT` 当输入，
# 于是 Java 侧一被改坏，`selftest` 就跟着变成 FAIL —— 报告会写成「这门禁没证明自己
# 有牙」，而真实原因是**产物**坏了。两个完全不同的故障合成一个词，且假话恰好出现在
# 你最需要看自测结论的时刻。修法：干净对照用这份**自造**的一致样本（它同时把计数逻辑
# 钉在一个已知输入上），真产物另跑一条 advisory 行，只印结论、不参与裁决。
RAW_PY_CLEAN = '''from typing import Tuple

UNIT_PERCENT = "percent"
UNIT_RATIO = "ratio"
UNIT_COUNT = "count"
UNIT_MONEY = "money"
UNIT_DAYS = "days"

GROUP_RETURN = "\u6536\u76ca"
GROUP_RISK = "\u98ce\u9669"
GROUP_TRADE = "\u6210\u4ea4"
GROUP_COST = "\u6210\u672c"

METRIC_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec("alpha", GROUP_RETURN, "\u7532", UNIT_PERCENT, 4),
    MetricSpec("beta", GROUP_RETURN, "\u4e59", UNIT_MONEY, 2),
    MetricSpec("gamma", GROUP_RISK, "\u4e19", UNIT_RATIO, 4),
    MetricSpec("delta", GROUP_TRADE, "\u4e01", UNIT_COUNT, 0),
    MetricSpec("epsilon", GROUP_COST, "\u620a", UNIT_DAYS, 2),
)

STRUCTURE_COUNTS = ("equity_curve", "trades", "orders")
'''

RAW_JAVA_CLEAN = '''package com.quanauto.dashboard;

import java.util.List;

public record MetricSpec(String key, String group, String label, String unit, int digits) {

    public static final String UNIT_PERCENT = "percent";
    public static final String UNIT_RATIO = "ratio";
    public static final String UNIT_COUNT = "count";
    public static final String UNIT_MONEY = "money";
    public static final String UNIT_DAYS = "days";

    public static final String GROUP_RETURN = "\u6536\u76ca";
    public static final String GROUP_RISK = "\u98ce\u9669";
    public static final String GROUP_TRADE = "\u6210\u4ea4";
    public static final String GROUP_COST = "\u6210\u672c";

    public static final List<MetricSpec> METRIC_SPECS = List.of(
            new MetricSpec("alpha", GROUP_RETURN, "\u7532", UNIT_PERCENT, 4),
            new MetricSpec("beta", GROUP_RETURN, "\u4e59", UNIT_MONEY, 2),
            new MetricSpec("gamma", GROUP_RISK, "\u4e19", UNIT_RATIO, 4),
            new MetricSpec("delta", GROUP_TRADE, "\u4e01", UNIT_COUNT, 0),
            new MetricSpec("epsilon", GROUP_COST, "\u620a", UNIT_DAYS, 2));

    public static final List<String> STRUCTURE_COUNTS =
            List.of("equity_curve", "trades", "orders");
}
'''


class HarnessError(Exception):
    """变异没打中 ⇒ 这是**工具**的问题，不许静默变成「干净样本」。"""


def _must(text, old, new, label):
    """变异必须自 assert：锚命中恰好 1 次，否则 HARNESS 级失败。

    实测过的坑：锚写错一个字符 ⇒ `str.replace` 静默 no-op ⇒ 坏样本与原文件**逐字节
    相同**，于是「全绿」是在**未修改的文件**上得出的结论。
    """
    count = text.count(old)
    if count != 1:
        raise HarnessError('%s：锚 %r 命中 %d 次（必须恰好 1 次）' % (label, old, count))
    print('    applied: %-18s %r -> %r' % (label, old[:56], new[:56]))
    return text.replace(old, new)


def _write_pair(name, py_src, java_src, drop_py=False, drop_java=False):
    root = os.path.join(SANDBOX['root'], name)
    if not drop_py:
        write_text(os.path.join(root, PY_REL), py_src)
    if not drop_java:
        write_text(os.path.join(root, JAVA_REL), java_src)
    return root


def sample(name, expect, py_mut=None, java_mut=None, root=None, raw_py=None,
           raw_java=None, drop_py=False, drop_java=False, exact=False, marker='',
           want=None, advisory=False, **kw):
    """一个样本：造沙箱 → 跑 audit → 断言「报出来的正是那个检查码」。

    只断言「有 FINDING」会让「变异没打到分支」与「探测器不存在」长得一模一样。
    `advisory=True` 的样本只印结论、不参与自测裁决（用于「顺带看一眼真产物」）。
    """
    if root is None:
        try:
            py_src = raw_py if raw_py is not None else read_text(os.path.join(ROOT, PY_REL))
            ja_src = (raw_java if raw_java is not None
                      else read_text(os.path.join(ROOT, JAVA_REL)))
            if py_mut:
                py_src = _must(py_src, py_mut[0], py_mut[1], 'python 变异')
            if java_mut:
                ja_src = _must(ja_src, java_mut[0], java_mut[1], 'java 变异')
            root = _write_pair(name, py_src, ja_src, drop_py=drop_py,
                               drop_java=drop_java)
        except HarnessError as exc:
            if advisory:
                print('  SAMPLE NOTE (advisory): %s' % exc)
                return True
            print('  SAMPLE FAIL (harness): %s' % exc)
            return False

    summary, findings = audit(root, **dict(CLEAN_KW, **kw))
    codes = sorted({code for code, _ in findings})
    problems = []
    if expect is None or expect == ():
        if findings:
            problems.append('期望 0 报错（干净样本防误报），实际 %s' % codes)
    elif expect not in codes:
        problems.append('期望命中 %s，实际 %s' % (expect, codes))
    elif marker and not any(marker in msg for code, msg in findings if code == expect):
        problems.append('期望 %s 的消息里含 %r，实际 %s'
                        % (expect, marker, [m for c, m in findings if c == expect]))
    if exact and codes != [expect]:
        problems.append('期望**只有** %s 报出来，实际 %s' % (expect, codes))
    for key, value in (want or {}).items():
        if summary.get(key) != value:
            problems.append('期望 summary[%s] == %r，实际 %r'
                            % (key, value, summary.get(key)))
    for code, message in findings[:3]:
        print('    [%s] %s %s' % (name, code, message[:110]))
    if problems:
        if advisory:
            print('  SAMPLE NOTE (advisory, 不参与自测裁决): %s' % '; '.join(problems))
            return True
        print('  SAMPLE FAIL: %s' % '; '.join(problems))
        return False
    print('  sample ok: %s (expect=%s)' % (name, expect or 'clean'))
    return True


def fsample(name, expect, **kw):
    """变异样本一律建在**自造的干净样本**上，不读真产物。

    实测教训（2026-09-30）：第一版让变异样本从真产物出发，于是把 Java 侧改坏之后，
    **每一条** `exact=True` 的样本都连带报出那条无关的 `SPEC-ROW-DIGITS` ⇒ 6 条样本
    集体 SAMPLE FAIL。更糟的是方向：`--selftest` 的结论因此取决于**产物**的现状，
    而「产物坏了」和「自测坏了」是两回事，合成一个词就等于在最需要看自测结论的时刻
    说了一句假话（同族教训见记忆里 `selftest_exit_code` 那条）。
    """
    return sample(name, expect, raw_py=RAW_PY_CLEAN, raw_java=RAW_JAVA_CLEAN, **kw)


def selftest():
    """每个探测器至少一个会失败的样本；外加一个必须 0 报错的干净样本。"""
    SANDBOX['root'] = tempfile.mkdtemp(prefix='platform-specs-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    ok = True

    # 干净对照用**自造的一致样本**（不是真产物），见上面 RAW_*_CLEAN 的说明。
    # 它同时把计数逻辑钉在一个已知输入上：5 行、5 量纲、4 分组、3 结构计数、比 5 行。
    ok &= sample('clean-control', (), raw_py=RAW_PY_CLEAN, raw_java=RAW_JAVA_CLEAN,
                 want={'py_rows': 5, 'ja_rows': 5, 'ja_raw_rows': 5, 'compared': 5,
                       'py_units': 5, 'ja_units': 5, 'py_groups': 4, 'ja_groups': 4,
                       'py_counts': ['equity_curve', 'trades', 'orders'],
                       'ja_counts': ['equity_curve', 'trades', 'orders']})

    # ① 常量：两个方向 + 两类常量
    ok &= fsample('java-const-unit-drift', 'SPEC-CONST-UNIT', marker='UNIT_DAYS',
                  java_mut=('UNIT_DAYS = "days";', 'UNIT_DAYS = "day";'))
    ok &= fsample('python-const-unit-drift', 'SPEC-CONST-UNIT', marker='UNIT_MONEY',
                  py_mut=('UNIT_MONEY = "money"', 'UNIT_MONEY = "moneyy"'))
    ok &= fsample('java-const-group-drift', 'SPEC-CONST-GROUP', marker='GROUP_COST',
                  java_mut=('GROUP_COST = "成本";', 'GROUP_COST = "费用";'))
    ok &= fsample('java-const-missing', 'SPEC-CONST-UNIT', marker='只在Python侧存在',
                  java_mut=('public static final String UNIT_DAYS = "days";\n', ''))

    # ② 行：行数 / 缺行 / 行序 / 四个字段（两侧都测，证明判据不是单向的）
    alpha = 'new MetricSpec("alpha", GROUP_RETURN, "甲", UNIT_PERCENT, 4),'
    beta = 'new MetricSpec("beta", GROUP_RETURN, "乙", UNIT_MONEY, 2),'
    # 用非末行（delta）做锚：末行以 `));` 收尾（它是 `List.of(` 的最后一项），
    # 锚写成 `...,),​newline` 会命中 0 次 ⇒ _must 报 HARNESS-FAIL。
    delta = 'new MetricSpec("delta", GROUP_TRADE, "丁", UNIT_COUNT, 0),'
    ok &= fsample('java-row-count-drift', 'SPEC-ROW-COUNT', exact=True,
                  java_mut=(alpha, alpha + '\n            ' + alpha))
    ok &= fsample('java-row-missing', 'SPEC-ROW-MISSING', marker='delta',
                  java_mut=(delta + '\n', ''))
    ok &= fsample('java-row-reordered', 'SPEC-ROW-ORDER', exact=True,
                  java_mut=(alpha + '\n            ' + beta, beta + '\n            ' + alpha))
    ok &= fsample('java-row-group-drift', 'SPEC-ROW-GROUP', exact=True,
                  java_mut=('new MetricSpec("gamma", GROUP_RISK,',
                            'new MetricSpec("gamma", GROUP_TRADE,'))
    ok &= fsample('java-row-label-drift', 'SPEC-ROW-LABEL', exact=True,
                  java_mut=('"戊", UNIT_DAYS', '"戊戊", UNIT_DAYS'))
    ok &= fsample('java-row-unit-drift', 'SPEC-ROW-UNIT', exact=True,
                  java_mut=('new MetricSpec("gamma", GROUP_RISK, "丙", UNIT_RATIO, 4),',
                            'new MetricSpec("gamma", GROUP_RISK, "丙", UNIT_PERCENT, 4),'))
    ok &= fsample('java-row-digits-drift', 'SPEC-ROW-DIGITS', exact=True,
                  java_mut=(delta, 'new MetricSpec("delta", GROUP_TRADE, "丁", '
                                  'UNIT_COUNT, 1),'))
    ok &= fsample('python-row-digits-drift', 'SPEC-ROW-DIGITS', exact=True,
                  py_mut=('MetricSpec("epsilon", GROUP_COST, "戊", UNIT_DAYS, 2),',
                          'MetricSpec("epsilon", GROUP_COST, "戊", UNIT_DAYS, 3),'))

    # ③ 结构计数：名字集合与**顺序**都算
    ok &= fsample('java-structure-counts-drift', 'SPEC-STRUCTURE-COUNTS', exact=True,
                  java_mut=('List.of("equity_curve", "trades", "orders");',
                            'List.of("orders", "equity_curve", "trades");'))

    # ④ 提取本身坏掉：形状变了 / 解不出来 —— 每一种都必须自己报出来
    ok &= fsample('java-row-shape-changed', 'SPEC-JAVA-UNPARSED', marker='没有被比对',
                  java_mut=(', "戊", UNIT_DAYS, 2)', ', "戊", UNIT_DAYS, 2 + 0)'),
                  want={'ja_raw_rows': 5, 'ja_rows': 4, 'py_rows': 5})
    ok &= fsample('python-row-unresolved', 'SPEC-PY-UNRESOLVED', exact=True,
                  py_mut=('MetricSpec("gamma", GROUP_RISK,',
                          'MetricSpec("gamma", GROUP_UNDEFINED,'))

    # ⑤ 空转守卫：提取为空 / 变薄 / 文件读不出来 —— 全都不许打印「0 issue(s) PASS」
    ok &= sample('no-rows-python', 'SPEC-NO-ROWS-PY', exact=True,
                 raw_py=RAW_PY_NO_ROWS, raw_java=RAW_JAVA_CLEAN)
    ok &= sample('no-rows-java', 'SPEC-NO-ROWS-JAVA', exact=True,
                 raw_py=RAW_PY_CLEAN, raw_java=RAW_JAVA_NO_ROWS)
    ok &= sample('thin-rows-python', 'SPEC-THIN-PY', raw_py=RAW_PY_CLEAN,
                 raw_java=RAW_JAVA_CLEAN, min_rows=20)
    ok &= sample('thin-rows-java', 'SPEC-THIN-JAVA', raw_py=RAW_PY_CLEAN,
                 raw_java=RAW_JAVA_CLEAN, min_rows=20)
    ok &= sample('missing-java-file', 'SPEC-MISSING-JAVA', drop_java=True,
                 raw_py=RAW_PY_CLEAN, marker='读不出来')
    ok &= sample('missing-python-file', 'SPEC-MISSING-PY', drop_py=True,
                 raw_java=RAW_JAVA_CLEAN, marker='读不出来')
    ok &= sample('broken-python-syntax', 'SPEC-SCAN-PY', raw_py=RAW_PY_BROKEN,
                 raw_java=RAW_JAVA_CLEAN)

    # ⑥ 顺带在真产物上跑一遍（**advisory**：只印结论，不参与自测裁决）。
    #    这样读者不会把「SELFTEST OK」误读成「产物也是好的」，也不会在产物变红时
    #    看到「SELFTEST FAIL」而误以为这门禁没证明自己有牙。真产物归不带 --selftest
    #    的那次运行判（统一入口里每个门禁本来就跑两次：selftest + real）。
    print('  -- 真产物现状（advisory，本行不决定自测结论）--')
    sample('real-files-now', (), root=ROOT, advisory=True)

    print('note: 真产物的 PASS/FAIL 由不带 --selftest 的那次运行负责判（见统一入口的 real 列）')
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
    root = os.path.abspath(root or ROOT)
    summary, findings = audit(root)
    return report(summary, findings)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
