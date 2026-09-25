#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""class-coverage —— 「实现侧每一个类都得有契约来源或显式登记」这条判据。

**为什么需要它（B9.2 + B10 的还债方式）**：到 2026-09-25 为止，本仓库**没有**任何判据
看得见「类的清单」。`contract-signature` 只查**登记过的成员还在不在**（新类它看不见），
`contract-appendix` 只审主契约**附录 G 节**，`enum-members` 只管**枚举成员**。
2026-09-25 实测：`quanauto/` 的 144 个类里 **29 个**既没有契约声明、也不在
`manifest.classes` / `impl_only` / `CONTRACT_FIRST` 里，当时的 13 个门禁照样全绿
（14 个时也一样 —— `enum-members` 管不到类清单）。

**判据（两句话）**：`quanauto/*.py` 里的每一个类，必须落在下面**四个**集合中的一个
（前两个是自动的，后两个是人工登记且**反向自查**）：

  ① `DECLARED`    —— 三份契约的 ```python 块里有 `class 同名`（`ast` 读，不是文本搜）；
  ② `REGISTERED`  —— `manifest.classes` ∪ `manifest.impl_only` ∪ `CONTRACT_FIRST` ∪ `T1_REQUIRED`
                     （后两个用 `ast.literal_eval` 从 `verify_contract_appendix.py` 读常量，
                     **不 import** —— import 就是执行）；
  ③ `DEBT`        —— 契约**点名过它的类名/把它当类型用**，但正文没有类块 ⇒ 显式欠账；
  ④ `LOCAL`       —— 契约**不定义**它（实现侧细节，或契约自己已裁决「契约里没有它」）。

**「登记」不是「合规」**：③④ 两张表**都不是合规声明**，只是把 29 个缺口从「谁也看不见」
变成「每次运行都印出来」。DEBT 是其中最硬的一类（契约点了名却仍然没有形状），
本门禁**不把它判红**（补齐异常层次是 `gates-baseline.json` 里那笔 T3=18 的活），
但每次运行都会印出它的条数。

**为什么登记不放进 `manifest.impl_only`**：那份清单按它自己的 `note` 是**签名清单**
（盯成员/字段的漂移），而这里要登记的是「它为什么没有契约来源」，还要反向自查
（登记项一旦被契约声明覆盖就必须删）。两者目的不同，混在一起会让 `impl_only`
变成一张什么都记的表。先例：`enum-members` 的 `IMPL_LOCAL` / `PENDING` 也是这个理由。

**「提及」与「声明」分开数（B10 的第二条要求）**：契约散文里出现一个类名**不算**覆盖。
本门禁为此做了三件事：① 声明侧只认 `ast` 解析出来的 `ClassDef`，不认文本；
② 报告里单独印「登记表里的名字，契约散文提到过几个 / 连名字都没有几个」；
③ `CC-UNCOVERED` 的消息里带上提及次数并写明「**提及不算覆盖**」——
把「用一句『正文提过』把缺口抹掉」这条路径在报告里点名。

**边界（它证明不了什么）**：
  * 只判**方向**：实现侧 ⇒ 必须有来源。反向（契约声明了但实现里没有）由
    `contract-signature`（成员级）与 `contract-appendix`（附录 G）管，这里只**印**计数。
    这条单向性对「变薄」是 fail-closed 的：契约块被删、`manifest` 被删、`impl_only`
    被删，都会让对应的类**失去覆盖** ⇒ 立刻红。
  * 只数**类**（`ClassDef`）。`NewType(...)` 是赋值不是类 —— B9.2 那次「145 个类」的
    计数更正就是把它折进去了（现值 **144 类 + 1 个 NewType 别名**）。两个数都印出来。
  * 不判「某个类**该不该**有契约块」。`StrategyHandle` 该补契约块还是该改返回值，
    是人的裁决；这个扫描器只保证「两侧不一致时一定有人知道」。
  * 不比成员/字段/方法。那是 `contract-signature` / `contract-appendix` 的活。
  * 扫描范围固定 `quanauto/*.py`（文件系统，不用 git 清单 —— 那会随提交漂移）。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
IMPL_REL = 'quanauto'

CONTRACTS = (
    ('core', os.path.join('docs', '智能量化交易平台-核心模块接口契约文档.md')),
    ('dc', os.path.join('docs', '智能量化交易平台-数据中心接口契约文档.md')),
    ('risk', os.path.join('docs', '智能量化交易平台-风控层接口契约文档.md')),
)

MANIFEST_REL = os.path.join('tools', 'contract-signature-manifest.json')
APPENDIX_REL = os.path.join('tools', 'verify_contract_appendix.py')

PY_BLOCK_RE = re.compile(r'```python\r?\n(.*?)```', re.S)
# 只在**解析不了的块**上用，用来找出「这块里提到了哪些类」。故意**不锚定行首**：
# .docx 损坏的典型形状是把 `class X:` 挤到行中间（实测 `@dataclass class Account:
# account_id: str …`），锚定行首会静默漏掉这个类 —— 而「漏掉」正是本门禁要防的事。
# 宁可多报（多报的下场是「必须登记进 DAMAGED」，是 fail-closed 的）。
CLASS_HINT_RE = re.compile(r'\bclass[ \t]+(\w+)')

# ── 登记表 ③：DEBT —— 契约点过名/把它当类型用，正文却没有类块 ─────────────
# 表里每一条都写清了**契约在哪用了这个名字**（引原文片段，不猜章节号）。
# 这不是「合规」，是显式未闭环：它每次都会在报告里被数出来。
DEBT = {
    'OrderRejectedError':
        '风控契约正文（「拦截方式」段）把它当类型用：`撮合器自己拒的单'
        '（OrderRejectedError）走同一本账`；核心契约附录 D 只给了码 `BROKER_002`，'
        '没有类块 ⇒ 码与类名之间没有任何判据',
    'RiskConfigInvalidError':
        '风控契约 §D4 的裁决段（`抛 RiskConfigInvalidError（RISK_004）`）与至少 4 处 '
        '`Raises:` 段都点名它，正文却从不声明异常层次 —— 与 `gates-baseline.json` '
        '记着的那笔 T3 = 18 是同源欠账',
    'RiskConfigLoadError':
        '风控契约多处 `Raises: RiskConfigLoadError: 存储层不可达时抛出。` 点名它，'
        '没有类块',
    'RiskConfigWriteError':
        '风控契约 `Raises: RiskConfigWriteError: 写入失败时抛出。` 与 §7.2 偏离表的'
        '「失败方向」都点名它，没有类块',
    'RiskInterceptError':
        '风控契约引擎方法的 `Raises:` 段用它（§7.3 自己也这么表述）；§7.3 另记了它的'
        '**第二个用途**（留痕写入方）属于实现侧 —— 但类本身在契约里仍然只有名字、没有形状',
    'RiskRuleNotFoundError':
        '风控契约 `Raises: RiskRuleNotFoundError: GLOBAL 层缺失该规则时抛出。` 点名它；'
        '实现的 `code` 继承 `RiskConfigInvalidError`（RISK_004）⇒ 契约没有类，'
        '就没有「它该不该有自己的码」这个说法',
    'StrategyHandle':
        '核心契约里 `add_strategy` 的返回值说明把它当**返回类型**用'
        '（`返回值：StrategyHandle — 策略句柄（含策略ID和初始资金）`），却没有类块；'
        '`contract-signature` 明确**不比返回类型** ⇒ 它正好落在两条判据的缝里',
}

# ── 登记表 ④：LOCAL —— 契约不定义这个类 ────────────────────────────────
# 两种形状混在一条表里，报告会分开数（契约散文提到过 / 连名字都没有）：
#   * 「契约用**码表/表/读数口径**定义，不用类块」——异常族、规则注册表、看板口径；
#   * 「契约自己的登记段已裁决『契约里没有它』」——`DATA_008`、§7.3。
LOCAL = {
    # —— 契约散文提到过名字（7 个）——
    'DataStoreError':
        '数据中心契约附录 C 已**裁决**它属实现侧新增（码 `DATA_008`）—— 契约 §3.9 的'
        '错误码表里没有「存储层失败」这一档；正文提到它的地方是附录 B 的返债/缺陷记录',
    'IngestReport':
        '数据中心契约正文只在附录 B 的**证据读数**里出现它（一次性落库的统计），'
        '不是契约形状；契约规定的是标准 schema 与 `DataCenter` 的取数语义',
    'PgBarIngestor':
        '数据中心契约正文只在附录 B 的链路记录里出现它；存储层类形状不在契约里'
        '（`BarStore` / `InMemoryBarStore` / `DailyBar` 同样按 `manifest.impl_only` 登记）',
    'PgBarStore':
        '同 `PgBarIngestor`：附录 B 的链路记录里出现，存储层形状不在契约里',
    'PsycopgConnection':
        '数据中心契约正文只在附录 B 的**缺陷根因**（包装层吃掉 `__cause__` 上的 '
        'sqlstate）段落里出现它；驱动连接类是存储层实现细节',
    'RiskError':
        '风控契约只在 §7.4 的缺陷记录里出现它（`放行判据…收窄为「是风控族 RiskError」`）；'
        '§3.7 的码表从 `RISK_001` 起，`RISK_000`（族的共同根）是实现侧为了**按族划'
        '放行边界**而引入的，不是契约概念',
    'RiskInterceptLogWriter':
        '风控契约 §7.3 明确登记为「实现侧新增的名字（契约里没有，别拿契约去找）」',
    # —— 三份契约连名字都没有（15 个）——
    'BrokerError':
        '核心契约附录 D 只给码（`BROKER_001`）；类名与继承层次是实现侧命名 ——'
        '`BrokerError` / `InsufficientFundsError` 同码，说明类名不是契约概念',
    'InsufficientFundsError':
        '同 `BrokerError`（码 `BROKER_001`）；核心契约附录 C 只要求「资金校验测试」，'
        '不要求这个类',
    'CurvePoint':
        '看板内部视图对象；核心契约附录 F2/F3 管的是**读数口径**（只读数不算数、'
        '回撤取正号），不规定视图类形状',
    'CurveStats':
        '同 `CurvePoint`（资金曲线下方的 min/max/回撤文字，纯显示层）',
    'DashboardView':
        '看板内部聚合对象（附录 F1：看板是纯读数组件），契约不规定它的形状',
    'DashboardError':
        '核心契约附录 F4 **刻意**只按错误码引用而不写类名 —— 原文自己写了这条取舍'
        '（免得给 T3 那笔欠账添一笔）。⚠️ 但码的拼写对不上：附录 F4 写 `DASHBOARD001`，'
        '`quanauto/errors.py` 是 `DASHBOARD_001` ⇒ 已作为独立缺口登记在 '
        '`docs/开工前缺口清单.md` 的「七之二 · 本次探针新登记的缺口」第 3 条，'
        '不在本表里抹掉',
    'MetricRead':
        '看板内部读数对象；14 项字段口径由门禁 `dashboard-consistency` 的 C2'
        '（双向比 `PerformanceMetrics` 的字段表）守着，比类块细',
    'MetricSpec':
        '同 `MetricRead`（看板内部指标规格 `METRIC_SPECS`）',
    'KillSwitchActiveError':
        '风控契约 §3.7 的码表给了码 `RISK_007`，正文没有任何一处写这个类名'
        '⇒ 类是码的实现侧命名',
    'RiskBreakerTrippedError':
        '同 `KillSwitchActiveError`：码 `RISK_002`（出自核心契约附录 D），'
        '契约正文没有这个类名',
    'RiskDegradedError':
        '同 `KillSwitchActiveError`：码 `RISK_006`，契约正文没有这个类名',
    'RiskRuleVersionConflictError':
        '同 `KillSwitchActiveError`：码 `RISK_010`（§3.7 码表最后一行），'
        '契约正文没有这个类名',
    'RuleSpec':
        '风控契约 §3.1.1 用**表**给出规则注册表（`rule_id` / `rule_type` / `unit` / '
        '`tightening_direction` / `default_threshold`）；六个全局规则的 id/单位/方向/'
        '默认值由 `verify_risk_config.py` 的 C16 逐条盯着（比类块细）',
    'SeverityEnum':
        '风控契约 §3.2.6 与 proto 注释把 `severity` 写成 `str`（WARNING / ERROR / '
        'CRITICAL），没有枚举块；同一裁决也登记在门禁 `enum-members` 的 `IMPL_LOCAL` 里'
        '（那份盯成员，这份盯它没有契约来源）',
    'SqlConnection':
        '存储层连接抽象（协议），与 `PsycopgConnection` 成对；契约没有存储层这一层',
}

# 提取为空 / 提取变薄 ⇒ 拒绝通过。实测值（2026-09-25）：契约声明 132、登记侧 54、
# 实现侧 144 个类。下限留足余量，只在「提取器失配」时才该响。
MIN_DECLARED = 80
MIN_REGISTERED = 25
MIN_IMPL = 100

# ── 已知损坏的块（登记表，键 = (契约 tag, 块里出现的类名)）─────────────────
# 主契约是 `.docx` 转换产物、只许追加 ⇒ 损坏的块**不能原地修**（改了会被重新转换
# 抹掉）。不登记就等于让门禁在绿仓库上长期报红；无条件放宽则等于给损坏开后门。
# 所以做成有牙的豁免：**块里出现的类名全部登记过**才算豁免（出现一个新类名照旧
# 报 CC-UNPARSABLE-CLASS）；反向，登记项在坏块里找不到、或这个类已经能从块里
# 解析出来 ⇒ CC-DAMAGED-STALE（豁免作废，别留着当永久洞）。
DAMAGED = {
    ('core', 'Account'):
        '核心契约的块是 .docx 转换损坏的（首行实测为 `@dataclass class Account: '
        'account_id: str # 账户ID …` —— 类声明与字段挤在一行，类名根本不在行首）'
        '；原文在 T1 表里已有可复制副本，这个类也登记在 T1_REQUIRED',
    ('core', 'ResultSerializer'):
        '核心契约 §2.8.1 的块是 .docx 转换损坏的（`class ResultSerializer: """…"""` '
        '与后面的方法挤在无缩进的几行里 ⇒ 三引号未闭合）。原文在 T1 表里已有可复制'
        '副本；这个类同时登记在 CONTRACT_FIRST / T1_REQUIRED',
}


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


# ── 提取：类与 NewType ───────────────────────────────────────────────
def classes_of(src, line_offset):
    """一块源码 → ({类名: 行号}, NewType 别名数, [问题])。

    `NewType` 是 `Assign` 不是 `ClassDef` —— 单独数出来，免得再犯 B9.2 那次
    「145 个类」（把 `OrderId = NewType(...)` 折进去了，真值 144）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {}, 0, ['第 %d 行语法错：%s'
                       % (line_offset + (exc.lineno or 1), exc.msg)]
    found = {}
    newtypes = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            found.setdefault(node.name, line_offset + node.lineno)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == 'NewType'):
            newtypes += 1
    return found, newtypes, []


def blocks_of(text):
    """全文的 ```python 块 → [(源码, 文档内 fence 行号)]。"""
    out = []
    for match in PY_BLOCK_RE.finditer(text):
        out.append((match.group(1), text.count('\n', 0, match.start()) + 1))
    return out


def scan_contracts(root, contracts):
    """读三份契约 → (declared, prose, summary, findings)。"""
    findings = []
    summary = {'blocks': 0, 'unparsable': 0, 'class_hint': 0, 'newtypes': 0,
               'per_doc': {}, 'prose_chars': {}, 'damaged': []}
    declared = {}
    prose = {}
    for tag, rel in contracts:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            findings.append(('CC-NO-CONTRACT',
                             '找不到契约 %s（%s）—— 少读一份契约会让它声明的类全部'
                             '失去覆盖，拒绝通过' % (tag, rel)))
            summary['per_doc'][tag] = None
            continue
        text = read_text(path)
        blocks = blocks_of(text)
        summary['blocks'] += len(blocks)
        if not blocks:
            findings.append(('CC-NO-BLOCKS',
                             '契约 %s（%s）里一个 ```python 块都没有 ⇒ 声明侧提取为空，'
                             '比对形同虚设' % (tag, rel)))
            summary['per_doc'][tag] = []
            continue
        plain = PY_BLOCK_RE.sub('', text)
        prose[tag] = plain
        summary['prose_chars'][tag] = len(plain)
        if not plain.strip():
            findings.append(('CC-NO-PROSE',
                             '契约 %s 去掉 ```python 块之后一字散文都没有 ⇒ '
                             '「提及 vs 声明」这条核心区分算不出来，拒绝通过' % tag))
        names = []
        for src, line in blocks:
            found, newtypes, problems = classes_of(src, line)
            summary['newtypes'] += newtypes
            if problems:
                summary['unparsable'] += 1
                hints = sorted(set(CLASS_HINT_RE.findall(src)))
                head = next((ln.strip() for ln in src.split('\n') if ln.strip()), '')
                summary['damaged'].append(
                    {'tag': tag, 'line': line, 'hints': hints,
                     'head': head[:70], 'why': '; '.join(problems)})
                if hints:
                    summary['class_hint'] += 1
                continue
            for name, where in sorted(found.items()):
                names.append(name)
                declared.setdefault(name, []).append(
                    {'contract': tag, 'where': '%s:%d' % (rel, where)})
        summary['per_doc'][tag] = names
    return declared, prose, summary, findings


def scan_impl(root, impl_rel):
    """扫 `quanauto/*.py` → ({类名: [record]}, 文件数, NewType 数, [问题])。"""
    findings = []
    directory = os.path.join(root, impl_rel)
    if not os.path.isdir(directory):
        return {}, 0, 0, [('CC-NO-IMPL-DIR', '%s/ 不存在 ⇒ 没有可比对象' % impl_rel)]
    impl = {}
    files = 0
    newtypes = 0
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith('.py'):
            continue
        files += 1
        # 文件首行就是第 1 行 ⇒ 行号偏移 0（契约块用的是 fence 行号）
        found, count, problems = classes_of(
            read_text(os.path.join(directory, fname)), 0)
        newtypes += count
        for problem in problems:
            findings.append(('CC-IMPL-SCAN', '%s/%s %s' % (impl_rel, fname, problem)))
        for name, line in found.items():
            impl.setdefault(name, []).append(
                {'source': '%s/%s:%d' % (impl_rel, fname, line)})
    return impl, files, newtypes, findings


def literal_names(path, wanted):
    """从另一个工具里用 `ast` 读顶层常量的名字 —— **不 import**。

    2026-09-24 的教训：`importlib` 加载被测脚本＝**执行**它；那个脚本结尾是裸的
    `main()`，于是「读一个常量」在真仓库里跑了一次真实清理。
    """
    try:
        tree = ast.parse(read_text(path))
    except (OSError, SyntaxError):
        return None
    wanted = set(wanted)
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in wanted:
                continue
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if isinstance(value, dict):
                found[target.id] = sorted(value)
            elif isinstance(value, (list, tuple, set)):
                found[target.id] = sorted(value)
            # 只有**找到**的常量才进 found：空容器（`CONTRACT_FIRST = {}`）与
            # 「压根没这个常量」必须能分开，否则一次正当的清空会被误报成提取失配。
    return found


def scan_registry(root, manifest_rel, appendix_rel, min_registered):
    """读登记侧四个来源 → ({名字: [来源]}, summary, findings)。"""
    findings = []
    summary = {'manifest': {}, 'sources': {}, 'union': 0, 'from_appendix': {}}
    registry = {}

    def add(name, source):
        registry.setdefault(name, [])
        if source not in registry[name]:
            registry[name].append(source)

    path = os.path.join(root, manifest_rel)
    if not os.path.isfile(path):
        findings.append(('CC-NO-MANIFEST',
                         '找不到 %s —— 登记侧少一份来源会让它名下的类整体变成'
                         '「没人管」，拒绝通过' % manifest_rel))
    else:
        try:
            data = json.loads(read_text(path))
        except ValueError as exc:
            findings.append(('CC-NO-MANIFEST',
                             '%s 不是合法 JSON（%s）' % (manifest_rel, exc)))
            data = None
        if data is not None:
            for key in ('classes', 'impl_only'):
                value = data.get(key)
                if not isinstance(value, dict):
                    findings.append(('CC-MANIFEST-BAD',
                                     '%s 的 `%s` 不是对象（实际 %s）⇒ '
                                     '登记侧提取不到名字'
                                     % (manifest_rel, key, type(value).__name__)))
                    continue
                summary['manifest'][key] = len(value)
                for name in value:
                    add(name, key)

    apath = os.path.join(root, appendix_rel)
    if not os.path.isfile(apath):
        findings.append(('CC-NO-APPENDIX',
                         '找不到 %s —— 它里面的 `CONTRACT_FIRST` / `T1_REQUIRED` '
                         '是登记侧的两份来源' % appendix_rel))
    else:
        got = literal_names(apath, ('CONTRACT_FIRST', 'T1_REQUIRED')) or {}
        for key in ('CONTRACT_FIRST', 'T1_REQUIRED'):
            names = got.get(key)
            if names is None:
                findings.append(('CC-APPENDIX-EMPTY',
                                 '%s 里读不到 %s 的名字（用 ast 读常量、不 import）—— '
                                 '这张表名下的类会整体变成「没人管」'
                                 % (appendix_rel, key)))
                continue
            summary['from_appendix'][key] = len(names)
            for name in names:
                add(name, key)

    summary['union'] = len(registry)
    if not registry:
        findings.append(('CC-MANIFEST-EMPTY',
                         '登记侧（manifest.classes ∪ impl_only ∪ CONTRACT_FIRST ∪ '
                         'T1_REQUIRED）一个名字都没提取到 ⇒ 提取为空，后面的覆盖比对'
                         '一条都不会跑，拒绝通过'))
    elif len(registry) < min_registered:
        findings.append(('CC-THIN-REGISTERED',
                         '登记侧只提取出 %d 个名字（少于下限 %d）—— '
                         '提取器可能已经失配，这个数在变薄本身就是线索'
                         % (len(registry), min_registered)))
    return registry, summary, findings


def mentions_of(name, prose):
    """契约散文（已剥掉 ```python 块）里这个类名出现了几次、在哪些文档。

    边界用**显式排除 ASCII 标识符字符**，不用 `\b`：中文散文里类名常常紧贴着
    汉字（`抛RiskError（RISK_000）`），而 Python 把汉字也算作单词字符 ⇒
    `\b` 在那种写法下一次都不匹配（见下面的实现）。
    `\bRiskError\b` 在那种写法下**一次都不匹配**，会把「正文提到过」误判成
    「连名字都没有」。
    """
    pattern = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])')
    total = 0
    where = []
    for tag in sorted(prose):
        count = len(pattern.findall(prose[tag]))
        if count:
            total += count
            where.append('%s x%d' % (tag, count))
    return total, where


def audit(root, debt=None, local=None, damaged=None, contracts=None,
          impl_rel=None, manifest_rel=None, appendix_rel=None,
          min_declared=None, min_registered=None, min_impl=None):
    """返回 (summary, findings)。findings = [(code, message)]，已排序去重。"""
    debt = DEBT if debt is None else debt
    local = LOCAL if local is None else local
    damaged = DAMAGED if damaged is None else damaged
    contracts = CONTRACTS if contracts is None else contracts
    impl_rel = IMPL_REL if impl_rel is None else impl_rel
    manifest_rel = MANIFEST_REL if manifest_rel is None else manifest_rel
    appendix_rel = APPENDIX_REL if appendix_rel is None else appendix_rel
    min_declared = MIN_DECLARED if min_declared is None else min_declared
    min_registered = MIN_REGISTERED if min_registered is None else min_registered
    min_impl = MIN_IMPL if min_impl is None else min_impl

    findings = []
    summary = {'root': root, 'contracts': contracts, 'debt': sorted(debt),
               'local': sorted(local), 'declared': 0, 'impl_classes': 0,
               'covered': 0, 'by_declaration': 0, 'registration_only': 0,
               'uncovered': 0, 'declared_or_registered': 0,
               'triage_mentioned': 0, 'triage_absent': 0,
               'triage_mentioned_names': [], 'skipped': False}

    declared, prose, doc_summary, doc_findings = scan_contracts(root, contracts)
    findings.extend(doc_findings)
    summary.update({k: doc_summary[k] for k in
                    ('blocks', 'unparsable', 'class_hint', 'newtypes',
                     'per_doc', 'prose_chars', 'damaged')})
    summary['declared'] = len(declared)
    if not declared:
        findings.append(('CC-NO-DECLARED',
                         '三份契约共 %d 个 ```python 块，却一个类都没提取到 ⇒ '
                         '声明侧提取为空，拒绝通过' % summary['blocks']))
    elif len(declared) < min_declared:
        findings.append(('CC-THIN-DECLARED',
                         '三份契约只提取出 %d 个类（少于下限 %d）—— '
                         '提取器可能已经失配' % (len(declared), min_declared)))

    # 损坏块：块里出现的类名**全部登记过**才算豁免，出现一个新类名照旧报红。
    hinted = set()
    for item in summary['damaged']:
        hints = item['hints']
        if not hints:
            continue
        hinted.update((item['tag'], name) for name in hints)
        unknown = sorted(name for name in hints
                         if (item['tag'], name) not in damaged)
        if unknown:
            findings.append(('CC-UNPARSABLE-CLASS',
                             '契约 %s 文档 %d 行的块里有 `class ...` 但整块解析不了'
                             '（%s）—— 里头的类退出了比对；其中 %s 没有登记进 '
                             'DAMAGED（已登记的损坏块只豁免其登记过的类名）'
                             % (item['tag'], item['line'], item['why'],
                                ', '.join(unknown))))
    # 反向：登记项在坏块里找不到、或这个类已经能从块里解析出来 ⇒ 豁免作废。
    for key in sorted(damaged):
        tag, name = key
        if key in hinted:
            continue
        if any(rec['contract'] == tag for rec in declared.get(name, [])):
            findings.append(('CC-DAMAGED-STALE',
                             'DAMAGED 登记了（%s, %s），但这个块现在已经能解析出'
                             '这个类（%s）—— 豁免作废：删掉这一条，让它回到正常比对'
                             % (tag, name, declared[name][0]['where'])))
        else:
            findings.append(('CC-DAMAGED-STALE',
                             'DAMAGED 登记了（%s, %s），但契约 %s 里既没有「解析'
                             '不了的块含这个类名」、也没有它的类块 ⇒ 豁免失效：'
                             '要么损坏已消失（删掉登记），要么类名变了（改登记）'
                             % (tag, name, tag)))

    registry, reg_summary, reg_findings = scan_registry(
        root, manifest_rel, appendix_rel, min_registered)
    findings.extend(reg_findings)
    summary.update({k: reg_summary[k] for k in ('manifest', 'union')})
    summary['appendix'] = reg_summary['from_appendix']
    # 「声明 ∪ 登记」这个并集是 B9.2/B10 引用的那个分母，一律**算出来**再打印
    # （手推的数会随两侧各自增删而漂移，而漂移了没人知道）。
    summary['declared_or_registered'] = len(set(declared) | set(registry))

    impl, files, newtypes, impl_findings = scan_impl(root, impl_rel)
    findings.extend(impl_findings)
    summary['impl_files'] = files
    summary['impl_newtypes'] = newtypes
    summary['impl_classes'] = len(impl)
    for name in sorted(impl):
        if len(impl[name]) > 1:
            findings.append(('CC-IMPL-AMBIGUOUS',
                             'quanauto 里有 %d 处定义同名类 %s（%s）—— 比对对象不唯一'
                             % (len(impl[name]), name,
                                ', '.join(r['source'] for r in impl[name]))))
    if not impl:
        findings.append(('CC-NO-IMPL-CLASSES',
                         '扫 %s/*.py 得到 0 个类 ⇒ 没有可比对象，覆盖比对形同虚设，'
                         '拒绝通过' % impl_rel))
        return summary, sorted(set(findings))
    if len(impl) < min_impl:
        findings.append(('CC-THIN-IMPL',
                         '%s/*.py 只提取出 %d 个类（少于下限 %d）—— '
                         '提取器可能已经失配' % (impl_rel, len(impl), min_impl)))

    # 登记表反向自查：被契约声明或已在登记侧 ⇒ 免检资格当场作废；类没了 ⇒ 登记失效。
    for table, label, drift_code, stale_code in (
            (debt, 'DEBT', 'CC-DEBT-DRIFT', 'CC-DEBT-STALE'),
            (local, 'LOCAL', 'CC-LOCAL-DRIFT', 'CC-LOCAL-STALE')):
        for name in sorted(table):
            if name in declared:
                findings.append((drift_code,
                                 '%s 登记了 %s，但它已经在契约里有了类块（%s）—— '
                                 '免检登记当场作废：删掉这一条，改由声明侧覆盖'
                                 % (label, name, declared[name][0]['where'])))
            elif name in registry:
                findings.append((drift_code,
                                 '%s 登记了 %s，但它已经在**登记侧**（%s）—— '
                                 '两处登记重了，删掉本表这一条'
                                 % (label, name, ', '.join(registry[name]))))
            if name not in impl:
                findings.append((stale_code,
                                 '%s 登记了 %s，但 %s/*.py 里没有这个类 —— '
                                 '登记项失效' % (label, name, impl_rel)))

    by_declaration = sorted(name for name in impl if name in declared)
    registration_only = sorted(name for name in impl
                               if name in registry and name not in declared)
    covered = set(by_declaration) | set(registration_only)
    uncovered = sorted(name for name in impl if name not in covered)
    summary['by_declaration'] = len(by_declaration)
    summary['registration_only'] = len(registration_only)
    summary['covered'] = len(covered)
    summary['uncovered'] = len(uncovered)

    triaged = set(debt) | set(local)
    for name in uncovered:
        if name in triaged:
            continue
        count, where = mentions_of(name, prose)
        extra = ('（契约散文提到过它 %d 次：%s，但**提及不算覆盖**）'
                 % (count, ', '.join(where))) if count else '（三份契约连名字都没出现过）'
        findings.append(('CC-UNCOVERED',
                         'quanauto 的类 %s（%s）既不在契约声明里、也不在 manifest.'
                         'classes / impl_only / CONTRACT_FIRST / T1_REQUIRED 里，'
                         '也没登记进 DEBT/LOCAL %s —— 要么补契约/登记，要么登记'
                         '「它为什么没有契约来源」'
                         % (name, impl[name][0]['source'], extra)))

    mentioned = 0
    mentioned_names = []
    for name in sorted(triaged):
        if mentions_of(name, prose)[0]:
            mentioned += 1
            mentioned_names.append(name)
    summary['triage_mentioned'] = mentioned
    summary['triage_absent'] = len(triaged) - mentioned
    summary['triage_mentioned_names'] = mentioned_names

    summary['skipped'] = not covered
    if summary['skipped']:
        findings.append(('CC-NO-COMPARE',
                         '实现侧 %d 个类里一个都没与声明侧/登记侧对上（covered=0）—— '
                         '下面唯一的结论只覆盖了守卫与登记项，拒绝通过' % len(impl)))
    return summary, sorted(set(findings))


def report(summary, findings):
    print('root:            %s' % summary['root'])
    for tag, rel in summary['contracts']:
        names = summary['per_doc'].get(tag)
        print('contract[%-4s]   %s  classes=%s prose=%d chars'
              % (tag, rel, '-' if names is None else len(names),
                 summary['prose_chars'].get(tag, 0)))
    print('python blocks:   %d (unparsable=%d, of which class-like=%d, '
          'NewType aliases=%d)'
          % (summary['blocks'], summary['unparsable'], summary['class_hint'],
             summary['newtypes']))
    for item in summary['damaged']:
        print('NOTE: 契约 %s 文档 %d 行的块解析不了（%s）—— 类名提示=%s 首行=%s'
              % (item['tag'], item['line'], item['why'],
                 ','.join(item['hints']) or '-', item['head']))
    print('declared side:   classes=%d（只认 ast 解出来的 ClassDef，文本搜不算）'
          % summary['declared'])
    print('registry side:   manifest.classes=%s impl_only=%s CONTRACT_FIRST=%s '
          'T1_REQUIRED=%s -> union=%d（声明∪登记=%d）'
          % (summary['manifest'].get('classes', '-'),
             summary['manifest'].get('impl_only', '-'),
             summary['appendix'].get('CONTRACT_FIRST', '-'),
             summary['appendix'].get('T1_REQUIRED', '-'), summary['union'],
             summary['declared_or_registered']))
    print('impl scan:       %s/*.py files=%d classes=%d (NewType aliases=%d, '
          '不计入)'
          % (IMPL_REL, summary['impl_files'], summary['impl_classes'],
             summary['impl_newtypes']))
    print('coverage:        covered=%d (by declaration=%d, registration-only=%d) '
          'uncovered=%d'
          % (summary['covered'], summary['by_declaration'],
             summary['registration_only'], summary['uncovered']))
    print('triage:          debt(契约点过名、正文没有类块)=%d '
          'local(契约不定义)=%d'
          % (len(summary['debt']), len(summary['local'])))
    print('mention split:   登记表里 %d 个名字：契约散文提到过 %d / 连名字都没有 %d '
          '（提及不算覆盖）'
          % (len(summary['debt']) + len(summary['local']),
             summary['triage_mentioned'], summary['triage_absent']))
    print('  只靠散文提及 %d 个：%s'
          % (len(summary['triage_mentioned_names']),
             ', '.join(summary['triage_mentioned_names']) or '-'))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    if summary['skipped']:
        print('NOTE: 一个类都没对上 —— 上面的结论只覆盖了守卫与登记项')
    print('verdict: %s (%d finding(s))'
          % ('PASS' if not findings else 'DIRTY', len(findings)))
    return 1 if findings else 0


# ── 自测：每个探测器一个样本 ──────────────────────────────────────────
CORE_DOC = ('# 核心契约\n\n正文里点了 `Debt` 这个名字（提及，不是声明）。\n\n'
            '```python\nfrom dataclasses import dataclass\n\n\n'
            '@dataclass\nclass Alpha:\n    x: int = 0\n```\n')
DC_DOC = ('# 数据中心契约\n\n散文一句。\n\n```python\nclass Beta:\n    pass\n```\n')
RISK_DOC = ('# 风控契约\n\n散文一句。\n\n```python\nclass Gamma:\n    pass\n```\n')
CLEAN_MANIFEST = {
    'classes': {'Alpha': {'source': '自测样本'}},
    'impl_only': {'Beta': {'reason': '自测样本'}},
    'note': '自测样本',
}
CLEAN_APPENDIX = ("CONTRACT_FIRST = {'Delta': '自测样本'}\n"
                  "T1_REQUIRED = ('Alpha',)\n")
CLEAN_IMPL = ('@dataclass\nclass Alpha:\n    x: int = 0\n\n\n'
              'class Beta:\n    pass\n\n\n'
              'class Gamma:\n    pass\n\n\n'
              'class Local:\n    pass\n\n\n'
              'class Debt:\n    pass\n')
CLEAN_KW = {'debt': {'Debt': '自测样本'}, 'local': {'Local': '自测样本'},
            'damaged': {}, 'min_declared': 2, 'min_registered': 2, 'min_impl': 2}
# 只含一个损坏块的契约样本：块里**只有一个**类名（'Half'），否则「登记一半」与
# 「全部登记」两个样本会同时命中，看不出豁免到底看的是哪一个类名。
HALF_DOC = ('# 核心契约\n\n散文里点了 `Debt` 这个名字（提及，不是声明）。\n\n'
            '```python\nclass Half:\n```\n')
# 干净样本用的三份契约（每个 tag 一个类，且与 CLEAN_IMPL 逐条对得上）。
DEFAULT_DOCS = {'core': CORE_DOC, 'dc': DC_DOC, 'risk': RISK_DOC}

SANDBOX = {'root': None}


def patched(**kw):
    """在「三份全写」的基础上打补丁。

    样本里只传一份文档会让另两份根本不存在 ⇒ 声明数骤降，于是「样本没打中分支」
    会被误读成「探测器不存在」。
    """
    docs = dict(DEFAULT_DOCS)
    docs.update(kw)
    return docs


def sample(name, docs, impl, expect, marker='', manifest=None, appendix=None,
           **kw):
    """一个样本：写沙箱 → 跑 audit → 断言命中的**正是**那个检查码。

    只断言「有 FINDING」会让「变异没打到分支」与「探测器不存在」长得一模一样。
    `manifest=False` / `appendix=False` 表示**故意不写**那个文件（缺失样本）。
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
    if impl is None:
        # 连目录都不建 —— 这才是 CC-NO-IMPL-DIR 的样本；传 {} 表示「目录在、里面没类」
        pass
    elif impl == {}:
        os.makedirs(os.path.join(root, IMPL_REL), exist_ok=True)
    else:
        for fname, src in impl.items():
            write_text(os.path.join(root, IMPL_REL, fname), src)
    payload = CLEAN_MANIFEST if manifest is None else manifest
    if payload is not False:
        # 传 str 表示「故意写一段坏 JSON」；传 dict 才 json.dumps（否则坏样本会被
        # 序列化成合法的 JSON 字符串，样本静默空转）。
        write_text(os.path.join(root, MANIFEST_REL),
                   payload if isinstance(payload, str) else json.dumps(payload))
    source = CLEAN_APPENDIX if appendix is None else appendix
    if source is not False:
        write_text(os.path.join(root, APPENDIX_REL), source)
    summary, findings = audit(root, contracts=contracts,
                              **dict(CLEAN_KW, **kw))
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
    SANDBOX['root'] = tempfile.mkdtemp(prefix='class-coverage-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    impl = {'models.py': CLEAN_IMPL}
    ok = True

    ok &= sample('clean-control', None, impl, ())

    # 提取侧四条
    ok &= sample('no-contract', None, impl, 'CC-NO-CONTRACT',
                 extra_contracts=(('gone', os.path.join('docs', 'nope.md')),))
    ok &= sample('no-blocks', {'core': '# 核心契约\n\n只有散文，没有代码块。\n'},
                 impl, 'CC-NO-BLOCKS')
    ok &= sample('no-declared',
                 {'core': '# 核心契约\n\n```python\nx = 1\n```\n',
                  'dc': '# 数据中心契约\n\n```python\ny = 2\n```\n',
                  'risk': '# 风控契约\n\n```python\nz = 3\n```\n'},
                 impl, 'CC-NO-DECLARED', min_declared=1)
    ok &= sample('thin-declared', {'core': CORE_DOC, 'dc': DC_DOC},
                 {'models.py': CLEAN_IMPL.replace(
                     '\n\n\nclass Gamma:\n    pass\n', '\n')},
                 'CC-THIN-DECLARED', min_declared=3,
                 **{'local': {'Local': '自测样本'}})
    ok &= sample('no-prose',
                 {'core': '```python\nclass Alpha:\n    pass\n```\n'},
                 impl, 'CC-NO-PROSE', min_declared=1)
    ok &= sample('unparsable-class', patched(core=HALF_DOC), impl,
                 'CC-UNPARSABLE-CLASS', marker='没有登记进',
                 min_declared=2, min_registered=2,
                 **{'local': {'Local': '自测样本'}})
    # 登记过的损坏块只豁免它登记过的类名：全部登记 ⇒ 干净样本（不能报红）
    ok &= sample('damaged-registered-clean', patched(core=HALF_DOC), impl, (),
                 min_declared=2, min_registered=2,
                 **{'damaged': {('core', 'Half'): '自测样本'},
                    'local': {'Local': '自测样本'}})
    ok &= sample('damaged-stale-parsable', None, impl, 'CC-DAMAGED-STALE',
                 marker='已经能解析出',
                 **{'damaged': {('core', 'Alpha'): '自测样本'}})
    ok &= sample('damaged-stale-missing', None, impl, 'CC-DAMAGED-STALE',
                 marker='豁免失效',
                 **{'damaged': {('core', 'Nowhere'): '自测样本'}})

    # 登记侧五条
    ok &= sample('no-manifest', None, impl, 'CC-NO-MANIFEST', manifest=False)
    ok &= sample('bad-manifest', None, impl, 'CC-NO-MANIFEST',
                 manifest='{ 不是 json')
    ok &= sample('manifest-bad', None, impl, 'CC-MANIFEST-BAD',
                 manifest={'classes': ['Alpha'], 'impl_only': {}})
    ok &= sample('manifest-empty', None, impl, 'CC-MANIFEST-EMPTY',
                 manifest={'classes': {}, 'impl_only': {}}, appendix=False)
    ok &= sample('no-appendix', None, impl, 'CC-NO-APPENDIX', appendix=False)
    ok &= sample('appendix-empty', None, impl, 'CC-APPENDIX-EMPTY',
                 appendix='OTHER = 1\n', min_registered=2)
    ok &= sample('thin-registered', None, impl, 'CC-THIN-REGISTERED',
                 min_registered=99)

    # 实现侧四条
    ok &= sample('no-impl-dir', None, None, 'CC-NO-IMPL-DIR')
    ok &= sample('no-impl-classes', None, {'models.py': 'x = 1\n'},
                 'CC-NO-IMPL-CLASSES')
    ok &= sample('thin-impl', None,
                 {'models.py': CLEAN_IMPL.replace('\n\n\nclass Debt:\n    pass\n',
                                                  '\n')},
                 'CC-THIN-IMPL', min_impl=5, **{'debt': {}})
    ok &= sample('impl-scan', None, {'models.py': 'class Broken(:\n'},
                 'CC-IMPL-SCAN')
    ok &= sample('impl-ambiguous', None,
                 {'models.py': CLEAN_IMPL, 'more.py': CLEAN_IMPL},
                 'CC-IMPL-AMBIGUOUS')

    # 覆盖判据本体（正向：新增未登记的类必须报；提及不能顶替声明）
    ok &= sample('uncovered-absent', None,
                 {'models.py': CLEAN_IMPL + '\n\nclass Orphan:\n    pass\n'},
                 'CC-UNCOVERED', marker='Orphan')
    ok &= sample('mention-not-coverage',
                 patched(core=CORE_DOC + '\n散文里再点一次 `Orphan`。\n'),
                 {'models.py': CLEAN_IMPL + '\n\nclass Orphan:\n    pass\n'},
                 'CC-UNCOVERED', marker='提及不算覆盖')
    ok &= sample('no-compare', None,
                 {'models.py': 'class Local:\n    pass\n\n\nclass Debt:\n    pass\n'},
                 'CC-NO-COMPARE',
                 manifest={'classes': {'Zeta': {}}, 'impl_only': {}},
                 appendix="CONTRACT_FIRST = {}\nT1_REQUIRED = ()\n",
                 min_registered=1)

    # 登记表反向自查四条
    ok &= sample('debt-stale', None, impl, 'CC-DEBT-STALE',
                 **{'debt': {'Debt': '自测样本', 'Gone': '自测样本'}})
    ok &= sample('debt-drift-declared', None, impl, 'CC-DEBT-DRIFT',
                 marker='契约里有了类块',
                 **{'debt': {'Debt': '自测样本', 'Alpha': '自测样本'}})
    ok &= sample('local-stale', None, impl, 'CC-LOCAL-STALE',
                 **{'local': {'Local': '自测样本', 'Gone': '自测样本'}})
    # 把 dc 里声明的类换成 Epsilon，让 Beta 只剩 impl_only 这一条来源
    # —— 否则命中的是「已被契约声明」那个分支，注册分支根本没跑到。
    ok &= sample('local-drift-registered',
                 patched(dc=DC_DOC.replace('class Beta:', 'class Epsilon:')),
                 impl, 'CC-LOCAL-DRIFT', marker='两处登记重了',
                 **{'local': {'Local': '自测样本', 'Beta': '自测样本'}})

    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


# ── 触发测试（对真实产物）：`--selftest` 用的是合成样本，证明不了「真仓库那条
#    扫描路径不是空转」。这里把真实的 quanauto/ docs/ 与登记侧两份文件**拷进临时
#    目录**再改坏 —— 副本与原文件逐字节相同、走的是同一条扫描路径，而仓库本身
#    **一个字节都不动**（这是刻意的：门禁不该会写仓库）。
#    两条自我约束：① 干净副本的每个数必须与真仓库逐项相同（副本不忠实，后面的
#    「红」就不能算数）；② 每条只断言**预期的那几个检查码**，多报也判 FAIL
#    （「随便红了就行」是假触测）。
FALSIFY_IMPL_FILE = 'quanauto/enums.py'
FALSIFY_ANCHOR_OLD = 'class PsycopgConnection:'
FALSIFY_ANCHOR_FILE = 'quanauto/pgstore.py'
FALSIFY_ANCHOR_NEW = 'class PsycopgConnectionX:'
FALSIFY_PROBE_FILE = 'quanauto/_falsify_probe.py'
FALSIFY_PROBE_SRC = ('"""一次性变异探针：谁都不管的类（只存在于临时副本里）。"""\n'
                     '\n'
                     '\n'
                     'class FalsifyCoverageProbe:\n'
                     '    """契约与登记表里都没有它。"""\n'
                     '\n'
                     '    pass\n')


def falsify(argv):
    """拿真实产物的**逐字节副本**改坏，确认本门禁在真仓库上会红。"""
    root = None
    for arg in argv:
        if arg.startswith('--root='):
            root = arg.split('=', 1)[1]
        elif not arg.startswith('--'):
            root = arg
    src = os.path.abspath(root or os.path.join(ROOT, '..'))
    print('falsify src:     %s' % src)

    base, base_findings = audit(src)
    if base_findings:
        print('FALSIFY FAIL: 真仓库本身就不绿（%d finding(s)）—— '
              '先在干净树上跑一次，否则后面的「红」说明不了任何事'
              % len(base_findings))
        return 1
    keys = ('declared', 'impl_files', 'impl_classes', 'union', 'covered',
            'by_declaration', 'registration_only', 'uncovered')
    print('baseline:        impl_files=%d impl_classes=%d declared=%d union=%d '
          'covered=%d uncovered=%d'
          % (base['impl_files'], base['impl_classes'], base['declared'],
             base['union'], base['covered'], base['uncovered']))

    sandbox = tempfile.mkdtemp(prefix='class-coverage-falsify-')
    print('sandbox:         %s' % sandbox)
    shutil.copytree(os.path.join(src, IMPL_REL),
                    os.path.join(sandbox, IMPL_REL))
    shutil.copytree(os.path.join(src, 'docs'), os.path.join(sandbox, 'docs'))
    for rel in (MANIFEST_REL, APPENDIX_REL):
        target = os.path.join(sandbox, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(src, rel), target)

    copy, copy_findings = audit(sandbox)
    drift = [(key, base[key], copy[key]) for key in keys if base[key] != copy[key]]
    if copy_findings or drift:
        print('FALSIFY FAIL: 副本不忠实（findings=%d, 数漂移=%s）—— '
              '副本与真仓库不一致，后面的红不能算数'
              % (len(copy_findings), drift or '-'))
        return 1
    print('copy baseline:   与真仓库逐项相同 —— 后面的变异打在等价输入上')

    cases = []
    # M1 正向：塞一个契约与登记表都不管的类（**新文件**，不依赖任何锚点）
    probe = os.path.join(sandbox, FALSIFY_PROBE_FILE)

    def add_probe():
        write_text(probe, FALSIFY_PROBE_SRC)

    def drop_probe():
        if os.path.exists(probe):
            os.remove(probe)

    cases.append(('M1-uncovered-new-file', add_probe, drop_probe,
                  {'CC-UNCOVERED'},
                  {'impl_files': base['impl_files'] + 1,
                   'impl_classes': base['impl_classes'] + 1,
                   'uncovered': base['uncovered'] + 1}))
    # M2 反向：把 LOCAL 表里登记过的类改名 ⇒ 登记失效，同时新名字没人管
    anchor = os.path.join(sandbox, FALSIFY_ANCHOR_FILE)
    original = read_text(anchor)
    if original.count(FALSIFY_ANCHOR_OLD) != 1:
        print('FALSIFY FAIL: %s 里的锚点 `%s` 出现 %d 次（要求恰好 1 次）—— '
              '锚点是**故意写死的**：源文件改了就在这里响亮地失败，'
              '而不是静默跳过一条样本'
              % (FALSIFY_ANCHOR_FILE, FALSIFY_ANCHOR_OLD,
                 original.count(FALSIFY_ANCHOR_OLD)))
        return 1

    def rename_conn():
        write_text(anchor, original.replace(FALSIFY_ANCHOR_OLD,
                                            FALSIFY_ANCHOR_NEW))

    def unrename_conn():
        write_text(anchor, original)

    cases.append(('M2-local-stale-rename', rename_conn, unrename_conn,
                  {'CC-UNCOVERED', 'CC-LOCAL-STALE'}, {}))

    failed = 0
    for label, setup, teardown, want_codes, want_counts in cases:
        setup()
        got, findings = audit(sandbox)
        teardown()
        codes = sorted(set(code for code, _ in findings))
        counts_bad = [(key, want_counts[key], got[key]) for key in want_counts
                      if got[key] != want_counts[key]]
        ok = codes == sorted(want_codes) and not counts_bad
        print('  [%s] exit=%d codes=%s%s'
              % (label, 1 if findings else 0, ', '.join(codes) or '-',
                 '' if ok else '  <<< MISMATCH'))
        for code, message in findings:
            print('      %s: %s' % (code, message[:150]))
        if codes != sorted(want_codes):
            print('      want codes=%s' % sorted(want_codes))
            ok = False
        if counts_bad:
            print('      want counts=%s' % counts_bad)
            ok = False
        if not ok:
            failed += 1

    # 收尾：每条样本都撤销后必须**回到基线**。少了这一条，某条样本的残留会让
    # 下一条的结论与被测文件无关（而报告里看不出来）。
    tail, tail_findings = audit(sandbox)
    drift = [(key, base[key], tail[key]) for key in keys if base[key] != tail[key]]
    ok = not tail_findings and not drift
    print('  [after-teardown-clean] exit=%d findings=%d 数漂移=%s%s'
          % (1 if tail_findings else 0, len(tail_findings), drift or '-',
             '' if ok else '  <<< MISMATCH'))
    if not ok:
        failed += 1
    total = len(cases) + 1
    print('FALSIFY %s: %d/%d case(s) matched'
          % ('OK' if not failed else 'FAIL', total - failed, total))
    return 1 if failed else 0


def main(argv):
    harden_stdout()
    if '--selftest' in argv:
        return selftest()
    if '--falsify' in argv:
        return falsify([a for a in argv if a != '--falsify'])
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
