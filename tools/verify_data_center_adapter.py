#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data-center-adapter -- 采集侧适配器门禁（tier A）。

这个门禁**只解析源码：不 import pandas、不跑 pytest、不联网**（C4：门禁不许把
「装不装第三方库」变成能不能跑的条件）。`tests/test_data_center_adapter.py` 证明的是
「今天的 32 个用例过了」；这个门禁证明的是另一件事：适配器层那几条「结构上就不该发生」
的性质，在**下一个人加数据源**时不会静默失效。

它盯三条互相独立的承诺，每条都能追到一句原文：

  1. 「源字段名不得泄漏到输出列」（契约 §4 验收表 / D9；`quanauto/datacenter.py`
     `DailyBar` docstring 点名要求本门禁反向检查这条）。
     落地方式是**显式列名映射表**，而不是「名字看着像标准列名就放行」。映射表因此有
     两条可静态检查的性质：值域 ⊆ 标准 schema；键域里出现标准列名时**必须是恒等映射**
     （源恰好同名，如 baostock 的 open/close —— 反过来把 open 指到 close 是真实数据错误）。
     第三条是反向的：标准行类上不得出现源特有字段名。这条用**反推黑名单**判，不用
     「名字看着怪」判 —— `quanauto/models.py` 的 `limit_up` / `turn_rate` 看着也怪，
     但它们是主契约自己的字段，用「怪异度」判会制造假阳性。
  2. 采集侧不写库（D1/D10；`quanauto/datasources.py` 模块 docstring 点名要求本门禁盯这条）。
     适配器里不得出现任何数据库驱动 import，**惰性导入也算** —— 把 psycopg 藏进函数体
     并不改变「谁在写库」。
  3. 源 SDK 只能惰性导入（`_default_fetch` 内）。源库缺失必须由 `_call` 翻译成
     `SourceAdapterError`（DATA_005），模块级导入会让「没装 akshare」升级成
     「`import quanauto.datasources` 直接失败」—— 失败地点从数据源挪到了包导入。

Detector（每个都独立跑、互不 return；`--selftest` 每个探测器一个样本）：

  A0  EXTRACT-PARSE / CONSTANT-MISSING
      源码解析失败，或本门禁依赖的常量不存在/不是字面量。**这不是「没问题」，
      而是「没检查」** —— 改名、挪文件、把常量改成表达式，都会让后续检查空转。
  A1  MAPPING-VALUES-IN-SCHEMA
      列名映射表的**值**必须落在标准 schema 里。
  A2  MAPPING-STANDARD-KEY-IS-IDENTITY
      映射表的键若本身是标准列名，映射必须是恒等（`open -> open`）。
  A3  MAPPING-TABLE-UNCLASSIFIED
      模块级 str->str 映射表必须被 `COLUMN_MAPS` / `VALUE_MAPS` 分类；未分类的表
      不会被任何检查看到，而它很可能正是新数据源赖以归一化的那张表。
  A4  REPORT-TYPE-VALUES-CLOSED
      取值映射表的输出必须落在 `REPORT_TYPES`（契约 §3.6.1 的 `ck_dc_fin_report_type`）里。
  A5  NO-DB-DRIVER-IN-COLLECT
      适配器文件里不得出现数据库驱动 import（含惰性）。
  A6  SOURCE-IMPORT-MUST-BE-LAZY
      源 SDK 不得模块级导入。
  A7  ROW-CLASS-NO-SOURCE-FIELD
      行类（`@dataclass`）上不得出现黑名单里的源特有字段名。黑名单是**反推**出来的：
      映射表键域 ∪ `BAOSTOCK_DROPPED_MARKERS`，再减掉标准列名与本模块 import 进来的名字
      （`date` 这类是类型名，不是源字段名）。
  A8  SCHEMA-MATCHES-CONTRACT-3-2
      `quanauto/datasources.py` 里抄的列名元组必须与契约 §3.2 原文一致；锚点找不到、
      或抽出来一个标识符都没有，都判 FAIL（契约措辞改了要人来同步，不是静默跳过）。
  A9  SCHEMA-TUPLE-UNCLASSIFIED
      模块级 `_COLUMNS` 元组必须登记进 `SCHEMA_TUPLES`，否则它不参与值域判定。
  A10 NON-VACUITY
      上面每个探测器都必须真的看到过目标。提取为空（改名/失配）时所有探测项都在空转，
      却会打印「0 issue(s) PASS」—— 零目标是 FAILURE，不是通过。

