#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据中心侧的表-写入者门禁：`db/data_center.sql` 里每张 dc 表都要有人写，没有的要登记。

守的是什么：

* **观测空白本身。** DDL 里现在有 10 张 dc 表，其中 6 张在 `quanauto/**` 里**一个写入者
  都没有**（`dc_quality_issue` / `dc_data_version` / `dc_trading_calendar` / `dc_symbol` /
  `dc_financial_report` / `dc_index_member`）。这种缺口不会让任何一条判据变红：DDL 与契约
  一致性门禁照样绿（表和约束都在）、SQL 冒烟照样绿（约束确实会拒绝）、pytest 照样绿
  （没有用例碰它）—— 关于这六张表的每一份报告都是**在空集上通过**的。风控侧已经有一条
  同形的门禁（`tools/verify_risk_table_wiring.py`），它守的是 2026-09-25 实测出来的同一个
  空白（`risk_decision_log` 有表、有约束、零写入者）。数据中心侧到这一刻为止只有一个**手写**
  的登记处 —— 契约 B20.6 那一格里的「仍是零写入者的 6 张」，而它自 2026-09-30 起已经人工
  订正过（9 张 → 10 张、3 张 → 4 张），没有任何判据盯着它。
* **把「提到」当成「写」。** `quanauto/ingest.py` 的模块 docstring 里写着「`dc_quality_issue`
  仍零写入者」（这是**真话**，本门禁也靠它当旁证），而 `quanauto/enums.py` 里写着「BFILL …
  必须写 `dc_quality_issue`」—— 一条 grep 会把后者当成写入者。本门禁用 `ast` 解析、**丢掉
  docstring**，只在确实带写关键字（`INSERT INTO` / `UPDATE` / `DELETE FROM`）的语句字面量上
  认表名，所以「在注释/docstring 里被提到」永远满足不了它。
* **能力被悄悄删掉。** 写入者还在、行还是写进去了，但让这一行有价值的那条性质没了
  （日线的 `ON CONFLICT … DO UPDATE` 幂等、`IS DISTINCT FROM` 的「同值不写」、`dc_dividend`
  的**公告日**也参与判等、`dc_ingest_run` 收批次的时间戳来自库而不是传参）。这些都是**一个
  token** 的改动，其余门禁全绿。

## 登记而不是覆盖

豁免是允许的，但只允许「写在契约里」：`db/data_center.sql` 的表在某一份**代码清单**
（`POLICY`）里，或者在某一张**契约登记表**（B20.7）里，两边都查 ——
新加一张没人写的表 ⇒ 既不在 POLICY、也不在登记里 ⇒ FAIL，强制一次人类决定；
豁免长成永久免检的挡箭牌（表已经有人写了、登记还留着）⇒ 也 FAIL。这是故意的。

## 它探不到什么（别把它读成「这些表能用了」）

* **不跑任何 SQL**：「语句存在」不等于「行落库」（那是 `tools/run_sql_smoke.py` 与 store
  测试的活）。
* **不判断写入方写得对不对**：只判断存在、归属、关键能力还在。
* **不扫 `db/*.sql` 里的写入路径**（触发器 / 函数）：本仓库没有；真加了得先扩这条的扫描面。
* **够不着的契约表面登记在 `PROSE_UNCHECKED` 里**（登记不是覆盖）：本门禁核的是
  「带数字的零写入者读数」与「本块里的三个计数」，契约里那几处**不抄名单**的句子够不着。

## 对脚本自己的约束

* 每个提取步骤后面都跟空转守卫：DDL 里 0 张表、源文件里 0 条写语句、登记块里 0 行 ——
  三者都判 FAIL，而不是打印「0 问题 PASS」。
* 每条判据独立跑，不提前 return（除了空转守卫，因为它之后的每一项都在空集上恒真）。
* `--selftest` 拿真实仓库跑一遍**正控**（真文件必须 0 问题），再拿一批样本逐个喂给**同一个**
  `run_checks()`；每个探测器至少有一个样本让它真的红，外加三个空转守卫的样本。样本条数由
  `--selftest` 自己打出来，别在这里抄。

用法：
  python tools/verify_data_center_table_wiring.py
  python tools/verify_data_center_table_wiring.py --selftest
