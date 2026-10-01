#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""平台层前端（页面）与 Java 声明 / 门禁注册表的一致性。

为什么需要它
============
`platform/` 这一层此前有两条 tier-A 门禁：`platform-spec-parity`（两侧的**声明**
一致：指标规格表）与 `platform-text-parity`（两侧的**显示规则**逐字节一致）。
但**页面本身**（谁读哪些字段、这些字段 Java 侧有没有、页脚自报的门禁清单对不对）
一直登记成「零覆盖项」——理由是「打开页面要一个活着的 JVM 与端口」。
那条理由只对**渲染/视觉**成立，对下面这几件事**不成立**，因为它们都是纯文本可判的：

* 页面读的每一个字段（`view.dataVersion` 这种**点链**）是不是 Java 记录里真有的组件
  —— 前端写错一个字段名 ⇒ 屏幕上是一个静悄悄的空，四种门禁**没有一条会红**
  （JSON 反序列化不校验字段名，TypeScript 也没上；`resolve()` 只认 id）；
* 页脚那条「本层有自建门禁了（…几条…都在 CI 里跑）」的**自报清单**是不是真的
  —— 加了第四条门禁而页脚没跟上，或者页脚点了一个不存在的名字，此前无人核对；
* 「绩效数字只在服务端格式化」这条自称是不是还成立（前端一旦出现 `toFixed` 之类的
  数字格式化调用，两侧就各算一套，而那正是 `platform-text-parity` 要防的漂移的源头）。

判据是**形状**而不是正确性（与 `line-anchors` 同一哲学）：正确性要语义判据，
机械做不到；形状能机械断言。

它比什么（口径）
================
1. `PW-UNKNOWN-FIELD` / `PW-NOT-A-FIELD`：把 `platform/web/src` 里所有
   「登记过的别名 + 点链」（`view.a.b`）逐段对着 **Java 记录组件与类型** 走一遍
   —— 组件不存在 ⇒ 报；落到一个非记录类型上再往下点（不是 JS 内建）⇒ 报。
2. `PW-STALE-ROOT` / `PW-WEAK-ANCHOR`：登记表里每条别名都必须带 `anchor`
   （在 JS 里**恰好出现一次**的代码字面量）。改别名会让 anchor 失配 ⇒ 变红
   —— 这是「只查单向」的补丁：不然登记表会变成永久空壳。
3. `PW-FORMAT-LEAK` / `PW-FORMAT-SCOPE-READ`：数字格式化调用只允许出现在
   登记的函数里，且那个函数里**不许**读非数字字段（几何输入必须是 double/Integer）。
4. `PW-SPEC-COPY`：指标键 / 分组名不许在 JS 里以字符串字面量出现
   （参照物 = `quanauto/dashboard.py` 的规格表，**不 import 它、走
   `tools/verify_platform_specs.py` 的 AST 扫描**，避免第三份副本）。
5. `PW-TEXT-ECHO`：凡是 Java 记录里带 `text` 组件的别名，页面上必须**真的**读它的
   `.text`（不然「服务端算好、前端原样呈现」就成了自称）。
6. `PW-GATE-LIST` / `PW-GATE-COUNT` / `PW-CI-CLAIM`：页脚点名的 `platform-*` 门禁集合
   必须**等于**注册表里的集合；页脚那句「N 条」必须**数得出来**（= 集合大小）；
   页脚说「都在 CI 里跑」时，CI 工作流里必须真的有统一入口。

它**不**比什么（边界，别把这些读进来）
=====================================
* 它**不打开页面**：不起服务、不渲染、不看布局与视觉 —— 零覆盖项「页面本身
  （渲染 / 布局 / 视觉）」**仍然零覆盖**，这条门禁**不解除**它，只把其中
  「纯文本可判」的部分钉住。判据依赖活着的 JVM/端口就是判据的缺陷
  （`platform/check_text_parity.py` 因此不是门禁）。
* 它**不启动 JVM、不编译 Java**：Java 侧只读文本（记录名/组件名/组件类型），
  编译与测试归 `platform-runtime`。
* 它**看不见未登记的别名**：解构（`const { text } = metric`）、计算属性名
  （`obj[key]`）、穿过函数参数传出去的字段都不在扫描面里。所以「前端字段全被钉住了」
  这句话是**假的**；准确说法是「登记表里的那几条根，读的字段都被钉住了」。
* 它**不管字段的值对不对**：`view.window` 是不是 `[start, end]`、`counts` 的
  `null` 显示成什么，那是 `ReportView` 的 Javadoc 与 `platform-text-parity` 的事。

自证
====
`--selftest` 会在 %TEMP% 的沙箱里（只拷 `platform/web/src`、Java 主源码、
本门禁的登记表、统一入口、CI 工作流）逐个构造坏样本：**每条检查码都有一个
打到靶子的样本**、一个**干净样本**（防误报），并断言报出来的**正是**预期的检查码
集合（多报一个码往往就是「变异打偏了」的签名）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

# 不写字节码：① 不给统一入口那条「门禁改了仓库里的文件」守卫添乱（`tools/__pycache__`）；
# ② 避开「陈旧字节码」那一族坑（改了源码却读到旧 pyc，往返验证因此失真）。
sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JS_DIR_REL = os.path.join('platform', 'web', 'src')
JS_SUFFIXES = ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx')
JAVA_DIR_REL = os.path.join('platform', 'api', 'src', 'main', 'java')
BINDINGS_REL = os.path.join('tools', 'platform-web-bindings.json')
GATES_REL = os.path.join('tools', 'run_all_gates.py')
CI_REL = os.path.join('.github', 'workflows', 'ci.yml')
PAGE_REL = os.path.join('platform', 'web', 'src', 'App.jsx')
PY_SPEC_REL = os.path.join('quanauto', 'dashboard.py')