覆盖范围就是 `SOURCE_RELS` 点名的三个文件，不是「整个仓库」。要加数据源、要让某个
新模块也被检查，就得往常量里登记 —— 这是刻意的：一个 glob 全仓的门禁没法告诉评审
「它到底看了哪几个文件」，而这里要防的失效模式恰恰是「下一个模块没被看见，所有门禁
却是绿」。

**这个门禁证明不了什么**（写在正文里，避免被读成更多）：它只看形状，看不到内容。
映射表里的源列名是从各源文档抄来的、**从未联网核对过**（akshare/baostock 都没装），
所以「映射方向对、单位对、列名对」这三件事它一概不保证。那条欠账登记在数据中心契约
附录 B。运行时行为由 `tests/test_data_center_adapter.py` 的 32 个用例负责。

用法：
    python tools/verify_data_center_adapter.py             # 检查仓库
    python tools/verify_data_center_adapter.py --selftest  # 证明每个探测器都能变红

退出码：0 = 干净，1 = 至少一条 finding（或输入缺失）。

打印文本用中文（GBK 表示得出来），但刻意不含 GBK 之外的字符：裸跑
`python tools/verify_*.py` 时 stdout 是 cp936 控制台，一个 U+2194 之类的字形就会
把干净的运行变成 UnicodeEncodeError。
"""

import ast
import os
import re
import sys

ADAPTER_REL = os.path.join('quanauto', 'datasources.py')
ROWCLASS_REL = os.path.join('quanauto', 'datacenter.py')
CONTRACT_REL = os.path.join('docs', '智能量化交易平台-数据中心接口契约文档.md')
SOURCE_RELS = (ADAPTER_REL, ROWCLASS_REL, CONTRACT_REL)

# 列名映射表：{源列名: 标准列名}。**新增数据源的映射表必须登记在这里**，否则它既不算
# 对也不算错（A3 会因此报错，而不是静默放行）。
COLUMN_MAPS = ('AKSHARE_DAILY_BAR', 'BAOSTOCK_DAILY_BAR', 'EASTMONEY_FINANCIAL',
               'EASTMONEY_INDEX_MEMBER')
# 取值域映射表：{源取值: 标准取值}。键不是列名，所以不参与 A1/A2 的列名判定。
VALUE_MAPS = ('EASTMONEY_REPORT_TYPE',)
# 定义标准 schema 的四个元组。A1 的「标准列名集合」由它们拼出来（不读
# `STANDARD_COLUMNS` —— 那是个 BinOp，`ast.literal_eval` 拿不到，而且拼法本身就是
# `STANDARD_COLUMNS` 的定义）。
SCHEMA_TUPLES = ('DAILY_BAR_COLUMNS', 'FINANCIAL_REQUIRED_COLUMNS',
                 'FINANCIAL_SUBJECT_COLUMNS', 'INDEX_MEMBER_COLUMNS')
REPORT_TYPES_CONST = 'REPORT_TYPES'
DROPPED_MARKERS_CONST = 'BAOSTOCK_DROPPED_MARKERS'

# 采集侧禁止出现的库：任何「能写库」的驱动都算。
DB_DRIVER_MODULES = ('psycopg', 'psycopg2', 'sqlalchemy', 'asyncpg', 'aiomysql',
                     'pymysql', 'MySQLdb', 'sqlite3', 'oracledb', 'cx_Oracle',
                     'pyodbc', 'clickhouse_driver', 'duckdb', 'redis')
# 源 SDK：只能出现在函数体内。
SOURCE_SDK_MODULES = ('akshare', 'baostock', 'tushare', 'jqdatasdk', 'rqdatac',
                      'efinance', 'adata', 'yfinance')

# 契约 §3.2 的三段列名清单：(标签, 对应常量, 起点锚, 终点锚)。
# 锚都是实测唯一的（`--selftest` 的 NEG8b 会证明「锚丢了 = 判 FAIL」而不是跳过）。
CONTRACT_SCHEMA_SPECS = (
    ('daily', 'DAILY_BAR_COLUMNS', '列名必须是标准 schema ——', '，且'),
    ('financial', 'FINANCIAL_REQUIRED_COLUMNS', '必须包含列 ——', '（缺失该列'),
    ('index', 'INDEX_MEMBER_COLUMNS', '列名 ——', '。'),
)

# 契约正文里除列名之外还会有散文（如财务清单尾巴上的「各财务科目」）。散文允许存在，
# 但要被打印出来（`contract-prose:`），并且「一个标识符都没抽到」必须判 FAIL。
IDENT_RE = re.compile(r'^[a-z_][a-z0-9_]*$')

STAT_KEYS = ('scanned_modules', 'str_maps', 'column_maps', 'value_maps',
             'schema_bindings', 'module_imports', 'lazy_source_imports',
             'row_classes', 'denylist_names', 'contract_schemas')

# A10 的下限：必须是「这个仓库当前实测到的东西」的最小值，不是愿望值。
# 阈值定高一点是刻意的 —— 提取器一旦失配，这些数会整体掉到 0，而 0 必须红。
MIN_STATS = (
    ('str_maps', 5, '模块级 str->str 映射表（4 张列名表 + 1 张取值表）'),
    ('column_maps', 4, '解析到的列名映射表'),
    ('value_maps', 1, '解析到的取值域映射表'),
    ('schema_bindings', 4, '标准 schema 列名元组'),
    ('module_imports', 1, '模块级 import'),
    ('lazy_source_imports', 1, '函数体内导入的源 SDK（A6 的证据：看不到就说明惰性导入没了）'),
    ('row_classes', 1, '被检查的数据类'),
    ('denylist_names', 1, '反推出来的源特有字段名黑名单'),
    ('contract_schemas', 3, '从契约 §3.2 抽出的列名清单'),
)

# `ast.literal_eval` 拿不到时的哨兵。用两个哨兵是为了让报错能区分
# 「这个名字根本不存在」和「存在但不是字面量」—— 后者的排查方向完全不同。
_MISSING = object()
_NO = object()


def read_text(path):
    """UTF-8（容忍 BOM）再 CRLF -> LF。

    先规范化是必须的：本仓库 `.py`/`.md` 全是 CRLF（`.gitattributes = * -text`），
    而下面的锚、以及 `ast` 报出的行号，都建立在「一行一个 \\n」上。裸 `\\n` 的正则
    在 CRLF 上会**静默失配**，那是本仓库已经踩过两次的坑。
    """
    with open(path, encoding='utf-8-sig') as fh:
        text = fh.read()
    return text.replace('\r\n', '\n')


def parse_module(text, where, issues):
    try:
        return ast.parse(text, filename=where)
    except SyntaxError as exc:
        issues.append(('A0', '%s 解析失败：第 %s 行 %s'
                       % (where, exc.lineno, exc.msg)))
        return None


def literal_value(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _NO


def module_constants(tree):
    """模块级 `NAME = <字面量>` -> {name: value}；非字面量的值为 `_NO`。"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = literal_value(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = (literal_value(node.value) if node.value is not None
                                   else _NO)
    return out