"""

import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DDL_REL = 'db/data_center.sql'
CONTRACT_REL = os.path.join('docs', '智能量化交易平台-数据中心接口契约文档.md')

#: 登记处所在的章节号。它是**契约里的一节**，不是本文件里的常量表 —— 理由见上面
#: 「登记而不是覆盖」：理由要写在权威文档里，门禁只核对它还在不在、对不对得上。
REGISTRY_SECTION = 'B20.7'

#: 与风控侧**同一个**标记词（`tools/verify_risk_table_wiring.py` 的 `EXEMPT_MARKER`）。
#: 复用它是有意的：读的人一眼就知道这是同一套约定，不需要记住两个词。
EXEMPT_MARKER = 'WIRING-EXEMPT'

SOURCE_GLOBS = ('quanauto/*.py',)

#: 这些目录下的写语句**不算产品写入者**（它们顶多证明「测试能写」）。仍然会被提取，
#: 因为一条「只有测试在写」的读数应当出现在失败消息里，而不是被静默丢掉。
NON_PRODUCT_PREFIXES = ('tests/', 'tools/', 'platform/')

# ---------------------------------------------------------------------------
# 期望（手写）：每张有产品写入者的 dc 表一条
# ---------------------------------------------------------------------------
# writer: 写入方（类名）。按「谁引用了那条语句」归属：模块级 SQL 常量 → 引用它的类；
#         类属性 / 行内字面量 → 所在类；两者都没有 → `<module>`。
# caps:   (这是哪条性质, 正则, 钉在哪个常量上或 None)。第三条是**归属域**：`None` 表示
#         「这张表的全部写语句」都算；给了常量名，就只在这条常量绑定的字面量上找。
#         `dc_ingest_run` 有两条语句（开批次 / 收批次），所以它必须区分 —— 否则
#         「INSERT 的 RETURNING」会被 UPDATE 的 RETURNING 顶掉，判据变成假绿。
POLICY = {
    'dc_daily_bar': {
        'writer': 'PgBarIngestor',
        'caps': [
            ('写入必须幂等：同键重跑不新增行（约定 1 / D10）',
             re.compile(r'\bON\s+CONFLICT\s*\(\s*symbol\s*,\s*trade_date\s*,\s*data_version\s*\)'
                        r'\s*DO\s+UPDATE\b'), None),
            ('同值不写：值没变就不发 UPDATE（约定 6 的核心）',
             re.compile(r'\bIS\s+DISTINCT\s+FROM\b'), None),
        ],
    },
    'dc_adjust_factor': {
        'writer': 'PgFactorIngestor',
        'caps': [
            ('写入必须幂等：同键重跑不新增行（约定 1 / D10）',
             re.compile(r'\bON\s+CONFLICT\s*\(\s*symbol\s*,\s*trade_date\s*,\s*data_version\s*\)'
                        r'\s*DO\s+UPDATE\b'), None),
            ('判等时库侧列必须点名（`别名化` 会让「拿库里的值和 EXCLUDED 比」这一点不可读）',
             re.compile(r'\bdc_adjust_factor\.adjust_factor\s+IS\s+DISTINCT\s+FROM\s+'
                        r'EXCLUDED\.adjust_factor\b'), None),
        ],
    },
    'dc_dividend': {
        'writer': 'PgDividendIngestor',
        'caps': [
            ('写入必须幂等：同键重跑不新增行（事件日是 `ex_date`，不是 `trade_date`）',
             re.compile(r'\bON\s+CONFLICT\s*\(\s*symbol\s*,\s*ex_date\s*,\s*data_version\s*\)'
                        r'\s*DO\s+UPDATE\b'), None),
            ('公告日也参与判等：漏掉它 ⇒ 公告日变了的新值会被静默丢掉（附录 B22）',
             re.compile(r'\(\s*dc_dividend\.announce_date\s*,\s*dc_dividend\.cash_per_share\s*\)'
                        r'\s*IS\s+DISTINCT\s+FROM'), None),
        ],
    },
    'dc_ingest_run': {
        'writer': 'IngestRunLog',
        'caps': [
            ('开批次必须回 `run_id`：拿不到就不许继续跑（否则收尾静默改 0 行）',
             re.compile(r'\bRETURNING\s+run_id\b'), 'SQL_START_RUN'),
            ('收批次的时间戳来自库（`CURRENT_TIMESTAMP(3)`，与 DDL 默认值同精度），不传参',
             re.compile(r'finished_at\s*=\s*CURRENT_TIMESTAMP\(3\)'), 'SQL_FINISH_RUN'),
        ],
    },
}

#: 契约里够不着、只能人工看的表面。登记**不是**覆盖：它在这里的作用是让「够不着」这件事
#: 本身可核对 —— 锚点从契约里消失（那一节被重写在别处了）也会 FAIL，逼人回来重读一遍。
PROSE_UNCHECKED = (
    # 附录 J 的 J-10 行：现值一列明文说「以 B20.6 那一格为准」，本身就是「不抄名单」。
    '仍是零写入者的 dc 表',
    # J 那一片里「仍开着零写入者的：…」那句 —— 它不抄名单，所以没有数字可供比对。
    '仍开着零写入者的',
    # B20.7 自己那一句：读者须知名单不在这里。
    '**不抄名单**',
)

# ---------------------------------------------------------------------------
# 提取
# ---------------------------------------------------------------------------

WRITE_KEYWORD_RE = re.compile(r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b', re.I)
TABLE_TOKEN_RE = re.compile(r'\bdc_[a-z0-9_]+')
CREATE_TABLE_RE = re.compile(r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                             r'(?:[A-Za-z_][\w$]*\.)?("?)(dc_[a-z0-9_]+)\1', re.I | re.M)
SECTION_RE = re.compile(r'^#{1,4}[ \t]+' + re.escape(REGISTRY_SECTION) + r'\b', re.M)
NEXT_HEADING_RE = re.compile(r'^#{1,4}[ \t]+\S', re.M)
ROW_RE = re.compile(r'^\|[ \t]*`(dc_[a-z0-9_]+)`[ \t]*\|([^|]*)\|([^|]*)\|[ \t]*$', re.M)
MARKER_RE = re.compile(EXEMPT_MARKER + r'[ \t]*:[ \t]*([^\n]*)')

#: 「仍是零写入者的 N 张」这一类**带数字**的现值陈述。契约里出现几处就核几处 —— B20.6 那一格
#: 与 B20.7 的现值行用的是同一句式，所以这一个正则同时覆盖「那一格」与「这一块」。
#: 数字两侧的 `*` 可有可无：契约里写的是 `**仍是零写入者的 6 张**`（粗体在整句上），
#: 换个作者可能写成 `**仍是零写入者的** 6 张` —— 两种都该认出来。
ZERO_WRITER_COUNT_RE = re.compile(r'仍(?:是)?零写入者的[ \t]*\*{0,2}(\d+)\*{0,2}[ \t]*张')
#: 这个句式只该出现在登记块里，所以按**块**取值。
BLOCK_COUNT_RE = {
    'tables': re.compile(r'dc 表共[ \t]*\*\*(\d+)\*\*[ \t]*张'),
    'writers': re.compile(r'有产品写入者的[ \t]*\*\*(\d+)\*\*[ \t]*张'),
    'exempt': ZERO_WRITER_COUNT_RE,
}
COUNT_LABEL = {'tables': 'dc 表共 N 张', 'writers': '有产品写入者的 N 张',
               'exempt': '仍是零写入者的 N 张'}

REASON_COL = '为什么现在可以没有写入者'
NEED_COL = '关它要什么'
REASON_MIN_CHARS = 12


def read_text(path):
    """读成 LF 结尾的文本。CRLF 归一化是必须的：下面几条 `^` 锚定的正则都按 `\\n` 写。"""
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read().replace('\r\n', '\n').replace('\r', '\n')


def ddl_tables(ddl_text):
    """`db/data_center.sql` 里的表名集合（按出现顺序）。"""
    seen = []
    for m in CREATE_TABLE_RE.finditer(ddl_text):
        name = m.group(2)
        if name not in seen:
            seen.append(name)
    return seen


def source_texts(root):
    """{相对路径: 文本} —— 只看产品代码（`quanauto/*.py`）。"""
    out = {}
    for pattern in SOURCE_GLOBS:
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            out[rel] = read_text(path)
    return out


def _docstring_ids(tree):
    """树里所有 docstring 节点的 id。模块/类/函数的**第一条**语句若是字符串就是 docstring。"""
    ids = set()
    owners = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for owner in owners:
        body = getattr(owner, 'body', None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _string_sites(tree):
    """→ [(节点, 值, 所在类名或 None, 绑定的名字或 None)]

    绑定名 = 「这个字面量是谁」：模块级 `NAME = "..."` 或类属性 `NAME = "..."` 给 `NAME`；
    `foo(sql="...")` 这类关键字实参给参数名。有了它，才能把一条语句**归属**到引用它的类
    （`_class_refs`），而不是瞎猜。每个字面量**只出现一次**（带归属的走前两趟，剩下的走第三趟）
    —— 重复计入会让「一张表有几条写语句」这个读数虚高。

    识别不了的东西：f-string 拼出来的语句（`f"UPDATE {t}"`）、从别处拼进来的表名。
    本仓库的写语句都是普通常量 + `%s` 占位，没走到这条缝上（真走了会在 ① 步露出来：
    表没人认领 ⇒ `DCW-EXEMPT-MISSING` 或 `DCW-NO-WRITER`）。
    """
    sites = []
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        flat = _flatten_str(value)
        if flat is None:
            continue
        name = None
        for target in targets:
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
        covered.add(id(value))
        sites.append((value, flat, owner_of(tree, value), name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                flat = _flatten_str(kw.value)
                if flat is None:
                    continue
                covered.add(id(kw.value))
                sites.append((kw.value, flat, owner_of(tree, node), kw.arg))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in covered:
            sites.append((node, node.value, owner_of(tree, node), None))
    return sites


def _flatten_str(node):
    """把 `"a" "b"` / `"a" + "b"` 折叠成一个字符串；别的表达式 → None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _flatten_str(node.left)
        right = _flatten_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