SCHEMA = 'quanauto.platform-web-bindings/1'
MIN_WHY = 16

# 别名后面可以继续点的东西：JS 内建。写死成一张表而不是「任意标识符都放过」，
# 因为这条判据要防的正是「以为服务端发了一个字段，其实没那么回事」。
JS_BUILTINS = frozenset((
    'length', 'map', 'filter', 'forEach', 'reduce', 'find', 'findIndex', 'some', 'every',
    'join', 'slice', 'concat', 'flat', 'indexOf', 'includes', 'at', 'entries', 'keys',
    'values', 'push', 'pop', 'shift', 'unshift', 'sort', 'reverse', 'fill', 'flatMap',
    'toString', 'valueOf', 'charAt', 'charCodeAt', 'substring', 'substr', 'split',
    'trim', 'trimStart', 'trimEnd', 'toUpperCase', 'toLowerCase', 'startsWith',
    'endsWith', 'padStart', 'padEnd', 'repeat', 'replace', 'replaceAll', 'match',
    'search', 'test', 'has', 'add', 'delete', 'get', 'set', 'size',
))

NUMERIC_TYPES = frozenset((
    'byte', 'short', 'int', 'long', 'float', 'double',
    'Byte', 'Short', 'Integer', 'Long', 'Float', 'Double',
    'BigDecimal', 'BigInteger', 'Number',
))

# 数字格式化调用。`Number(` 只匹配调用形式；`Intl.` 只匹配命名空间形式。
FORMAT_CALL_RE = re.compile(
    r'\.\s*toFixed\s*\(|\.\s*toPrecision\s*\(|\.\s*toLocaleString\s*\(|'
    r'\bIntl\s*\.|\bNumber\s*\(|\bparseFloat\s*\(|\bparseInt\s*\(')

# Java 侧：只读文本，不编译。声明体用花括号配对量出来，record 的形参用圆括号配对量。
DECL_RE = re.compile(r'\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)\s*(\(|<|extends|implements|\{)')
RECORD_RE = re.compile(r'\brecord\s+([A-Za-z_$][\w$]*)\s*\(')
ANNOTATION_RE = re.compile(r'@\w+(?:\([^)]*\))?\s*')
FIELD_NAME_RE = re.compile(r'([A-Za-z_$][\w$]*)\s*$')

GATE_NAME_RE = re.compile(r"^\s*'name':\s*'([^']+)'", re.M)
CODE_TAG_RE = re.compile(r'<code>([^<]+)</code>')
PAGE_GATE_RE = re.compile(r'^platform-[a-z0-9-]+$')
COUNT_PHRASE_RE = re.compile(r'([0-9]+|[一二三四五六七八九十两]+)\s*条')
CN_DIGITS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
             '八': 8, '九': 9, '十': 10}


class HarnessError(Exception):
    """自测夹具自己的问题（变异没打中）—— 不许静默变成「干净样本」。"""


def harden_stdout():
    for stream in ('stdout', 'stderr'):
        handle = getattr(sys, stream, None)
        if handle is not None and hasattr(handle, 'reconfigure'):
            try:
                handle.reconfigure(errors='replace')
            except (ValueError, OSError):
                pass


def read_text(path):
    """**必须**先去掉 BOM 再规范化 CRLF。

    CRLF 会让裸 `\\n` 的正则静默失配（提取到 0 却打印「0 issue(s) PASS」），
    BOM 会让首个字面量（比如 `import`）永远匹配不上 —— 两个都踩过。
    """
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        return handle.read().replace('\r\n', '\n')


def write_text(path, text):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, '/')


# ── 扫描面 ──────────────────────────────────────────────────────────
def js_files(root):
    base = os.path.join(root, JS_DIR_REL)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != 'node_modules')
        for name in sorted(filenames):
            if name.endswith(JS_SUFFIXES):
                path = os.path.join(dirpath, name)
                out.append((_rel(root, path), path))
    return sorted(out)


def java_files(root):
    base = os.path.join(root, JAVA_DIR_REL)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            if name.endswith('.java'):
                path = os.path.join(dirpath, name)
                out.append((_rel(root, path), path))
    return sorted(out)


# ── Java 侧：记录名 -> 组件（名字 + 类型） ───────────────────────────
def _match_pair(text, open_index, opener, closer):
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_params(text):
    """按顶层逗号切形参（`<` `(` `[` 都算括号深度）。"""
    parts = []
    depth = 0
    current = []
    for char in text:
        if char in '(<[':
            depth += 1
        elif char in ')>]':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
    if ''.join(current).strip():
        parts.append(''.join(current))
    return [part.strip() for part in parts if part.strip()]


def _decl_spans(text):
    """所有类型声明的 (kind, name, start, end)：end = 声明体结束（含嵌套）。"""
    spans = []
    for match in DECL_RE.finditer(text):
        kind, name = match.group(1), match.group(2)
        cursor = match.end() - 1
        if text[cursor] == '{':
            brace = cursor
        else:
            brace = text.find('{', cursor)
            if brace == -1:
                continue
            # 形参里可能出现 `{`（注解/数组初始化），取的是配平后的那一个。
            if text[cursor] == '(':
                close = _match_pair(text, cursor, '(', ')')
                if close == -1:
                    continue
                brace = text.find('{', close)
                if brace == -1:
                    continue
        close = _match_pair(text, brace, '{', '}')
        if close == -1:
            continue
        spans.append((kind, name, match.start(), close))
    return spans