def import_roots(node):
    """一个 import 语句引入的顶层模块名（`from .enums import X` -> 'enums'）。"""
    if isinstance(node, ast.Import):
        return [alias.name.split('.')[0] for alias in node.names]
    module = node.module or ''
    if not module:
        return []
    return [module.split('.')[0]]


def imported_names(tree):
    """模块里 import 进来的名字（含别名）。A7 用它把「类型名」和「源字段名」分开。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.add(alias.asname or alias.name)
    return out


def _decorator_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ''


def dataclass_fields(tree):
    """[(类名, [字段名...])]，只取模块级 `@dataclass` 类。

    行类就是这个形状（`DailyBar` / `PITReport` / `LeakagePoint`）。刻意不做「所有类都
    扫一遍」：没有注解的普通类里 `self.x = ...` 是赋值不是字段声明，混进来只会制造噪音，
    而噪音会让人把门禁调松。
    """
    out = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_decorator_name(d).split('.')[-1] == 'dataclass'
                   for d in node.decorator_list):
            continue
        fields = [stmt.target.id for stmt in node.body
                  if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)]
        out.append((node.name, fields))
    return out


def contract_list(text, start_token, end_token):
    """从契约正文里抽一段用 `/` 分列的清单。

    返回 `(names_or_None, prose, why)`。锚不是唯一、找不到终点锚、或一个标识符都没抽到，
    都返回 `None` —— 契约措辞改了要人来看，不能让「抽不到」变成「跳过」。
    """
    first = text.find(start_token)
    if first < 0:
        return None, [], '找不到起点锚 %r' % (start_token,)
    if text.find(start_token, first + 1) >= 0:
        return None, [], '起点锚 %r 出现多次，无法确定该抽哪一处' % (start_token,)
    begin = first + len(start_token)
    end = text.find(end_token, begin)
    if end < 0:
        return None, [], '起点锚 %r 之后找不到终点锚 %r' % (start_token, end_token)
    body = text[begin:end].replace('\n', ' ')
    names, prose = [], []
    for token in body.split('/'):
        token = token.strip().strip('*').strip()
        if not token:
            continue
        (names if IDENT_RE.match(token) else prose).append(token)
    if not names:
        return None, prose, '清单里一个标识符都没抽到（正文只抽到 %r）' % (prose,)
    return names, prose, ''


def run_checks(adapter_text, rows_text, contract_text):
    """三个文本 -> (issues, stats)。返回 `issues` 为空才是 PASS。"""
    issues, stats = [], {}
    adapter_tree = parse_module(adapter_text, ADAPTER_REL, issues)
    rows_tree = parse_module(rows_text, ROWCLASS_REL, issues)
    if adapter_tree is None or rows_tree is None:
        # 解析都失败了就抽不出任何东西，后续检查无从谈起。这里**提前返回是安全的**：
        # A0 本身就是一条 FAIL，不存在「看起来绿」的窗口。反过来，如果在解析失败后
        # 继续跑，其余探测器会因为「什么都没抽到」而全部报空转，把一条真问题淹没在
        # 十条噪音里。
        issues.append(('A0', '源码解析失败，后续检查全部无从谈起 —— 这不是「没问题」，'
                             '而是「没检查」'))
        return issues, stats
    stats['scanned_modules'] = 2

    constants = module_constants(adapter_tree)

    def need(name, what):
        value = constants.get(name, _MISSING)
        if value is _MISSING:
            issues.append(('A0', '常量 %s（%s）不存在 —— 改名或挪走会让依赖它的检查静默空转'
                           % (name, what)))
            return _MISSING
        if value is _NO:
            issues.append(('A0', '常量 %s（%s）不是字面量（表达式/引用其它名字）—— '
                                 '本门禁拿不到它的值，相关检查会退化成空转'
                           % (name, what)))
            return _MISSING
        return value

    # ── 标准 schema：由四个元组拼出来 ────────────────────────────────────────────
    tuples = {}
    for name in SCHEMA_TUPLES:
        value = need(name, '标准 schema 列名元组')
        if value is _MISSING:
            continue
        if not (isinstance(value, tuple) and all(isinstance(x, str) for x in value)):
            issues.append(('A0', '常量 %s 不是「全字符串的元组」，拿不到可信的标准列名集合'
                           % name))
            continue
        tuples[name] = tuple(value)
    stats['schema_bindings'] = len(tuples)
    standard = tuple(x for name in SCHEMA_TUPLES for x in tuples.get(name, ()))
    standard_set = set(standard)

    # ── 映射表：缺表/形状不对都是 A0，不是「跳过」 ────────────────────────────────
    maps = {}
    for name in COLUMN_MAPS + VALUE_MAPS:
        value = constants.get(name, _MISSING)
        if value is _MISSING:
            issues.append(('A0', '映射表 %s 不存在 —— 它可能只是改了名，但本门禁看不到它，'
                                 '于是「源字段名不得泄漏」这条就没了守卫' % name))
            continue
        if value is _NO or not isinstance(value, dict):
            issues.append(('A0', '映射表 %s 不是字面量字典 —— 映射关系必须能被静态读出来，'
                                 '否则它只能靠运行时测试兜底' % name))
            continue
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            issues.append(('A0', '映射表 %s 里有非字符串的键或值 —— 列名映射只能是 str -> str'
                           % name))
            continue
        maps[name] = dict(value)
    stats['column_maps'] = len([n for n in COLUMN_MAPS if n in maps])
    stats['value_maps'] = len([n for n in VALUE_MAPS if n in maps])

    # ── A1 / A2：列名映射表的两条静态性质 ───────────────────────────────────────
    for name in COLUMN_MAPS:
        table = maps.get(name)
        if table is None:
            continue
        bad = sorted(set(v for v in table.values() if v not in standard_set))
        if bad:
            issues.append(('A1', '映射表 %s 的值 %s 不在标准 schema 里（标准列名共 %d 个：%s）'
                                 ' —— 映射表的**值**必须是标准列名，写反方向或写错名字都会'
                                 '让归一化产出契约之外的列'
                           % (name, bad, len(standard_set), ' / '.join(standard))))
        bad = sorted('%s -> %s' % (k, v) for k, v in table.items()
                     if k in standard_set and k != v)
        if bad:
            issues.append(('A2', '映射表 %s 把标准列名指到了别的标准列名：%s —— 源恰好同名'
                                 '（open -> open）是允许的恒等映射，把 open 指到 close 是'
                                 '实打实的数据错误' % (name, bad)))

    # ── A3：没有分类的 str->str 映射表 ──────────────────────────────────────────
    str_maps = sorted(n for n, v in constants.items()
                      if isinstance(v, dict) and v
                      and all(isinstance(k, str) for k in v)
                      and all(isinstance(x, str) for x in v.values()))
    stats['str_maps'] = len(str_maps)
    known_maps = set(COLUMN_MAPS) | set(VALUE_MAPS)
    for name in str_maps:
        if name not in known_maps:
            issues.append(('A3', '模块级 str->str 映射表 %s 没有被本门禁分类（既不在 '
                                 'COLUMN_MAPS 也不在 VALUE_MAPS）—— 未分类的表不会被任何'
                                 '检查看到，而它很可能正是新数据源赖以归一化的那张表' % name))

    # ── A4：取值映射表的输出必须闭在 report_type 枚举里 ─────────────────────────
    report_types = need(REPORT_TYPES_CONST, '契约 §3.6.1 的 ck_dc_fin_report_type 取值域')
    if report_types is not _MISSING:
        if not (isinstance(report_types, tuple) and all(isinstance(x, str) for x in report_types)):
            issues.append(('A0', '常量 %s 不是「全字符串的元组」，拿不到可信的取值域'
                           % REPORT_TYPES_CONST))
        else:
            allowed = set(report_types)
            for name in VALUE_MAPS:
                table = maps.get(name)
                if table is None:
                    continue
                bad = sorted(set(v for v in table.values() if v not in allowed))
                if bad:
                    issues.append(('A4', '取值映射表 %s 会产出 %s，不在 %s 里 —— 漏了一个'
                                         '取值，那条 CHECK 就会在**数据库**那一层才报错，'
                                         '那时数据已经走到写库门口了'
                                   % (name, bad, list(report_types))))

    # ── A5 / A6：import 的位置和种类 ────────────────────────────────────────────
    # 顶层 import 用节点 id 精确判定：`ast.walk` 会把模块级 import 也走一遍，靠
    # `node.col_offset == 0` 之类的启发式区分会在缩进异常的源码上判错。
    top_ids = set(id(n) for n in adapter_tree.body
                  if isinstance(n, (ast.Import, ast.ImportFrom)))
    top_roots, lazy_roots = [], []
    for node in ast.walk(adapter_tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for root in import_roots(node):
            (top_roots if id(node) in top_ids else lazy_roots).append(root)
    stats['module_imports'] = len(top_roots)
    stats['lazy_source_imports'] = len([r for r in lazy_roots if r in SOURCE_SDK_MODULES])

    for root in sorted(set(r for r in top_roots + lazy_roots if r in DB_DRIVER_MODULES)):
        issues.append(('A5', '适配器文件里出现数据库驱动 import `%s` —— D1/D10 说采集侧不写库，'
                             '写库是 DataCenter 的事。惰性导入也算：把驱动藏进函数体并不'
                             '改变「谁在写库」' % root))
    for root in sorted(set(r for r in top_roots if r in SOURCE_SDK_MODULES)):
        issues.append(('A6', '源 SDK `%s` 是**模块级**导入 —— 源库缺失时必须由 `_call` 翻译成 '
                             'SourceAdapterError（DATA_005），模块级导入会让「没装 %s」升级成'
                             '「import quanauto.datasources 直接失败」，报错地点从数据源'
                             '挪到了包导入' % (root, root)))

    # ── A7：行类上不得出现源特有字段名 ──────────────────────────────────────────
    denylist = set()
    for name in COLUMN_MAPS:
        denylist |= set(maps.get(name, {}))
    markers = constants.get(DROPPED_MARKERS_CONST, _MISSING)
    if isinstance(markers, tuple):
        denylist |= set(m for m in markers if isinstance(m, str))
    denylist = sorted(denylist - standard_set - imported_names(rows_tree))
    stats['denylist_names'] = len(denylist)
    deny_set = set(denylist)
    classes = dataclass_fields(rows_tree)
    stats['row_classes'] = len(classes)
    for cls, fields in classes:
        hit = sorted(f for f in fields if f in deny_set)
        if hit:
            issues.append(('A7', '数据类 %s 上出现源特有字段名 %s —— D9 要求上层不感知数据源'
                                 '差异，源字段名在适配器那一层就该被换掉。黑名单是反推的'
                                 '（映射表键域 ∪ 被丢弃的源列名，共 %d 个：%s）'
                           % (cls, hit, len(denylist), ' / '.join(denylist))))

    # ── A8：抄来的列名必须与契约 §3.2 一致 ──────────────────────────────────────
    extracted = 0
    for label, const_name, start_token, end_token in CONTRACT_SCHEMA_SPECS:
        names, prose, why = contract_list(contract_text, start_token, end_token)
        if names is None:
            issues.append(('A8', '从契约 §3.2 抽 %s 的列名清单失败：%s —— 抽不到就必须报错，'
                                 '不能变成「这一段不检查了」' % (label, why)))
            continue
        extracted += 1
        stats.setdefault('contract_prose', []).extend(prose)
        want = tuples.get(const_name)
        if want is None:
            continue
        missing = sorted(set(want) - set(names))
        extra = sorted(set(names) - set(want))
        if missing or extra:
            issues.append(('A8', '契约 §3.2 的 %s 列名与 %s 不一致：契约缺 %s / 契约多 %s'
                           ' —— 两处必须同批改，否则适配器与契约就是各说各话'
                           % (label, const_name, missing, extra)))
    stats['contract_schemas'] = extracted

    # ── A9：没登记的标准 schema 元组 ────────────────────────────────────────────
    for name, value in sorted(constants.items()):
        if not name.endswith('_COLUMNS') or name in SCHEMA_TUPLES:
            continue
        if not (isinstance(value, tuple) and value
                and all(isinstance(x, str) for x in value)):
            continue
        issues.append(('A9', '模块级 `_COLUMNS` 元组 %s 没登记进 SCHEMA_TUPLES —— 未登记的'
                             ' schema 不参与「映射表值域 ⊆ 标准 schema」的判定，'
                             '于是那张表的映射值既不算对也不算错' % name))

    # ── A10：非空转 ─────────────────────────────────────────────────────────────
    for key, minimum, what in MIN_STATS:
        got = stats.get(key, 0)
        if got < minimum:
            issues.append(('A10', '提取到的 %s = %d，低于下限 %d（%s）—— 提取为空或失配时'
                                  '上面每个探测项都在空转，却会打印「0 issue(s) PASS」。'
                                  '零目标是 FAILURE，不是通过'
                           % (key, got, minimum, what)))
    return issues, stats


def _mutate(text, old, new, tag):
    """唯一锚点 + 自断言的替换（与 data-center-pit 同一套纪律）。

    锚不存在／不唯一／替换是 no-op 都返回 `None`，调用方必须当成失败。静默 no-op 是
    最经典的假门禁：变异样本与原文件逐字节相同，探测器当然不响，而「不响」被读成了
    「探测器有效」。
    """
    count = text.count(old)
    if count != 1:
        print('    sample %s: ANCHOR %s (%d occurrence(s), need exactly 1) %r'
              % (tag, 'AMBIGUOUS' if count else 'NOT FOUND', count, old[:60]))
        return None
    line = text[:text.index(old)].count('\n') + 1
    out = text.replace(old, new, 1)
    if out == text:
        print('    sample %s: NO-OP (replacement is byte-identical to the anchor)' % tag)
        return None
    print('    applied: %s (line %d)' % (tag, line))
    return out


# 合成一套「干净」的三件套：形状与真货同构（常量名、映射表分类、惰性导入），
# 但内容与真货无关。POSITIVE 样本靠它证明「不会永远报红」—— 没有这个样本，
# 一个「任何输入都报错」的探测器在上面每个 NEG 里都会显得完美。
CLEAN_ADAPTER = """\
import re