_OWNER_CACHE = {}


def _owner_map(tree):
    """`{id(节点): 最小包含它的类名或 None}`，一趟建好。

    原来每问一次就 `ast.walk` 一遍、是 O(n²)；`pgstore.py` 上千行，白付。
    缓存里连着树一起存 ⇒ `id()` 不会被回收复用骗到。
    """
    key = id(tree)
    hit = _OWNER_CACHE.get(key)
    if hit is not None and hit[0] is tree:
        return hit[1]
    owner = {id(tree): None}
    stack = [(tree, None)]
    while stack:
        node, current = stack.pop()
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.ClassDef) else current
            owner[id(child)] = name
            stack.append((child, name))
    _OWNER_CACHE[key] = (tree, owner)
    return owner


def owner_of(tree, node):
    """`node` 落在哪个类里（取最小的那个）。不落在类里 → None。"""
    return _owner_map(tree).get(id(node))


def _class_refs(tree):
    """{类名: 该类（含其方法）的名字引用集合}。"""
    out = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        names = set()
        for node in ast.walk(cls):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        out[cls.name] = names
    return out


def scan_source(rel, text, tables):
    """→ ({表: {'scopes': {…}, 'literals': [(绑定名或 None, 语句)]}}, 有没有看到写关键字)

    只有**带写关键字**且提到某张 dc 表的字符串才算一条语句。docstring 一律不算。
    """
    found = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return found, False
    docs = _docstring_ids(tree)
    refs = _class_refs(tree)
    had_keyword = False
    for node, value, cls, holder in _string_sites(tree):
        if id(node) in docs:
            continue
        if not WRITE_KEYWORD_RE.search(value):
            continue
        had_keyword = True
        names = set(TABLE_TOKEN_RE.findall(value)) & tables
        if not names:
            continue
        scopes = set()
        if cls is not None:
            scopes.add(cls)
        if holder:
            owners = {name for name, names_ in refs.items() if holder in names_}
            scopes |= owners or ({'<module>'} if cls is None else {cls})
        elif cls is None:
            scopes.add('<module>')
        for name in sorted(names):
            slot = found.setdefault(name, {'scopes': set(), 'literals': []})
            slot['scopes'] |= scopes
            slot['literals'].append((holder, value))
    return found, had_keyword