def parse_java_record_texts(java_texts):
    """java_texts = [(rel, text)] -> (records, findings)。

    records: {'MetricRead': {'fields': [(name, type)], 'types': {name: type},
              'file': rel, 'outer': None|str}}
    键同时登记简单名与 `Outer.Simple`（跨文件引用两种写法都能对上）。
    """
    records = {}
    findings = []
    for rel, text in java_texts:
        spans = _decl_spans(text)
        for match in RECORD_RE.finditer(text):
            name = match.group(1)
            open_index = match.end() - 1
            close = _match_pair(text, open_index, '(', ')')
            if close == -1:
                findings.append(('PW-SCAN-JAVA',
                                 '%s 里 record %s 的形参括号没配平 —— 这一条不参与核对'
                                 % (rel, name)))
                continue
            fields = []
            for part in _split_params(text[open_index + 1:close]):
                stripped = ANNOTATION_RE.sub('', part).strip()
                hit = FIELD_NAME_RE.search(stripped)
                if hit is None:
                    findings.append(('PW-SCAN-JAVA',
                                     '%s：record %s 的形参 %r 解析不出名字' % (rel, name, part)))
                    continue
                field = hit.group(1)
                field_type = stripped[:hit.start()].strip()
                if not field_type:
                    findings.append(('PW-SCAN-JAVA',
                                     '%s：record %s 的组件 %s 解析不出类型' % (rel, name, field)))
                    continue
                fields.append((field, field_type))
            outer = None
            for kind, other, start, end in spans:
                if other == name or start >= match.start() or end < close:
                    continue
                if outer is None or start > outer[0]:
                    outer = (start, other)
            entry = {
                'fields': fields,
                'types': dict(fields),
                'file': rel,
                'outer': outer[1] if outer else None,
            }
            keys = [name]
            if entry['outer']:
                keys.append('%s.%s' % (entry['outer'], name))
            for key in keys:
                records.setdefault(key, entry)
    return records, findings


def all_fields(records, names):
    seen = []
    for name in names:
        record = records.get(name)
        if record is None:
            continue
        for field, _type in record['fields']:
            if field not in seen:
                seen.append(field)
    return seen


# ── 点链核对 ────────────────────────────────────────────────────────
def walk_chain(records, start_types, segments, label, container=False):
    """沿 `alias.a.b` 逐段核对。

    返回 `(code|None, message|None, kinds)`：
    * `code` 非空 = 这条链有问题；
    * `kinds` = 每段落到的东西（`record` / `numeric` / `opaque` / `builtin`），
      给「几何函数里不许读非数字字段」那条判据用。
    """
    if container:
        stage = {'kind': 'value', 'raw': ['(List 元素)']}
    else:
        stage = {'kind': 'record', 'records': list(start_types), 'raw': list(start_types)}
    kinds = []
    for index, segment in enumerate(segments):
        head = '%s.%s' % (label, '.'.join(segments[:index + 1]))
        if stage['kind'] != 'record':
            if segment not in JS_BUILTINS:
                return ('PW-NOT-A-FIELD',
                        '%s：前一段的类型是 %s（不是 Java 记录），再往下只允许 JS 内建读 %s，'
                        '而这里读的是 %r'
                        % (head, '、'.join(stage['raw']), '/'.join(sorted(JS_BUILTINS)), segment),
                        kinds)
            kinds.append('builtin')
            return (None, None, kinds)
        raw_types = []
        for record_name in stage['records']:
            record = records.get(record_name)
            if record is None:
                continue
            raw = record['types'].get(segment)
            if raw is not None and raw not in raw_types:
                raw_types.append(raw)
        if not raw_types:
            return ('PW-UNKNOWN-FIELD',
                    '%s：Java 记录 %s 没有组件 %r（组件清单：%s）'
                    % (head, '、'.join(stage['records']), segment,
                       '、'.join(sorted(all_fields(records, stage['records']))) or '(空)'),
                    kinds)
        if all(raw in records for raw in raw_types):
            kinds.append('record')
            stage = {'kind': 'record',
                     'records': [raw for raw in raw_types if raw in records],
                     'raw': raw_types}
        else:
            kinds.append('numeric' if all(raw in NUMERIC_TYPES for raw in raw_types) else 'opaque')
            stage = {'kind': 'value', 'raw': raw_types}
    return (None, None, kinds)


def value_chains(text, alias):
    """text 里所有以 alias 起头的点链（去掉 alias 本身，只留各段）。"""
    pattern = re.compile(r'\b%s((?:\.[A-Za-z_$][\w$]*)+)' % re.escape(alias))
    out = []
    for match in pattern.finditer(text):
        segments = [part for part in match.group(1).split('.') if part]
        if segments:
            out.append(segments)
    return out