DAILY_BAR_COLUMNS = ('symbol', 'trade_date', 'open', 'high', 'low', 'close',
                     'volume', 'amount')
FINANCIAL_REQUIRED_COLUMNS = ('symbol', 'report_type', 'period_end', 'announce_date')
FINANCIAL_SUBJECT_COLUMNS = ('revenue',)
INDEX_MEMBER_COLUMNS = ('index_code', 'symbol', 'effective_from', 'effective_to',
                        'weight')
STANDARD_COLUMNS = (DAILY_BAR_COLUMNS + FINANCIAL_REQUIRED_COLUMNS
                    + FINANCIAL_SUBJECT_COLUMNS + INDEX_MEMBER_COLUMNS)
REPORT_TYPES = ('BALANCE', 'INCOME')

AKSHARE_DAILY_BAR = {
    'date': 'trade_date',
    'close': 'close',
}
BAOSTOCK_DAILY_BAR = {
    'volume': 'volume',
}
EASTMONEY_FINANCIAL = {
    'SECURITY_CODE': 'symbol',
}
EASTMONEY_INDEX_MEMBER = {
    'WEIGHT': 'weight',
}
EASTMONEY_REPORT_TYPE = {
    '利润表': 'INCOME',
}


class Adapter:
    def fetch(self):
        import akshare
        return akshare