def registry_block(contract_text):
    """登记块的文本；没有那一节 → None。"""
    m = SECTION_RE.search(contract_text)
    if not m:
        return None
    nxt = NEXT_HEADING_RE.search(contract_text, m.end())
    return contract_text[m.end():nxt.start() if nxt else len(contract_text)]


def outside_block(contract_text, block):
    """契约里**除登记块之外**的文本（按块内容做一次整体删除）。"""
    return contract_text.replace(block, '\n', 1) if block else contract_text


def class_names(sources):
    out = set()
    for text in sources.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                out.add(node.name)
    return out


def constants(sources):
    """模块级 / 类属性里的 `NAME = "..."` 常量名集合（供 caps 的归属域核对）。"""
    out = set()
    for text in sources.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        out.add(target.id)
    return out


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def run_checks(ddl_text, sources, contract_text, policy=None, prose_unchecked=None):
    """→ (problems [(码, 消息)], notes [str], stats dict)。纯函数，不碰文件系统。"""
    policy = POLICY if policy is None else policy
    prose_unchecked = PROSE_UNCHECKED if prose_unchecked is None else prose_unchecked
    problems = []
    notes = []

    def fail(code, msg):
        problems.append((code, msg))

    tables = ddl_tables(ddl_text)
    if not tables:
        fail('DCW-EMPTY', '在 %s 里一张 dc 表都没提取到 —— 提取为空时下面每一项都在空转，'
                          '所以这里直接判 FAIL' % DDL_REL)
        return problems, notes, {}
    table_set = set(tables)

    if not sources:
        fail('DCW-EMPTY', '一个源文件都没读到（%s）—— 提取为空，不能报「0 问题」'
             % ', '.join(SOURCE_GLOBS))
        return problems, notes, {}

    writers, sites, keyword_files = {}, {}, 0
    for rel in sorted(sources):
        found, had_keyword = scan_source(rel, sources[rel], table_set)
        keyword_files += 1 if had_keyword else 0
        for table, info in found.items():
            slot = writers.setdefault(table, {'product': set(), 'other': set(), 'files': set()})
            slot['files'].add(rel)
            slot['product' if not rel.startswith(NON_PRODUCT_PREFIXES) else 'other'] |= info['scopes']
            sites.setdefault(table, []).extend(info['literals'])

    if not keyword_files:
        fail('DCW-EMPTY', '扫过的 %d 个源文件里一条 INSERT/UPDATE/DELETE 语句都没有 —— '
                          '扫描面坏了（比如写关键字正则失配），不是「代码很干净」'
             % len(sources))
        return problems, notes, {}

    block = registry_block(contract_text)
    rows, marker = {}, set()
    if block is None:
        fail('DCW-REGISTRY-MISSING', '契约里找不到 `%s` 那一节 —— 没有登记处，'
                                     '下面「每张没有写入者的表都登记了」这一项就无从核对'
             % REGISTRY_SECTION)
    else:
        for m in ROW_RE.finditer(block):
            rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
        if not rows:
            fail('DCW-EMPTY', '契约 `%s` 那一节里一行登记都没解析出来 —— 提取为空时'
                              '「每张表都登记了」恒真' % REGISTRY_SECTION)
        mm = MARKER_RE.search(block)
        if mm:
            marker = set(TABLE_TOKEN_RE.findall(mm.group(1)))
        else:
            fail('DCW-REGISTRY-MISSING', '契约 `%s` 那一节里没有 `%s:` 字面标记行 —— '
                                         '标记是给读契约的人看的索引，不能只有表格'
                 % (REGISTRY_SECTION, EXEMPT_MARKER))
    exempt_registered = set(rows) | marker

    # ① 逐表：有写入者 ↔ 在 POLICY；没写入者 ↔ 在契约登记里
    for table in tables:
        slot = writers.get(table, {'product': set(), 'other': set(), 'files': set()})
        product, other = slot['product'], slot['other']
        entry = policy.get(table)
        if entry:
            if not product:
                tail = ('（只有 %s 里有写它的语句 —— 测试/工具里的写入方**不算**产品写入者）'
                        % ', '.join(sorted(slot['files']))) if other else ''
                fail('DCW-NO-WRITER', '`%s` 在 POLICY 里声称写入方是 `%s`，但 %s 里没有任何'
                                      '产品写语句提到它%s'
                     % (table, entry['writer'], '/'.join(SOURCE_GLOBS), tail))
            elif entry['writer'] not in product:
                fail('DCW-WRONG-WRITER', '`%s` 的写入方按 POLICY 应该是 `%s`，实际检测到的是 '
                                         '%s' % (table, entry['writer'],
                                                 ', '.join(sorted(product))))
        elif product:
            fail('DCW-POLICY-MISSING', '`%s` 已经有产品写入者（%s）但 POLICY 里没有它 —— '
                                       '新写入路径必须登记，否则它的关键能力没人守'
                 % (table, ', '.join(sorted(product))))
        if not product and table not in exempt_registered:
            fail('DCW-EXEMPT-MISSING', '`%s` 没有产品写入者，也没在契约 `%s` 里登记豁免 —— '
                                       '新加一张没人写的表必须逼出一次人类决定'
                 % (table, REGISTRY_SECTION))
        if product and table in exempt_registered:
            fail('DCW-EXEMPT-STALE', '`%s` 已经有产品写入者（%s），但契约 `%s` 里还登记着豁免 —— '
                                     '豁免长成永久免检的挡箭牌了'
                 % (table, ', '.join(sorted(product)), REGISTRY_SECTION))

    # ② 写入方确实是个类（POLICY 里的类名不能是幻影）
    defined = class_names(sources)
    for table in sorted(policy):
        writer = policy[table]['writer']
        if writer not in defined:
            fail('DCW-CLASS-MISSING', '`%s` 点名的写入方 `%s` 在任何源文件里都没有定义'
                 % (table, writer))

    # ③ 关键能力还在
    known_constants = constants(sources)
    for table in sorted(policy):
        for what, rx, holder in policy[table]['caps']:
            if rx.match('') is not None or rx.search('') is not None:
                fail('DCW-CAP-VACUOUS', '`%s` 的能力判据 %r 能匹配空串（等于没有判据）：%s'
                     % (table, rx.pattern, what))
                continue
            if holder is not None and holder not in known_constants:
                fail('DCW-CAP-MISSING', '`%s` 的这条能力钉在常量 `%s` 上，但源里没有这个常量'
                                        '（改名了？）：%s' % (table, holder, what))
                continue
            pool = [value for name, value in sites.get(table, [])
                    if holder is None or name == holder]
            if not pool:
                fail('DCW-CAP-MISSING', '`%s` 的写语句里找不到这条能力（%s%s）'
                     % (table, what, '' if holder is None else '，域：`%s`' % holder))
            elif not any(rx.search(value) for value in pool):
                fail('DCW-CAP-MISSING', '`%s` 的%s里 `%s` 不在了：%s'
                     % (table, '' if holder is None else ' `%s`' % holder, what,
                        '（判据 %r）' % rx.pattern))

    # ④ 登记处自身的双向核对
    for table in sorted(set(policy) - table_set):
        fail('DCW-POLICY-GHOST', 'POLICY 里有 `%s`，但 %s 里没有这张表' % (table, DDL_REL))
    for table in sorted(exempt_registered - table_set):
        fail('DCW-EXEMPT-GHOST', '契约 `%s` 里登记了 `%s` 的豁免，但 %s 里没有这张表'
             % (REGISTRY_SECTION, table, DDL_REL))
    for table in sorted(rows):
        why, need = rows[table]
        if len(why) < REASON_MIN_CHARS:
            fail('DCW-REASON-MISSING', '契约里 `%s` 那一行的「%s」是空的/太短（%r）—— '
                                       '没有理由的豁免就是挡箭牌' % (table, REASON_COL, why))
        if len(need) < REASON_MIN_CHARS:
            fail('DCW-REASON-MISSING', '契约里 `%s` 那一行的「%s」是空的/太短（%r）—— '
                                       '不写清关它要什么，这张表就会永远留在豁免里'
                 % (table, NEED_COL, need))
    if rows and marker and set(rows) != marker:
        fail('DCW-MARKER-MISMATCH', '契约 `%s` 的 `%s:` 标记行与登记表不是同一组：'
                                    '只在标记行里 %s；只在表里 %s'
             % (REGISTRY_SECTION, EXEMPT_MARKER,
                sorted(marker - set(rows)) or '（无）', sorted(set(rows) - marker) or '（无）'))

    # ⑤ 现值计数：本块里三个数、契约里每一处带数字的陈述
    expected = {
        'tables': len(tables),
        'writers': sum(1 for t in tables if writers.get(t, {'product': set()})['product']),
        'exempt': sum(1 for t in tables if not writers.get(t, {'product': set()})['product']),
    }
    if block is not None:
        for key, rx in sorted(BLOCK_COUNT_RE.items()):
            hits = rx.findall(block)
            if not hits:
                fail('DCW-COUNT-MISMATCH', '契约 `%s` 那一节里读不到「%s」这个数 —— '
                                           '缺了它就没人知道现值是多少'
                     % (REGISTRY_SECTION, COUNT_LABEL[key]))
            elif len(hits) > 1:
                fail('DCW-COUNT-MISMATCH', '契约 `%s` 那一节里「%s」这个数出现了 %d 次，'
                                           '取不到唯一的值' % (REGISTRY_SECTION, COUNT_LABEL[key],
                                                              len(hits)))
            elif int(hits[0]) != expected[key]:
                fail('DCW-COUNT-MISMATCH', '契约 `%s` 里「%s」写的是 %s，按 %s 与源码现数是 %d'
                     % (REGISTRY_SECTION, COUNT_LABEL[key], hits[0], DDL_REL, expected[key]))
    # 一行里同一个读数可能出现两次（实测 B20.6 那一格就是这样）；逐**行**报一次，
    # 免得同一处漂移在报告里看起来像两个问题。
    seen_prose = set()
    for m in ZERO_WRITER_COUNT_RE.finditer(contract_text):
        if int(m.group(1)) == expected['exempt']:
            continue
        line_no = contract_text[:m.start()].count('\n') + 1
        if (line_no, m.group(0)) in seen_prose:
            continue
        seen_prose.add((line_no, m.group(0)))
        fail('DCW-PROSE-MISMATCH', '契约里「%s」写的是 %s 张，现数是 %d 张（第 %d 行）'
             % (m.group(0), m.group(1), expected['exempt'], line_no))
    outside = outside_block(contract_text, block)
    for line_no, line in enumerate(outside.split('\n'), start=1):
        if '零写入者' not in line or ZERO_WRITER_COUNT_RE.search(line):
            continue
        if any(anchor in line for anchor in prose_unchecked):
            continue
        fail('DCW-PROSE-MISMATCH', '契约第 %d 行提到零写入者却没有带数字的读数，'
                                   '也不在 `PROSE_UNCHECKED` 登记里 ⇒ 要么把读数写清楚、'
                                   '要么登记它：%s' % (line_no, line.strip()[:100]))
    for anchor in prose_unchecked:
        if anchor not in contract_text:
            fail('DCW-PROSE-MISMATCH', '`PROSE_UNCHECKED` 里登记的 %r 在契约里找不到了 —— '
                                       '登记过期（那一节被重写了？），删掉它或改锚点'
                 % anchor)

    stats = {
        'tables': len(tables),
        'writers': expected['writers'],
        'exempt': expected['exempt'],
        'sites': sum(len(v) for v in sites.values()),
        'files': len(sources),
    }

    notes.append('TOTAL tables=%d writers=%d exempt=%d sites=%d' % (
        stats['tables'], stats['writers'], stats['exempt'], stats['sites']))
    for table in tables:
        slot = writers.get(table, {'product': set(), 'other': set()})
        if slot['product']:
            notes.append('  [writer] %s <- %s' % (table, ', '.join(sorted(slot['product']))))
        elif table in exempt_registered:
            notes.append('  [exempt] %s （登记在契约 %s）' % (table, REGISTRY_SECTION))
        else:
            notes.append('  [exempt] %s （**未登记**，见 DCW-EXEMPT-MISSING）' % table)
    return problems, notes, stats