# ── JS 侧：函数体（给「格式化调用只在哪个函数里」用） ────────────────
FUNCTION_RE = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(')
ARROW_FUNCTION_RE = re.compile(r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\((?:[^()]*)\)\s*=>\s*\{')


def function_spans(text):
    """[(name, body_start, body_end)] —— 名字 + 函数体（含嵌套函数体）。"""
    spans = []
    for pattern in (FUNCTION_RE, ARROW_FUNCTION_RE):
        for match in pattern.finditer(text):
            cursor = match.end() - 1
            if text[cursor] == '(':
                close = _match_pair(text, cursor, '(', ')')
                if close == -1:
                    continue
                cursor = close
            brace = text.find('{', cursor)
            if brace == -1:
                continue
            end = _match_pair(text, brace, '{', '}')
            if end == -1:
                continue
            spans.append((match.group(1), brace, end))
    return spans


def innermost_function(spans, index):
    best = None
    for name, start, end in spans:
        if start <= index <= end:
            if best is None or (start, end) > (best[1], best[2]):
                best = (name, start, end)
    return best[0] if best else None


# ── 登记表 ──────────────────────────────────────────────────────────
def load_bindings(root):
    path = os.path.join(root, BINDINGS_REL)
    if not os.path.isfile(path):
        return {}, [('PW-NO-BINDINGS', '读不到 %s —— 没有登记表就没法判断任何一条别名' % BINDINGS_REL)]
    try:
        with open(path, 'r', encoding='utf-8-sig') as handle:
            doc = json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [('PW-NO-BINDINGS', '%s 读不出来/不是合法 JSON：%s' % (BINDINGS_REL, exc))]
    if not isinstance(doc, dict):
        return {}, [('PW-NO-BINDINGS', '%s 顶层不是对象' % BINDINGS_REL)]
    if doc.get('schema') != SCHEMA:
        return doc, [('PW-NO-BINDINGS', '%s 的 schema 是 %r，期望 %r'
                      % (BINDINGS_REL, doc.get('schema'), SCHEMA))]
    for key in ('jsRoots', 'formatScope', 'specCopy'):
        if key not in doc:
            return doc, [('PW-NO-BINDINGS', '%s 缺少 %r 这一节' % (BINDINGS_REL, key))]
    return doc, []


# ── 规格表参照物（不 import quanauto.dashboard：走已有门禁的 AST 扫描） ──
_SPEC_CACHE = {'tokens': None, 'findings': None}


def spec_tokens():
    """(forbidden_tokens, findings)：指标键 + 分组名 —— 参照物是 Python 侧的规格表。"""
    if _SPEC_CACHE['tokens'] is not None:
        return _SPEC_CACHE['tokens'], _SPEC_CACHE['findings']
    tokens = []
    findings = []
    tools_dir = os.path.join(ROOT, 'tools')
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import verify_platform_specs as specs
        _consts, rows, _counts, spec_findings = specs.scan_python(
            os.path.join(ROOT, PY_SPEC_REL))
    except Exception as exc:  # noqa: BLE001 —— 参照物取不到必须响，不许静默 0 命中
        findings.append(('PW-SPEC-SOURCE',
                         '取不到 Python 侧规格表（%s）：%s —— 手抄检查会退化成 0 命中，'
                         '拒绝当「通过」' % (PY_SPEC_REL, exc)))
        _SPEC_CACHE.update({'tokens': [], 'findings': findings})
        return tokens, findings
    for finding in spec_findings:
        findings.append(('PW-SPEC-SOURCE', '参照物自身有问题：%s' % (finding[1],)))
    for row in rows:
        if not row.get('resolved'):
            continue
        if row.get('key'):
            tokens.append(row['key'])
        if row.get('group'):
            tokens.append(row['group'])
    tokens = sorted(set(tokens))
    if not tokens:
        findings.append(('PW-SPEC-SOURCE',
                         '规格表里一个键/分组名都没提取到 —— 手抄检查形同虚设，拒绝通过'))
    _SPEC_CACHE.update({'tokens': tokens, 'findings': findings})
    return tokens, findings


# ── 主判据 ──────────────────────────────────────────────────────────
def audit(root):
    findings = []
    summary = {'root': root}

    pairs = js_files(root)
    texts = {}
    for rel, path in pairs:
        try:
            texts[rel] = read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(('PW-SCAN', '%s 读不出来：%s' % (rel, exc)))
    summary['js'] = len(texts)
    if not texts:
        findings.append(('PW-NO-JS', '%s 下没扫到任何 JS/JSX —— 后面每一条检查都会空转，'
                                     '拒绝通过' % JS_DIR_REL))
    blob = '\n'.join(texts[rel] for rel in sorted(texts))

    java_texts = []
    for rel, path in java_files(root):
        try:
            java_texts.append((rel, read_text(path)))
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(('PW-SCAN', '%s 读不出来：%s' % (rel, exc)))
    records, java_findings = parse_java_record_texts(java_texts)
    findings.extend(java_findings)
    summary['java'] = len(java_texts)
    summary['records'] = len([name for name in records if '.' not in name])
    if not records:
        findings.append(('PW-NO-RECORDS', 'Java 侧一个 record 都没解析出来（扫了 %d 个 .java）'
                                          ' —— 字段核对会全部空转，拒绝通过' % len(java_texts)))

    doc, bind_findings = load_bindings(root)
    findings.extend(bind_findings)
    roots = [entry for entry in (doc.get('jsRoots') or []) if isinstance(entry, dict)]
    summary['roots'] = len(roots)
    if not roots:
        findings.append(('PW-NO-BINDINGS', '登记表里 0 条别名 —— 「前端读了什么」这件事'
                                           '没有任何一条被钉住，拒绝通过'))

    # ① 别名：anchor（恰好一次）+ why
    anchors_ok = 0
    for entry in roots:
        alias = entry.get('alias') or '(缺 alias)'
        why = (entry.get('why') or '').strip()
        if len(why) < MIN_WHY:
            findings.append(('PW-NO-WHY', '登记项 %r 的 why 太短（%d 字）—— 登记而不写清'
                                          '「为什么这个别名/字段没有记录可对」，下一个人只能猜'
                             % (alias, len(why))))
        anchors = entry.get('anchor') or []
        if not anchors:
            findings.append(('PW-STALE-ROOT', '登记项 %r 没有 anchor —— 没有证据能证明它还在'
                                              '，登记表会变成空壳' % alias))
            continue
        for anchor in anchors:
            hits = blob.count(anchor)
            if hits == 0:
                findings.append(('PW-STALE-ROOT',
                                 '登记项 %r 的 anchor %r 在 %s 里已经找不到 —— 别名被改名/删掉'
                                 '了？删掉这条登记或把它改回按内容定位的锚点'
                                 % (alias, anchor, JS_DIR_REL)))
            elif hits > 1:
                findings.append(('PW-WEAK-ANCHOR',
                                 '登记项 %r 的 anchor %r 在 JS 里出现 %d 次 —— 说不清你指的是'
                                 '哪一处，换一个唯一的锚点' % (alias, anchor, hits)))
            else:
                anchors_ok += 1
    summary['anchors'] = anchors_ok

    # ② 字段：逐条点链去 Java 记录里核对
    chain_total = 0
    field_total = 0
    text_echo = 0
    seen_chains = set()
    for entry in roots:
        alias = entry.get('alias')
        if not alias:
            continue
        java_types = [name for name in (entry.get('java') or []) if isinstance(name, str)]
        evidence = {}
        for item in entry.get('evidence') or []:
            if isinstance(item, dict) and item.get('field'):
                evidence[str(item['field'])] = item
        for segments in value_chains(blob, alias):
            key = (alias, tuple(segments))
            if key in seen_chains:
                continue
            seen_chains.add(key)
            chain_total += 1
            head = '%s.%s' % (alias, '.'.join(segments))
            if segments[0] in evidence:
                item = evidence[segments[0]]
                rel = str(item.get('file') or '')
                literal = str(item.get('literal') or '')
                if not rel or not literal:
                    findings.append(('PW-EVIDENCE-MISSING',
                                     '登记项 %r 给字段 %s 记了证据却缺 file/literal'
                                     % (alias, head)))
                else:
                    path = os.path.join(root, rel.replace('/', os.sep))
                    if not os.path.isfile(path):
                        findings.append(('PW-EVIDENCE-MISSING',
                                         '登记项 %r 的证据文件 %s 不存在' % (alias, rel)))
                    else:
                        try:
                            if read_text(path).count(literal) == 0:
                                findings.append(('PW-EVIDENCE-MISSING',
                                                 '登记项 %r 的证据字面量 %r 在 %s 里找不到 —— '
                                                 '这条「它只能记证据」的说法已经过期'
                                                 % (alias, literal, rel)))
                        except (OSError, UnicodeDecodeError):
                            pass
                if len(segments) == 1:
                    field_total += 1
                continue
            code, message, _kinds = walk_chain(records, java_types, segments, alias,
                                               container=bool(entry.get('container')))
            if code:
                findings.append((code, message))
            elif segments == ['text']:
                text_echo += 1
            field_total += 1
    summary['chains'] = chain_total
    summary['fields'] = field_total
    summary['textEcho'] = text_echo

    # ③ text 回声：Java 记录里带 text 组件的别名，页面必须真的读它
    echo_expected = []
    for entry in roots:
        alias = entry.get('alias')
        if not alias:
            continue
        for name in entry.get('java') or []:
            record = records.get(name)
            if record is not None and 'text' in record['types']:
                echo_expected.append(alias)
                break
    summary['textEchoExpected'] = len(echo_expected)
    for alias in echo_expected:
        if not any(segments == ['text'] for segments in value_chains(blob, alias)):
            findings.append(('PW-TEXT-ECHO',
                             '别名 %r 对应的 Java 记录带 text 组件（= 服务端算好的显示串），'
                             '但页面里没有任何一处读它的 .text —— 「服务端算好、前端原样'
                             '呈现」就成了自称' % alias))

    # ④ 格式化调用：只在登记的函数里，且那个函数不许读非数字字段
    scope = doc.get('formatScope') or {}
    allowed = set(scope.get('functions') or [])
    if len((scope.get('why') or '').strip()) < MIN_WHY:
        findings.append(('PW-NO-WHY', 'formatScope 的 why 太短 —— 为什么这个函数可以格式化'
                                      '数字，得写出来'))
    spans = function_spans(blob)
    format_hits = 0
    for match in FORMAT_CALL_RE.finditer(blob):
        name = innermost_function(spans, match.start())
        format_hits += 1
        if name not in allowed:
            findings.append(('PW-FORMAT-LEAK',
                             '数字格式化调用 %r 出现在函数 %s 里 —— 绩效数字的格式化在服务端'
                             '（Java 算好 text 下发），前端只负责放进 DOM；允许的只有 %s'
                             % (match.group(0).strip(), name or '(模块顶层/组件体)',
                                '/'.join(sorted(allowed)) or '(未登记)')))
    summary['formatCalls'] = format_hits
    if not allowed:
        findings.append(('PW-NO-WHY', 'formatScope.functions 是空的 —— 「谁可以格式化」没有'
                                      '答案，这条判据只会空转'))
    for entry in roots:
        alias = entry.get('alias')
        if not alias:
            continue
        evidence_fields = {str(item.get('field')) for item in (entry.get('evidence') or [])
                           if isinstance(item, dict) and item.get('field')}
        for match in re.finditer(r'\b%s((?:\.[A-Za-z_$][\w$]*)+)' % re.escape(alias), blob):
            name = innermost_function(spans, match.start())
            if name not in allowed:
                continue
            parsed = [part for part in match.group(1).split('.') if part]
            if not parsed or parsed[0] in evidence_fields:
                continue
            code, _message, kinds = walk_chain(records, entry.get('java') or [], parsed, alias,
                                               container=bool(entry.get('container')))
            if code or 'opaque' not in kinds:
                continue
            findings.append(('PW-FORMAT-SCOPE-READ',
                             '%s.%s 出现在函数 %s 里，而它的类型是 %s（不是数字）—— 几何函数'
                             '只准拿数字算坐标，别把字符串/布尔读进来'
                             % (alias, '.'.join(parsed), name,
                                '、'.join(record_type_hint(records, entry, parsed)))))

    # ⑤ 手抄规格表
    tokens, spec_findings = spec_tokens()
    findings.extend(spec_findings)
    exempt = set((doc.get('specCopy') or {}).get('exempt') or [])
    for token in tokens:
        if token in exempt:
            continue
        for quote in ("'", '"'):
            literal = '%s%s%s' % (quote, token, quote)
            if literal in blob:
                findings.append(('PW-SPEC-COPY',
                                 'JS 里出现了规格表字面量 %s —— 指标键与分组名一律由服务端'
                                 '随报告下发（metric.key / metric.group），前端抄一份就多一个'
                                 '会漂的地方；确实需要抄请在 %s 的 specCopy.exempt 里登记并写 why'
                                 % (literal, BINDINGS_REL)))
                break
    summary['specTokens'] = len(tokens)

    # ⑥ 页脚自报的门禁清单：集合相等 + 计数短语 + CI 声明
    page_path = os.path.join(root, PAGE_REL)
    page = ''
    try:
        page = read_text(page_path)
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(('PW-GATE-LIST', '%s 读不出来：%s' % (PAGE_REL, exc)))
    footer = ''
    if '<footer>' in page and '</footer>' in page:
        footer = page.split('<footer>', 1)[1].split('</footer>', 1)[0]
    else:
        findings.append(('PW-GATE-LIST', '%s 里找不到 <footer>…</footer> 块 —— 页脚自报的门禁'
                                          '清单没法核对' % PAGE_REL))
    if len(footer.strip()) < 120:
        findings.append(('PW-GATE-LIST', '%s 的页脚只有 %d 字 —— 提取为空/被截断时后面几条'
                                          '检查会空转' % (PAGE_REL, len(footer.strip()))))

    gates_path = os.path.join(root, GATES_REL)
    registered = []
    try:
        registered = sorted({name for name in GATE_NAME_RE.findall(read_text(gates_path))
                             if name.startswith('platform-')})
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(('PW-GATE-LIST', '读不到 %s：%s' % (GATES_REL, exc)))
    summary['registeredGates'] = len(registered)
    if not registered:
        findings.append(('PW-GATE-LIST', '%s 里一个 platform-* 门禁都没提取到 —— 下面两条'
                                          '检查会空转，拒绝通过' % GATES_REL))

    named = sorted({tag for tag in CODE_TAG_RE.findall(footer) if PAGE_GATE_RE.match(tag)})
    summary['pageGates'] = len(named)
    if not named and registered:
        findings.append(('PW-GATE-LIST', '%s 的页脚一个 platform-* 门禁都没点名'
                                          % PAGE_REL))
    for name in registered:
        if name not in named:
            findings.append(('PW-GATE-LIST',
                             '注册表里的门禁 %s 没有出现在页脚的自报清单里（页脚现在写着：%s）'
                             ' —— 加门禁时页脚要同批改' % (name, '、'.join(named) or '(空)')))
    for name in named:
        if name not in registered:
            findings.append(('PW-GATE-LIST',
                             '页脚点名了 %s，而统一入口的注册表里没有这个门禁'
                             % name))

    phrases = [match.group(1) for match in COUNT_PHRASE_RE.finditer(footer)]
    summary['countPhrases'] = len(phrases)
    if not phrases:
        findings.append(('PW-GATE-COUNT',
                         '页脚里没有「N 条」这样的计数短语 —— 门禁条数要么数出来，要么别写'
                         '（写死一个数字，加门禁时就会静默过期）'))
    for raw in phrases:
        value = CN_DIGITS.get(raw) if raw in CN_DIGITS else (int(raw) if raw.isdigit() else None)
        if value is None:
            findings.append(('PW-GATE-COUNT', '页脚的计数短语 %r 认不出来' % raw))
        elif value != len(registered):
            findings.append(('PW-GATE-COUNT',
                             '页脚说「%s 条」，而注册表里 platform-* 门禁有 %d 个（%s）'
                             ' —— 计数必须数得出来' % (raw, len(registered),
                                                       '、'.join(registered))))

    ci_path = os.path.join(root, CI_REL)
    if 'CI' in footer:
        try:
            ci_text = read_text(ci_path)
        except (OSError, UnicodeDecodeError) as exc:
            ci_text = ''
            findings.append(('PW-CI-CLAIM', '页脚说「都在 CI 里跑」，而 %s 读不出来：%s'
                                            % (CI_REL, exc)))
        if ci_text and 'run_all_gates.py' not in ci_text:
            findings.append(('PW-CI-CLAIM',
                             '页脚说「都在 CI 里跑」，而 %s 里没有统一入口 run_all_gates.py'
                             % CI_REL))

    findings = sorted(set(findings))
    return summary, findings


def record_type_hint(records, entry, segments):
    """给报错信息用的类型提示（尽量说清是哪一层的类型不对）。"""
    types = list(entry.get('java') or [])
    for segment in segments[:-1]:
        nxt = []
        for name in types:
            record = records.get(name)
            if record is None:
                continue
            raw = record['types'].get(segment)
            if raw:
                nxt.append(raw)
        types = nxt or types
    out = []
    for name in types:
        record = records.get(name)
        if record is None:
            continue
        raw = record['types'].get(segments[-1])
        out.append('%s' % raw if raw else name)
    return out or types or ['(未登记)']


def report(summary, findings):
    print('platform web parity — 平台层前端（页面）与 Java 声明 / 门禁注册表的一致性')
    print('what:     页面读的字段必须是 Java 记录真有的（PW-UNKNOWN-FIELD / PW-NOT-A-FIELD）；'
          '别名改名要报（PW-STALE-ROOT）；绩效数字只许服务端格式化（PW-FORMAT-LEAK /'
          ' PW-FORMAT-SCOPE-READ）；指标键与分组名不许手抄（PW-SPEC-COPY）；'
          '带 text 的别名必须真读 .text（PW-TEXT-ECHO）；页脚自报的门禁清单要等于注册表'
          '（PW-GATE-LIST / PW-GATE-COUNT / PW-CI-CLAIM）')
    print('boundary: **不打开页面**（不起服务、不渲染、不看视觉：渲染/布局/视觉仍零覆盖）；'
          '不启动 JVM、不编译 Java；看不见未登记的别名（解构 / 计算属性名 / 穿了函数参数'
          '的字段）—— 所以别说「前端字段全被钉住了」')
    print('root:      %s' % summary['root'])
    print('scanned:   js=%d java=%d' % (summary['js'], summary['java']))
    print('records:   %d 个 Java 记录（组件名/类型只读文本，不编译）' % summary['records'])
    print('roots:     %d 条登记别名；anchor 唯一命中 %d；点链 %d 条（去重）；'
          'text 回声 %d/%d'
          % (summary['roots'], summary['anchors'], summary['chains'],
             summary['textEcho'], summary['textEchoExpected']))
    print('format:    %d 处数字格式化调用（只允许出现在登记的函数里）'
          % summary['formatCalls'])
    print('spec:      %d 个不许手抄的字面量（指标键 + 分组名，参照 quanauto/dashboard.py）'
          % summary['specTokens'])
    print('gates:     注册表 platform-*=%d；页脚点名=%d；页脚计数短语=%d'
          % (summary['registeredGates'], summary['pageGates'], summary['countPhrases']))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    print('verdict: %s (%d issue(s))' % ('DIRTY' if findings else 'PASS', len(findings)))
    return 1 if findings else 0


# ── 自测 ────────────────────────────────────────────────────────────
SANDBOX = {'root': None}
SANDBOX_PATHS = (
    JS_DIR_REL,
    JAVA_DIR_REL,
    BINDINGS_REL,
    GATES_REL,
    CI_REL,
)


def make_sandbox():
    """把本门禁真正读的那些文件拷进 %TEMP% —— 一个字节都不写进仓库。"""
    if SANDBOX['root'] and os.path.isdir(SANDBOX['root']):
        shutil.rmtree(SANDBOX['root'], ignore_errors=True)
    base = tempfile.mkdtemp(prefix='platform-web-parity-selftest-')
    for rel in SANDBOX_PATHS:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns('node_modules', 'target'))
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
        else:
            raise HarnessError('沙箱要拷的 %s 不存在 —— 自测夹具自己坏了' % rel)
    SANDBOX['root'] = base
    return base


def mutate(rel, old, new, count=1):
    """把沙箱里的文件改一处；命中次数不是 count 就**报工具错**（不许静默 no-op）。

    命中次数断言是必须的：同一批里新加的产物会把原本有效的变异悄悄变成 no-op，
    而 no-op 的变异与「探测器不存在」在报告里长得一模一样。
    """
    root = SANDBOX['root']
    path = os.path.join(root, rel)
    text = read_text(path)
    hits = text.count(old)
    if hits != count:
        raise HarnessError('变异没打中：%s 里 %r 出现 %d 次（期望 %d）'
                           % (rel, old, hits, count))
    write_text(path, text.replace(old, new))


def edit_registry(fn):
    root = SANDBOX['root']
    path = os.path.join(root, BINDINGS_REL)
    with open(path, 'r', encoding='utf-8-sig') as handle:
        doc = json.load(handle)
    before = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    fn(doc)
    after = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    if before == after:
        raise HarnessError('变异没打中：登记表 JSON 一个字节都没变')
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def sample(name, expect, mutate_fn=None, want=None, marker='', exact=True, advisory=False):
    root = make_sandbox()
    if mutate_fn is not None:
        mutate_fn()
    summary, findings = audit(root)
    codes = tuple(sorted({code for code, _message in findings}))
    ok = True
    notes = []
    if expect is None:
        if findings:
            ok = False
            notes.append('期望 0 条 FINDING')
    elif exact:
        if codes != tuple(sorted(expect)):
            ok = False
            notes.append('期望检查码恰好 %s' % (sorted(expect),))
    else:
        if not set(expect) <= set(codes):
            ok = False
            notes.append('期望至少包含 %s' % (sorted(expect),))
    for key, value in (want or {}).items():
        if summary.get(key) != value:
            ok = False
            notes.append('%s=%r（期望 %r）' % (key, summary.get(key), value))
    if marker and not any(marker in message for _code, message in findings):
        ok = False
        notes.append('没有任何 FINDING 的消息里含 %r' % marker)
    if advisory:
        print('  SAMPLE NOTE (advisory) %s: codes=%s findings=%d'
              % (name, ','.join(codes) or '-', len(findings)))
        return True
    print('%-34s %s  codes=%s' % (name, 'ok' if ok else 'FAIL', ','.join(codes) or '-'))
    for note in notes:
        print('    %s' % note)
    return ok


def selftest():
    harden_stdout()
    print('platform web parity — selftest（沙箱 = %TEMP%，逐条构造坏样本）')
    base = make_sandbox()
    summary, findings = audit(base)
    if findings:
        print('   干净样本就不干净：%s' % [code for code, _m in findings])
        return 1
    counted = {'js': summary['js'], 'java': summary['java'], 'records': summary['records'],
               'roots': summary['roots'], 'registeredGates': summary['registeredGates'],
               'pageGates': summary['pageGates'], 'chains': summary['chains'],
               'textEcho': summary['textEcho'],
               'textEchoExpected': summary['textEchoExpected']}

    ok = True
    ok &= sample('clean-control', (), want=counted)

    # ① 字段核对：组件不存在 / 在非记录类型上点字段
    ok &= sample('unknown-field', ('PW-UNKNOWN-FIELD',), marker='没有组件',
                 mutate_fn=lambda: mutate(PAGE_REL, 'view.dataVersion ||', 'view.dataVersions ||'))
    ok &= sample('not-a-field', ('PW-NOT-A-FIELD',), marker='JS 内建',
                 mutate_fn=lambda: mutate(
                     PAGE_REL, 'const points = curvePoints(view.curve);',
                     'const points = curvePoints(view.curve, view.curve.nopeLength);'))

    # ② 别名改名 ⇒ 登记表变空壳（只查单向的补丁）
    ok &= sample('stale-root', ('PW-STALE-ROOT',), marker='anchor',
                 mutate_fn=lambda: mutate(PAGE_REL, 'for (const metric of view.metrics) {',
                                          'for (const metricRow of view.metrics) {'))
    ok &= sample('weak-anchor', ('PW-WEAK-ANCHOR',), marker='说不清',
                 mutate_fn=lambda: mutate(
                     PAGE_REL, 'const [view, setView] = useState(null);',
                     'const [view, setView] = useState(null);\n'
                     '  // 这一行只是把同一句再写一遍：const [view, setView] = useState(null);\n'
                     '  // 出现两次的锚点说不清指的是哪一处。'))
    ok &= sample('no-why', ('PW-NO-WHY',), marker='why 太短',
                 mutate_fn=lambda: edit_registry(
                     lambda doc: doc['jsRoots'][0].__setitem__('why', '短')))

    # ③ 证据：把上游那句 Map.of("error", …) 改掉 ⇒ 证据过期
    ok &= sample('evidence-missing', ('PW-EVIDENCE-MISSING',), marker='证据字面量',
                 mutate_fn=lambda: mutate(
                     os.path.join('platform', 'api', 'src', 'main', 'java', 'com',
                                  'quanauto', 'dashboard', 'ApiExceptionHandler.java'),
                     'Map.of("error"', 'Map.of("message"', 4))

    # ④ 数字格式化：挪出登记的函数 / 在几何函数里读非数字
    ok &= sample('format-leak', ('PW-FORMAT-LEAK',), marker='格式化在服务端',
                 mutate_fn=lambda: mutate(
                     PAGE_REL, 'function Meta({ view }) {',
                     'function fmtPercent(value) {\n  return value.toFixed(2);\n}\n\n'
                     'function Meta({ view }) {'))
    ok &= sample('format-scope-read', ('PW-FORMAT-SCOPE-READ',), marker='不是数字',
                 mutate_fn=lambda: mutate(
                     PAGE_REL, 'const min = Math.min(...values);',
                     'const label = point.timestamp;\n  const min = Math.min(...values);'))

    # ⑤ 手抄规格表
    ok &= sample('spec-copy', ('PW-SPEC-COPY',), marker='规格表字面量',
                 mutate_fn=lambda: mutate(
                     PAGE_REL, 'const CURVE_PAD = 8;',
                     'const CURVE_PAD = 8;\nconst HIGHLIGHT = "sharpe_ratio";'))

    # ⑥ text 回声：把 metric.text 换成没格式化的裸值
    ok &= sample('text-echo', ('PW-TEXT-ECHO',), marker='原样呈现',
                 mutate_fn=lambda: mutate(PAGE_REL, '{metric.text}', '{metric.value}'))

    # ⑦ 页脚自报清单：集合相等 / 计数短语 / CI 声明
    ok &= sample('gate-list-missing', ('PW-GATE-LIST',), marker='没有出现在页脚',
                 mutate_fn=lambda: mutate(PAGE_REL, '<code>platform-web-parity</code>',
                                          'platform-web-parity', 2))
    ok &= sample('gate-list-unknown', ('PW-GATE-LIST',), marker='注册表里没有',
                 mutate_fn=lambda: mutate(PAGE_REL, '<code>platform-runtime</code>',
                                          '<code>platform-runtime</code> '
                                          '<code>platform-not-a-gate</code>'))
    ok &= sample('gate-count-wrong', ('PW-GATE-COUNT',), marker='必须数得出来',
                 mutate_fn=lambda: mutate(PAGE_REL, '四条都在 CI 里跑', '三条都在 CI 里跑'))
    ok &= sample('gate-count-absent', ('PW-GATE-COUNT',), marker='计数短语',
                 mutate_fn=lambda: mutate(PAGE_REL, '四条都在 CI 里跑', ''))
    ok &= sample('ci-claim', ('PW-CI-CLAIM',), marker='run_all_gates.py',
                 mutate_fn=lambda: mutate(CI_REL, 'run_all_gates.py', 'run_all_the_gates.py', 2))

    # ⑧ 空转守卫：扫描面为空 ⇒ 必须 FAIL，而不是「0 issue(s) PASS」
    ok &= sample('no-js', ('PW-NO-JS',), marker='空转', exact=False,
                 mutate_fn=lambda: shutil.rmtree(os.path.join(SANDBOX['root'], JS_DIR_REL)))
    ok &= sample('no-records', ('PW-NO-RECORDS',), marker='空转', exact=False,
                 mutate_fn=lambda: shutil.rmtree(os.path.join(SANDBOX['root'], JAVA_DIR_REL)))
    ok &= sample('no-bindings', ('PW-NO-BINDINGS',), marker='读不到', exact=False,
                 mutate_fn=lambda: os.remove(os.path.join(SANDBOX['root'], BINDINGS_REL)))
    ok &= sample('no-registered-gates', ('PW-GATE-COUNT', 'PW-GATE-LIST'), marker='空转',
                 mutate_fn=lambda: mutate(GATES_REL, "'name': 'platform-",
                                          "'name': 'x-platform-", 4))

    # ⑨ 真树现状（advisory，不决定自测结论）
    sample('real-tree-now', (), advisory=True)

    print('note: 真产物的 PASS/FAIL 由不带 --selftest 的那次运行负责判（见统一入口的 real 列）')
    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


def selftest_exit_code(selftest_rc, real_rc, selftest_mode):
    """带 --selftest 时退出码**只报自测**：产物红不红由不带 --selftest 的那次负责。

    不然「产物坏了」会被外层记成「这门禁没证明自己有牙」—— 假话恰好出现在最需要看
    自测结论的时刻。
    """
    if selftest_mode:
        return selftest_rc
    return real_rc


def main(argv):
    harden_stdout()
    selftest_mode = '--selftest' in argv
    root = None
    for arg in argv:
        if arg.startswith('--root='):
            root = arg.split('=', 1)[1]
        elif not arg.startswith('--'):
            root = arg
    root = os.path.abspath(root or ROOT)
    real_rc = report(*audit(root))
    if not selftest_mode:
        return real_rc
    return selftest_exit_code(selftest(), real_rc, selftest_mode)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