"""

CLEAN_ROWS = """\
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: date
    close: float
    volume: float
    source: str
"""

# A8 判据要求这三段能对上 CLEAN_ADAPTER 里的三个元组，所以列名清单与常量逐字一致。
CLEAN_CONTRACT = """\
            pd.DataFrame: 列名必须是标准 schema —— symbol / trade_date / open / high /
                low / close / volume / amount，且 trade_date 为 date 类型。

            pd.DataFrame: 必须包含列 —— symbol / report_type / period_end /
                **announce_date**（缺失该列即视为适配器不合格，见 D4）/ 各财务科目。

            pd.DataFrame: 列名 —— index_code / symbol / effective_from / effective_to
                / weight。effective_to 可为空表示仍在成分内。
"""


def selftest():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(root, rel) for rel in SOURCE_RELS]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        print('SELFTEST FAIL: missing input(s) %s' % ', '.join(missing))
        return 1
    adapter, rows, contract = [read_text(p) for p in paths]

    ok = True

    def scenario(tag, adapter_text, rows_text, contract_text, code, want_clean=False):
        nonlocal ok
        issues, _stats = run_checks(adapter_text, rows_text, contract_text)
        codes = sorted(set(k for k, _ in issues))
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s %s'
              % (tag, len(issues), codes, 'OK' if hit else 'MISSED'))
        if not hit:
            for c, m in issues:
                print('      %s: %s' % (c, m[:140]))
        ok = ok and hit

    # CONTROL：真实产物。只报告，不断言它是干净的 —— 门禁允许在真产物上变红，
    # 那正是「能跑起来」的全部意义。
    issues, stats = run_checks(adapter, rows, contract)
    print('  [control-real-artifacts] issues=%d codes=%s %s'
          % (len(issues), sorted(set(k for k, _ in issues)),
             ' '.join('%s=%d' % (k, stats.get(k, -1)) for k in STAT_KEYS)))

    # A0a：源码语法错。锚是真货里的导入块，替换成一句语法上打不开的东西。
    bad = _mutate(adapter,
                  'from .enums import SourcePriority\n',
                  'from .enums import SourcePriority(\n',
                  'NEG0a-syntax-error')
    if bad is None:
        ok = False
    else:
        scenario('NEG0a-syntax-error', bad, rows, contract, 'A0')

    # A0b：常量被删。删 REPORT_TYPES 会让 A4 失去取值域 —— 它必须变成 A0 而不是静默跳过。
    bad = _mutate(adapter,
                  "REPORT_TYPES = ('BALANCE', 'INCOME', 'CASHFLOW', 'INDICATOR')\n",
                  '# MUT: REPORT_TYPES deleted\n',
                  'NEG0b-constant-deleted')
    if bad is None:
        ok = False
    else:
        scenario('NEG0b-constant-deleted', bad, rows, contract, 'A0')

    # A1：映射表的值写成了标准 schema 之外的列名。
    bad = _mutate(adapter,
                  "    '成交额': 'amount',\n",
                  "    '成交额': 'amount_typo',\n",
                  'NEG1-value-outside-schema')
    if bad is None:
        ok = False
    else:
        scenario('NEG1-value-outside-schema', bad, rows, contract, 'A1')

    # A2：标准列名被指到了别的标准列名（源自己同名时的恒等映射被打散）。
    bad = _mutate(adapter,
                  "    'open': 'open',\n    'high': 'high',\n    'low': 'low',\n",
                  "    'open': 'close',\n    'high': 'high',\n    'low': 'low',\n",
                  'NEG2-standard-key-not-identity')
    if bad is None:
        ok = False
    else:
        scenario('NEG2-standard-key-not-identity', bad, rows, contract, 'A2')

    # A3：新加一张列名映射表但忘了登记 —— 它会被 A1/A2 完全无视。
    bad = _mutate(adapter,
                  "EXCHANGES = ('SH', 'SZ', 'BJ')\n",
                  "NEW_SRC_MAP = {\n    'x': 'trade_date',\n}\n"
                  "EXCHANGES = ('SH', 'SZ', 'BJ')\n",
                  'NEG3-unclassified-map')
    if bad is None:
        ok = False
    else:
        scenario('NEG3-unclassified-map', bad, rows, contract, 'A3')

    # A4：取值映射表产出了报告类型枚举之外的取值（写库才报错的经典来源）。
    bad = _mutate(adapter,
                  "EASTMONEY_REPORT_TYPE = {\n    '资产负债表': 'BALANCE',\n",
                  "EASTMONEY_REPORT_TYPE = {\n    '资产负债表': 'BALANCE_SHEET',\n",
                  'NEG4-report-type-outside-enum')
    if bad is None:
        ok = False
    else:
        scenario('NEG4-report-type-outside-enum', bad, rows, contract, 'A4')

    # A5：采集侧 import 了数据库驱动。
    bad = _mutate(adapter,
                  'import pandas as pd\n\nfrom .enums import SourcePriority\n',
                  'import pandas as pd\nimport psycopg\n\nfrom .enums import SourcePriority\n',
                  'NEG5-db-driver-imported')
    if bad is None:
        ok = False
    else:
        scenario('NEG5-db-driver-imported', bad, rows, contract, 'A5')

    # A6：源 SDK 从函数体搬到了模块顶层。
    bad = _mutate(adapter,
                  'import pandas as pd\n\nfrom .enums import SourcePriority\n',
                  'import akshare\nimport pandas as pd\n\n'
                  'from .enums import SourcePriority\n',
                  'NEG6-source-import-eager')
    if bad is None:
        ok = False
    else:
        scenario('NEG6-source-import-eager', bad, rows, contract, 'A6')

    # A7：行类上长出一个源特有的字段名（baostock 的停牌标记）。
    bad = _mutate(rows,
                  '    amount: float\n    source: str\n    data_version: str\n',
                  '    amount: float\n    tradestatus: int\n    source: str\n'
                  '    data_version: str\n',
                  'NEG7-source-field-on-row-class')
    if bad is None:
        ok = False
    else:
        scenario('NEG7-source-field-on-row-class', adapter, bad, contract, 'A7')

    # A8：契约与实现漂移（契约少了 amount 一列）。
    bad = _mutate(contract,
                  'low / close / volume / amount，且 trade_date 为 date 类型。\n',
                  'low / close / volume，且 trade_date 为 date 类型。\n',
                  'NEG8-contract-drift')
    if bad is None:
        ok = False
    else:
        scenario('NEG8-contract-drift', adapter, rows, bad, 'A8')

    # A8 的空转守卫：契约里三个锚一个都找不到。抽不到就必须红，
    # 而不是「这一段跳过」—— 那样契约改版时门禁会安静地少检查三件事。
    scenario('NEG8b-contract-anchors-gone', adapter, rows, 'X = 1\n', 'A8')

    # A9：新加一个标准 schema 元组但没登记 —— 它的列名不参与值域判定。
    bad = _mutate(adapter,
                  'PRICE_DECIMALS = 4\n',
                  "TURNOVER_COLUMNS = ('turn_rate',)\nPRICE_DECIMALS = 4\n",
                  'NEG9-unclassified-schema-tuple')
    if bad is None:
        ok = False
    else:
        scenario('NEG9-unclassified-schema-tuple', bad, rows, contract, 'A9')

    # A10：常量都在、语法也对，但全是空的 —— 所有探测器都在空转。
    # 这一条与 A0 的区别是关键：A0 是「抽不到」，A10 是「抽到了，但是零个目标」。
    empty_adapter = ("DAILY_BAR_COLUMNS = ()\nFINANCIAL_REQUIRED_COLUMNS = ()\n"
                     "FINANCIAL_SUBJECT_COLUMNS = ()\nINDEX_MEMBER_COLUMNS = ()\n"
                     "REPORT_TYPES = ()\nAKSHARE_DAILY_BAR = {}\n"
                     "BAOSTOCK_DAILY_BAR = {}\nEASTMONEY_FINANCIAL = {}\n"
                     "EASTMONEY_INDEX_MEMBER = {}\nEASTMONEY_REPORT_TYPE = {}\n")
    scenario('NEG10-extraction-empty', empty_adapter, rows, contract, 'A10')

    # POSITIVE：合成三件套必须一条问题都没有。没有这个样本，
    # 「永远报红」的探测器和「有效」的探测器在上面的 NEG 里长得一模一样。
    scenario('POSITIVE-clean-synthetic', CLEAN_ADAPTER, CLEAN_ROWS, CLEAN_CONTRACT, None,
             want_clean=True)

    print('SELFTEST %s' % ('OK: every detector fires on its own sample, two extraction '
                           'guards (A0/A10) and one clean sample stays clean'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if '--selftest' in sys.argv:
        return selftest()

    paths = [os.path.join(root, rel) for rel in SOURCE_RELS]
    for path in paths:
        if not os.path.exists(path):
            print('GATE FAIL: missing input %s' % path)
            return 1

    texts = [read_text(p) for p in paths]
    print('CRLF-normalised: ' + ', '.join(
        '%s=%d bytes' % (rel, len(text.encode('utf-8')))
        for rel, text in zip(SOURCE_RELS, texts)))

    issues, stats = run_checks(*texts)
    print('extracted: ' + ' '.join('%s=%d' % (k, stats.get(k, -1)) for k in STAT_KEYS))
    if stats.get('contract_prose'):
        print('contract-prose: ' + ' '.join(stats['contract_prose']))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: 这个门禁只解析源码、从不执行、不导入 pandas、不跑 pytest。它证明的是'
          '「结构上没被绕过」，**不是**「映射表的内容是对的」—— 源列名是从各源文档抄来的，'
          '从未联网核对（akshare/baostock 都没装），那条欠账登记在数据中心契约附录 B。'
          '运行时行为由 tests/test_data_center_adapter.py 负责。')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