# ---------------------------------------------------------------------------
# 自测夹具（只在内存里，不碰真文件）
# ---------------------------------------------------------------------------

FIX_DDL = """\
CREATE TABLE IF NOT EXISTS dc_alpha (
    alpha_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      text NOT NULL
);

CREATE TABLE IF NOT EXISTS dc_beta (
    beta_id     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      text NOT NULL
);

CREATE TABLE IF NOT EXISTS dc_orphan (
    orphan_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      text NOT NULL
);
"""

FIX_WRITER = """\
SQL_UPSERT_ALPHA = (
    "INSERT INTO dc_alpha (symbol) "
    "VALUES (%s) "
    "ON CONFLICT (symbol) DO UPDATE SET symbol = EXCLUDED.symbol "
    "WHERE dc_alpha.symbol IS DISTINCT FROM EXCLUDED.symbol "
    "RETURNING alpha_id"
)


class AlphaWriter:
    def write(self, rows):
        return _run(self.conn, SQL_UPSERT_ALPHA, rows, "写入 dc_alpha")
"""

FIX_LOWER_WRITER = FIX_WRITER.replace('INSERT INTO dc_alpha', 'insert into dc_alpha')

FIX_MODULE_WRITER = """\
SQL_UPSERT_BETA = "INSERT INTO dc_beta (symbol) VALUES (%s)"


def write_beta(conn, symbol):
    return _run(conn, SQL_UPSERT_BETA, (symbol,), "写入 dc_beta")
"""

FIX_DOC_ONLY = '''\
"""INSERT INTO dc_gamma (symbol) VALUES (%s) —— 这是 docstring，不是写入者。"""

# 注释里也提一句：INSERT INTO dc_gamma (symbol) VALUES (%s)
'''

FIX_REGISTRY = """\
#### B20.7 零写入者登记处（自测夹具）

现值（数出来的）：dc 表共 **3** 张 · 有产品写入者的 **1** 张 · **仍是零写入者的 2 张**。

字面标记 `WIRING-EXEMPT: dc_beta dc_orphan`。

| 表 | 为什么现在可以没有写入者 | 关它要什么 |
| --- | --- | --- |
| `dc_beta` | 这一迭代不做这个功能，属于「还没做」而不是「漏做」 | 要有产品需求才轮到它，现在没有 |
| `dc_orphan` | 同上：缺的是需求，不是实现 | 同上：要有需求，现在还没有 |

本表**不抄名单**（零写入者的名单见 B20.6 那一格）。
"""

FIX_REGISTRY_DOC = FIX_REGISTRY.replace(
    'dc 表共 **3** 张 · 有产品写入者的 **1** 张 · **仍是零写入者的 2 张**',
    'dc 表共 **4** 张 · 有产品写入者的 **1** 张 · **仍是零写入者的 3 张**'
).replace(
    '`WIRING-EXEMPT: dc_beta dc_orphan`',
    '`WIRING-EXEMPT: dc_beta dc_orphan dc_gamma`'
).replace(
    '| `dc_orphan` | 同上：缺的是需求，不是实现 | 同上：要有需求，现在还没有 |',
    '| `dc_orphan` | 同上：缺的是需求，不是实现 | 同上：要有需求，现在还没有 |\n'
    '| `dc_gamma` | 同上：缺的是需求，不是实现 | 同上：要有需求，现在还没有 |'
)

FIX_DDL_DOC = FIX_DDL + """
CREATE TABLE IF NOT EXISTS dc_gamma (
    gamma_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      text NOT NULL
);
"""

FIX_POLICY = {
    'dc_alpha': {
        'writer': 'AlphaWriter',
        'caps': [
            ('写入必须幂等（同键重跑不新增行）',
             re.compile(r'\bON\s+CONFLICT\s*\(\s*symbol\s*\)\s+DO\s+UPDATE\b'), None),
            ('同值不写（值没变就不发 UPDATE）',
             re.compile(r'\bIS\s+DISTINCT\s+FROM\b'), None),
        ],
    },
}

FIX_SOURCES = {'quanauto/alpha.py': FIX_WRITER}
FIX_ANCHOR = '**不抄名单**'


def _base(**over):
    """正控的输入，可按需覆盖任意一项。"""
    data = {
        'ddl_text': FIX_DDL,
        'sources': dict(FIX_SOURCES),
        'contract_text': FIX_REGISTRY,
        'policy': FIX_POLICY,
        'prose_unchecked': (FIX_ANCHOR,),
    }
    data.update(over)
    return data


def selftest(root):
    """真文件的正控 + 每个探测器至少一个样本；提取为空的三条各有一个样本。"""
    lines = []
    checks = []

    def check(name, ok, detail):
        checks.append((name, bool(ok), detail))
        lines.append('  [%s] %s -- %s' % ('ok' if ok else 'NO', name, detail))

    def run(name, want, forbid=(), **over):
        problems, notes, stats = run_checks(**_base(**over))
        codes = [code for code, _ in problems]
        if not want:
            ok = not codes
        else:
            ok = all(w in codes for w in want) and not any(f in codes for f in forbid)
        check(name, ok, 'want=%s got=%s | %s' % (list(want) or 'clean', codes or 'clean',
                                                 (problems[0][1][:150] if problems else '')))
        return problems, notes

    # ── 正控：**真文件**必须 0 问题 ─────────────────────────────────────────
    # 上面那批是内存夹具 —— 夹具只证明「探测器会红」。真文件跑一遍才证明它在本仓库上
    # 真的**绿**（提取面没瞎、登记处的现值没漂）。
    real, real_notes, real_stats = run_checks(
        read_text(os.path.join(root, DDL_REL)),
        source_texts(root),
        read_text(os.path.join(root, CONTRACT_REL)))
    check('positive-real-files', not real,
          '真文件必须 0 问题：tables=%s writers=%s exempt=%s%s'
          % (real_stats.get('tables'), real_stats.get('writers'), real_stats.get('exempt'),
             '' if not real else ' | ' + '; '.join('%s: %s' % (c, m[:120]) for c, m in real[:4])))
    check('positive-real-notes', any(n.startswith('TOTAL tables=') for n in real_notes),
          '真文件也要打出统计行（否则统计行是夹具喂出来的）')

    # ── 正控（夹具） ────────────────────────────────────────────────────────
    problems, notes = run('positive', [])
    check('positive-has-notes', any('TOTAL tables=3 writers=1 exempt=2' in n for n in notes),
          '统计行必须自己跟着走（现值由代码算，不抄）')
    run('positive-lowercase-sql', [], sources={'quanauto/alpha.py': FIX_LOWER_WRITER})
    run('positive-docstring-is-not-a-writer', [],
        ddl_text=FIX_DDL_DOC, contract_text=FIX_REGISTRY_DOC,
        sources=dict(FIX_SOURCES, **{'quanauto/gamma.py': FIX_DOC_ONLY}))

    # 归属：模块级语句常量 → 引用它的类；`def` 引用 → `<module>`
    found, had = scan_source('quanauto/alpha.py', FIX_WRITER, {'dc_alpha'})
    check('SRC-class-scope', had and found.get('dc_alpha', {}).get('scopes') == {'AlphaWriter'},
          '类里的语句要归属到那个类：%s' % sorted(found.get('dc_alpha', {}).get('scopes', [])))
    found, had = scan_source('quanauto/beta.py', FIX_MODULE_WRITER, {'dc_beta'})
    check('SRC-module-scope', had and found.get('dc_beta', {}).get('scopes') == {'<module>'},
          '模块级函数（没有类）要归属到 <module>：%s'
          % sorted(found.get('dc_beta', {}).get('scopes', [])))
    found, had = scan_source('quanauto/gamma.py', FIX_DOC_ONLY, {'dc_gamma'})
    check('SRC-docstring-dropped', (not found) and (not had),
          'docstring/注释不是写入者：found=%s 写关键字=%s'
          % (sorted(found), had))

    # ── 提取为空的三条空转守卫 ───────────────────────────────────────────────
    run('empty-ddl', ['DCW-EMPTY'], ddl_text='/* 一张表都没有 */\n')
    run('empty-sources', ['DCW-EMPTY'], sources={})
    run('empty-no-write-keyword', ['DCW-EMPTY'],
        sources={'quanauto/quiet.py': 'X = 1\nY = "统计口径见契约"\n'})
    run('empty-registry', ['DCW-EMPTY', 'DCW-EXEMPT-MISSING'],
        contract_text='#### B20.7 零写入者登记处\n\n（表格还没写）\n')

    # ── 逐表：写入者 ↔ POLICY ↔ 契约登记 ────────────────────────────────────
    run('no-writer', ['DCW-NO-WRITER'],
        sources={'tests/test_alpha.py': FIX_WRITER},
        forbid=['DCW-CAP-MISSING'])
    run('wrong-writer', ['DCW-WRONG-WRITER'],
        policy={'dc_alpha': {'writer': '<module>', 'caps': FIX_POLICY['dc_alpha']['caps']}})
    run('policy-missing', ['DCW-POLICY-MISSING'], policy={})
    run('class-missing', ['DCW-CLASS-MISSING', 'DCW-WRONG-WRITER'],
        policy={'dc_alpha': {'writer': 'WroteAway', 'caps': []}})
    run('exempt-stale', ['DCW-EXEMPT-STALE', 'DCW-COUNT-MISMATCH'],
        sources=dict(FIX_SOURCES, **{'quanauto/beta.py': FIX_MODULE_WRITER}))
    run('exempt-missing', ['DCW-EXEMPT-MISSING'],
        contract_text=FIX_REGISTRY.replace(
            '| `dc_beta` | 这一迭代不做这个功能，属于「还没做」而不是「漏做」 | 要有产品需求才轮到它，现在没有 |\n',
            '').replace('`WIRING-EXEMPT: dc_beta dc_orphan`', '`WIRING-EXEMPT: dc_orphan`'))
    run('registry-missing', ['DCW-REGISTRY-MISSING', 'DCW-EXEMPT-MISSING'],
        contract_text='# 契约\n\n（这一节被删掉了）\n')

    # ── 登记处自身的核对 ────────────────────────────────────────────────────
    run('exempt-ghost', ['DCW-EXEMPT-GHOST', 'DCW-MARKER-MISMATCH'],
        contract_text=FIX_REGISTRY.replace('`WIRING-EXEMPT: dc_beta dc_orphan`',
                                           '`WIRING-EXEMPT: dc_beta dc_orphan dc_ghost`'))
    run('marker-mismatch', ['DCW-MARKER-MISMATCH'],
        contract_text=FIX_REGISTRY.replace('dc_beta dc_orphan`', 'dc_beta`'))
    run('reason-missing', ['DCW-REASON-MISSING'],
        contract_text=FIX_REGISTRY.replace(
            '| `dc_beta` | 这一迭代不做这个功能，属于「还没做」而不是「漏做」 |',
            '| `dc_beta` | — |'))
    run('policy-ghost', ['DCW-POLICY-GHOST'],
        policy=dict(FIX_POLICY, **{'dc_ghost': {'writer': 'AlphaWriter', 'caps': []}}))

    # ── 关键能力 ────────────────────────────────────────────────────────────
    run('capability-gone', ['DCW-CAP-MISSING'],
        sources={'quanauto/alpha.py': FIX_WRITER.replace('IS DISTINCT FROM', 'IS NOT DISTINCT FROM')})
    run('cap-holder-missing', ['DCW-CAP-MISSING'],
        policy={'dc_alpha': {'writer': 'AlphaWriter', 'caps': [
            ('开批次必须回 run_id', re.compile(r'\bRETURNING\s+run_id\b'), 'SQL_NOT_THERE')]}})
    run('cap-pool-empty', ['DCW-CAP-MISSING'],
        sources=dict(FIX_SOURCES, **{'quanauto/other.py': 'SQL_OTHER = "SELECT 1"\n'}),
        policy={'dc_alpha': {'writer': 'AlphaWriter', 'caps': [
            ('这条能力只钉在另一条常量上', re.compile(r'\bRETURNING\s+alpha_id\b'), 'SQL_OTHER')]}})
    run('cap-vacuous', ['DCW-CAP-VACUOUS'],
        policy={'dc_alpha': {'writer': 'AlphaWriter', 'caps': [
            ('能匹配空串的判据', re.compile(r'.*'), None)]}})

    # ── 现值计数与散文 ──────────────────────────────────────────────────────
    run('count-drift', ['DCW-COUNT-MISMATCH'],
        contract_text=FIX_REGISTRY.replace('dc 表共 **3** 张', 'dc 表共 **4** 张'))
    run('count-ambiguous', ['DCW-COUNT-MISMATCH'],
        contract_text=FIX_REGISTRY.replace(
            '有产品写入者的 **1** 张',
            '有产品写入者的 **1** 张（口径见下）· 有产品写入者的 **1** 张'))
    run('count-missing', ['DCW-COUNT-MISMATCH'],
        contract_text=FIX_REGISTRY.replace('dc 表共 **3** 张 · ', ''))
    run('prose-drift', ['DCW-PROSE-MISMATCH'],
        contract_text=FIX_REGISTRY + '\n现值（订正后）仍是零写入者的 5 张。\n')
    run('prose-unregistered', ['DCW-PROSE-MISMATCH'],
        contract_text='沿革：这几张零写入者的情况见上表。\n\n' + FIX_REGISTRY)
    run('prose-stale-anchor', ['DCW-PROSE-MISMATCH'],
        prose_unchecked=(FIX_ANCHOR, '契约里根本没有的锚点'))

    bad = [name for name, ok, _ in checks if not ok]
    print('\n'.join(lines))
    print('SELFTEST %s -- %d sample(s), %d failed' % ('OK' if not bad else 'FAIL',
                                                      len(checks), len(bad)))
    return 0 if not bad else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv):
    root = os.path.abspath([a for a in argv[1:] if not a.startswith('--')][0]) \
        if [a for a in argv[1:] if not a.startswith('--')] else ROOT
    if '--selftest' in argv:
        return selftest(root)
    print('data-center-table-wiring -- dc 表写入者门禁（%s）' % root)
    try:
        ddl_text = read_text(os.path.join(root, DDL_REL))
        contract_text = read_text(os.path.join(root, CONTRACT_REL))
        sources = source_texts(root)
    except (IOError, OSError) as exc:
        print('GATE FAIL: 读不到输入：%s' % exc)
        print('verdict: FAIL (setup)')
        return 1
    print('files: %s + %d source file(s) + %s' % (DDL_REL, len(sources), CONTRACT_REL))
    problems, notes, _ = run_checks(ddl_text, sources, contract_text)
    for note in notes:
        print(note)
    for code, msg in problems:
        print('FINDING [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not problems else 'FAIL', len(problems)))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
