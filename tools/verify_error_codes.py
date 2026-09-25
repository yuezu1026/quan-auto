#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""错误码对账：三份契约的「错误码」表 x `quanauto/` 的实现。

为什么存在
----------
「契约定义了哪些错误码」这件事原先**一条机器判据都没有**：contract-appendix 比的是 G 节
的类块、contract-signature 比的是参数与字段、enum-members 比的是枚举成员、class-coverage
比的是「类归谁管」。错误码是**字符串常量**，落在所有这些网的缝里。

而这个缝是被**登记过三次**的已知缺口：

  * 数据中心契约的 A8/C6 两处都写着「§3.9 的异常码表（DATA_001~DATA_007）与实现的对应 |
    无机器判据，已知缺口」；
  * 风控契约 §3.7 的开头写着「与主契约附录 D 衔接：`RISK_001`、`RISK_002` 已在主契约中
    定义，本契约补充 `RISK_003` 起。**两处定义必须保持一致，不得冲突。**」—— 那句话本身
    就是一条**没有判据的要求**。

写本门禁时实测（2026-09-25）：那两条要求**已经不成立**。

  1. `RISK_001` 的级别：主契约码表写 CRITICAL，风控契约 §3.7 写 ERROR，而 §3.7 那一行
     自己标着「（主契约原有）」—— 它声称只是转抄，实际抄错了。
  2. 看板码的拼写：主契约 F4 写 `DASHBOARD001`（无下划线），`quanauto/errors.py` 写
     `DASHBOARD_001`。而 F4 给这个写法写的理由是「错误码 `DASHBOARD001` 是已经存在的
     公开标识（`quanauto/errors.py`）」—— 少了那个下划线，**这个标识根本不存在**，
     理由是自证不成立的。

检查（每一项独立执行，互不 return；报出来的每个码在 `--selftest` / `--falsify` 里各有样本）
  EC-EMPTY-<DOC>      三份契约里那张码表**提取为空** ⇒ 后续每一项都在空集上跑，拒判
  EC-EMPTY-IMPL       实现侧一个 `code` 都没解析出来 ⇒ 同上
  EC-IMPL-UNPARSABLE  `quanauto/*.py` 里有解析不了的（语法错误）⇒ 静默跳过会让它的类
                      悄悄退出比对，等于免检
  EC-TABLE-<DOC>      同一份文档里出现了**不止一张**「错误码 + 级别」表 ⇒ 形状变了，得由人
                      指明哪张才是权威（本门禁只读第一张，但先把这件事喊出来）
  EC-CODE-SHAPE       码表某一行的「错误码」列不是 `<前缀>_<三位数字>` 的形状 ⇒ 这一行会被
                      静默丢掉（「少一个下划线 ⇒ 整行消失」的入口）
  EC-GAP-<DOC>        同一前缀的编号不是从 1 连续（`DATA_001, DATA_002, DATA_004`）⇒ 有一行
                      被删了。表的**行数**看不出来，缺口能
  EC-THIN-<DOC>       提取到的行数/码数低于下限（远低于当下的实测值）⇒ 提取器塌了，不许把
                      「文档变短了」当成结论
  EC-CLASS-MISSING    §3.9 表里点名的异常类在 `quanauto/` 里没有对应的类
  EC-CLASS-MISMATCH   类在，但它的 `code` 与表里给这个类的码不同
  EC-CODE-ORPHAN      契约表里的码既没实现、也不在 `PENDING` 登记里 ⇒ 补实现或补登记
  EC-STALE-PENDING    `PENDING` 里登记的那个码**已经实现了**（或契约表里已经没有它了）⇒
                      登记不能再留着，否则它从「明账」腐化成永久免检的挡箭牌
  EC-CODE-INVENTED    实现里的码在任何契约表里都找不到、也不在 `IMPL_ONLY` 登记里
  EC-IMPL-ONLY-STALE  `IMPL_ONLY` 里登记的码已经不在实现里了 ⇒ 双向都要收口
  EC-SPELLING         同一个码出现了**两种拼写**且没登记 ⇒ 日志里的码与文档里的表对不上，
                      而「对不上」这种错没人会去查
  EC-SPELLING-STALE   登记过的拼写分歧**已经一致**了 ⇒ 登记必须销掉
  EC-LEVEL-CONFLICT   两张表都定义了同一个码，级别却不同（风控 §3.7 自己要求「不得冲突」）
  EC-RISK-RANGE       风控 §3.7 声称「补充 RISK_003 起」：本表的 RISK 编号必须是从 1 起的
                      连续整数，且主契约码表里不许出现 RISK_003 及以后的码 —— 否则那两句
                      话只能有一句是真的

判据的**方向**（刻意如此，别顺手改成「两边相等」）
  契约 -> 实现不是满射：契约里有一批码是**刻意不实现**的（`PENDING`，12 个），理由写在
  `quanauto/errors.py` 的模块 docstring 里：「只定义真的会抛的异常 …… 预支的异常没人抛，
  就等于没有测试覆盖的死代码，还会让「已实现」看起来比实际多」。所以判据是两条单向的：
    契约表的每个码   必须 实现，或在 `PENDING` 里带理由；
    实现里的每个码   必须 在契约表里，或在 `IMPL_ONLY` 里带理由。
  两张登记表**都查反向**（EC-STALE-PENDING / EC-IMPL-ONLY-STALE）。少这一次反向，登记表
  就会变成免检名单 —— 那是「报告比真通过还干净」那一族。

边界（这个门禁不证明什么）
  * 只比**码 / 拼写 / 级别 / 类名**四样。不比描述、不比建议措施、不比异常之间的继承关系：
    `DataNotAvailableError` 继承 `DataFeedError` 而不是 §3.9 正文里的 `DataCenterError`
    是一处**已登记的刻意偏离**（见 `quanauto/errors.py` 与数据中心契约），拿 `issubclass`
    去查它会造出一个假阳性，而假阳性会被用来削弱判据。
  * 扫描面只有三份契约 + `quanauto/*.py`。`docs/迭代计划.md`、`docs/开工前缺口清单.md`
    里的码**不参与**比对：它们是记录，不是契约（拿记录去当判据，等于把过去的自己变成法条）。
  * 静态文本 + AST。它证明「码对得上」，**证明不了**「该抛的时候真的会抛出来」。

自测
  python tools/verify_error_codes.py --selftest   # 夹具样本：每个检查码各有一个坏样本
  python tools/verify_error_codes.py --falsify    # 真产物副本上做变异（真实仓库得先是绿的）

Exit codes: 0 = PASS, 1 = FAIL, 2 = selftest/falsify 自己的断言失败（门禁坏了，不是产物坏了）
"""
import ast
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_REL = os.path.join('docs', '智能量化交易平台-核心模块接口契约文档.md')
DC_REL = os.path.join('docs', '智能量化交易平台-数据中心接口契约文档.md')
RISK_REL = os.path.join('docs', '智能量化交易平台-风控层接口契约文档.md')
IMPL_DIR = 'quanauto'
IMPL_REL = os.path.join(IMPL_DIR, 'errors.py')

# 打印用的是这几个简称；`EC-<码>-<简称>` 就是报告里出现的形状。
DOCS = (('CORE', CORE_REL), ('DC', DC_REL), ('RISK', RISK_REL))
DOC_NAME = {'CORE': '主契约', 'DC': '数据中心契约', 'RISK': '风控契约'}

# 表里的码必须长这样：大写前缀 + 下划线 + 正好三位数字。
CODE_RE = re.compile(r'^[A-Z][A-Z0-9]*_\d{3}$')
# 正文/AST 里出现的「像错误码的东西」：允许漏掉那个下划线 —— 漏了才要报 EC-SPELLING。
TOKEN_RE = re.compile(r'(?<![A-Za-z0-9_])[A-Z]{3,}_?\d{3}(?![A-Za-z0-9_])')
# 静态检查器的抑制注释不是错误码：`# noqa: BLE001` 里的 `BLE001` 会被 TOKEN_RE 命中
# （实测 11 处，占 token 总数的 4%）。它单形、无害，但① 让分母失真，② 若有人写成
# `BLE_001` 就会造出一个**假分歧**（而假阳性会被用来削弱判据）。所以先剔掉注释尾巴，
# 行号不受影响（只删同一行的尾部）。
NOQA_RE = re.compile(r'#[^\n]*\bnoqa\b[^\n]*')
LEVELS = ('CRITICAL', 'ERROR', 'WARNING', 'INFO')

# 报告里要打出分母：分母为 0 的报告不许被读成通过。
DETECTORS = (
    'EC-EMPTY', 'EC-IMPL-UNPARSABLE', 'EC-TABLE-COUNT', 'EC-CODE-SHAPE', 'EC-GAP',
    'EC-THIN', 'EC-CLASS-MISSING', 'EC-CLASS-MISMATCH', 'EC-CODE-ORPHAN',
    'EC-STALE-PENDING', 'EC-CODE-INVENTED', 'EC-IMPL-ONLY-STALE', 'EC-SPELLING',
    'EC-SPELLING-STALE', 'EC-LEVEL-CONFLICT', 'EC-RISK-RANGE',
)

# 提取下限：取**远低于**当下实测值，只用来抓「提取器塌了」，不当作计数基线。
# 实测（2026-09-25）：表行数 core=21 / dc=7 / risk=11，实现侧码 30 个，
# 全文码形状 token 出现 259 次、覆盖 42 个不同的码。
MIN_ROWS = {'CORE': 15, 'DC': 5, 'RISK': 8}
MIN_IMPL_CODES = 15
MIN_TOKENS = 80

# ── 两张登记表 ────────────────────────────────────────────────────────────
# 契约定义、实现**刻意**不定义。理由统一来自 `quanauto/errors.py` 的模块 docstring，
# 另有三条是那里面单独记过的（RISK_003/009/011）。
_PENDING_WHY = (
    '契约定义、实现未定义：`errors.py` 的模块 docstring 写着「只定义真的会抛的异常 …… '
    '预支的异常没人抛，就等于没有测试覆盖的死代码，还会让「已实现」看起来比实际多」。'
    '要抛它时补类，并**同批**从本表删掉（否则 EC-STALE-PENDING 当场红）。'
)
PENDING = {}
for _c in ('STRATEGY_007', 'STRATEGY_008', 'STRATEGY_010',
           'BACKTEST_003', 'BACKTEST_004', 'BACKTEST_005', 'BACKTEST_006',
           'BROKER_003', 'RISK_008'):
    PENDING[_c] = _PENDING_WHY
PENDING['RISK_003'] = ('契约定义、实现未定义：熔断在实现里与 `RISK_002` 走同一条路径，'
                       '两者靠 `message` 里的 `breaker_key` 区分，没有单独的异常类。' + _PENDING_WHY)
PENDING['RISK_009'] = ('契约定义、实现未定义：放宽变更未确认在实现里是一条**正常业务结果**'
                       '（`PENDING` 态），不是异常 —— 用异常表达会让「等下一交易日生效」'
                       '看起来像失败。' + _PENDING_WHY)
PENDING['RISK_011'] = ('契约定义、实现未定义：权益峰值缺失在实现里以 WARNING 级违规上报，'
                       '不走异常路径。' + _PENDING_WHY)
del _c

# 实现侧有、契约表里没有的码。**每一条都要有理由，而且会被反向检查**：一旦那个码从实现里
# 消失，EC-IMPL-ONLY-STALE 就红 —— 免得这张表长出永不失效的条目。
IMPL_ONLY = {
    'QUAN_000': '自造的**根哨兵码**：契约按模块分别命名异常、没给公用根。用途是让调用方写 '
                '`except QuanAutoError` 兜底而不是 `except Exception`。',
    'DATA_000': '同上的根哨兵码（`DataCenterError`）。契约的 DATA 表从 001 起。',
    'RISK_000': '同上的根哨兵码（`RiskError`）。契约的 RISK 表从 001 起。',
    'DATA_008': '实现侧追加：存储层错误（`DataStoreError`），登记在数据中心契约的 C1 节。',
    'DASHBOARD_001': '实现侧追加：看板码，主契约 F4 用散文引用了它（写成 `DASHBOARD001`）。'
                     '拼写分歧见 `KNOWN_SPELLINGS`。',
}

# 已登记的拼写分歧。判据是**双向**的：这里登记了、而两侧已经写一致了 ⇒ EC-SPELLING-STALE。
KNOWN_SPELLINGS = {
    'DASHBOARD001': {
        'forms': ('DASHBOARD001', 'DASHBOARD_001'),
        'what': '主契约 F4 的两句话 vs `quanauto/errors.py`',
        'reason': '主契约 F4 写 `DASHBOARD001`（少一个下划线），实现写 `DASHBOARD_001`。'
                  '裁决：以**实现侧为准** —— F4 为这个写法给的理由是「错误码 `DASHBOARD001` '
                  '是已经存在的公开标识（`quanauto/errors.py`）」，缺下划线时该标识不存在，'
                  '理由是自证不成立的；同族其它码（`STRATEGY_001`、`DATA_001`…）一律带下划线。'
                  '更正登记进主契约的 G 节（正文由 .docx 派生、只许追加，那两处拼写照旧留在'
                  '文件里）⇒ 本门禁把它钉成**显式例外**，而不是放宽拼写判据。',
    },
}


# ── 基础工具 ──────────────────────────────────────────────────────────────
def harden_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):  # pragma: no cover - 非 cp936 控制台
            pass


def read_text(path):
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        raw = fh.read()
    return raw.decode('utf-8-sig', errors='replace').replace('\r\n', '\n')


def write_text(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def must(text, old, new, tag, count=1):
    """变异必须自己断言命中 —— 命中 0 次还继续跑，等于在**未修改的输入**上得结论。

    `count` 是**要求命中几次**（默认恰好 1 次）。整体替换（例如「散落各处的同一个拼写
    一次改遍」）必须把次数写出来：看着脆，但那正是要的 —— 文档里多出一处同样的字符串时
    这里会**响亮地**失败并逼人重新看一遍，而不是默默漏掉一处。
    """
    n = text.count(old)
    if n != count:
        print('SELFTEST/FALSIFY FAIL: %s 里的锚点 %r 出现 %d 次（要求恰好 %d 次）'
              % (tag, old[:90], n, count))
        raise SystemExit(2)
    out = text.replace(old, new)
    if out == text:
        print('SELFTEST/FALSIFY FAIL: %s 的替换没有改变内容（%r -> %r）'
              % (tag, old[:60], new[:60]))
        raise SystemExit(2)
    return out


# ── 文档侧：markdown 表 ───────────────────────────────────────────────────
class Table(object):
    def __init__(self, header, rows, line, has_cls):
        self.header = header
        self.rows = rows
        self.line = line
        self.has_cls = has_cls


def _split_row(line):
    body = line.strip()
    if body.startswith('|'):
        body = body[1:]
    if body.endswith('|'):
        body = body[:-1]
    return [c.strip() for c in body.split('|')]


def _clean(cell):
    return cell.strip().strip('`').replace('**', '').strip()


def _is_sep(cells):
    return bool(cells) and all(re.match(r'^:?-{2,}:?$', c) for c in cells)


def code_tables(text):
    """所有「表头同时含『错误码』与『级别』」的 markdown 表。"""
    lines = text.split('\n')
    out = []
    i = 0
    while i < len(lines) - 1:
        if not lines[i].lstrip().startswith('|') or not _is_sep(_split_row(lines[i + 1])):
            i += 1
            continue
        header = [_clean(c) for c in _split_row(lines[i])]
        if '错误码' not in header or '级别' not in header:
            i += 1
            continue
        code_i = header.index('错误码')
        level_i = header.index('级别')
        cls_i = header.index('异常类') if '异常类' in header else None
        rows = []
        j = i + 2
        while j < len(lines) and lines[j].lstrip().startswith('|'):
            cells = _split_row(lines[j])
            if not _is_sep(cells):
                def get(idx):
                    return cells[idx] if idx is not None and idx < len(cells) else ''
                rows.append({'code': _clean(get(code_i)), 'raw_code': get(code_i),
                             'level': _clean(get(level_i)).upper(),
                             'cls': _clean(get(cls_i)), 'line': j + 1})
            j += 1
        out.append(Table(header, rows, i + 1, cls_i is not None))
        i = j
    return out


# ── 实现侧：AST（永不 import —— 「读常量」不能依赖被测模块的副作用）────────
def scan_impl(dirpath):
    out = {'files': [], 'texts': {}, 'classes': {}, 'codes': {}, 'unparsable': []}
    if not os.path.isdir(dirpath):
        return out
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith('.py'):
            continue
        text = read_text(os.path.join(dirpath, name))
        if text is None:
            continue
        out['files'].append(name)
        out['texts'][name] = text
        try:
            tree = ast.parse(text)
        except SyntaxError:
            out['unparsable'].append(name)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            code = None
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if (isinstance(target, ast.Name) and target.id == 'code'
                            and isinstance(stmt.value, ast.Constant)
                            and isinstance(stmt.value.value, str)):
                        code = stmt.value.value
            out['classes'][node.name] = (code, name, node.lineno)
            if code:
                out['codes'].setdefault(code, []).append(node.name)
    return out


# ── 主判据 ────────────────────────────────────────────────────────────────
def audit(root, floors=True, pending=None, impl_only=None, spellings=None):
    pending = PENDING if pending is None else pending
    impl_only = IMPL_ONLY if impl_only is None else impl_only
    spellings = KNOWN_SPELLINGS if spellings is None else spellings
    findings = []
    summ = {'root': root, 'tables': {}, 'impl': {}, 'spelling': {}}

    # 1. 三份契约各自的码表
    texts = {}
    for key, rel in DOCS:
        text = read_text(os.path.join(root, rel))
        texts[key] = text
        if text is None:
            findings.append(('EC-EMPTY-%s' % key,
                             '%s 读不到 —— 码表提取为空，后面的间距/下限/级别/范围四项都会在'
                             '空集上静默变绿' % rel))
            summ['tables'][key] = None
            continue
        found = code_tables(text)
        if not found:
            findings.append(('EC-EMPTY-%s' % key,
                             '%s 里没有一张「表头含错误码与级别」的表 —— 提取为空，'
                             'EC-GAP / EC-THIN / EC-LEVEL-CONFLICT / EC-RISK-RANGE 全部会空转'
                             % rel))
            summ['tables'][key] = None
            continue
        if len(found) > 1:
            findings.append(('EC-TABLE-%s' % key,
                             '%s 里有 %d 张「错误码 + 级别」表（起始行 %s）—— 形状变了，'
                             '必须由人指明哪一张权威；本门禁只读第一张（行 %d），'
                             '另外几行都没被比对' % (rel, len(found),
                                                '/'.join(str(t.line) for t in found),
                                                found[0].line)))
        summ['tables'][key] = found[0]

    # 2. 表内行的形状 + 每个前缀的编号连续性
    for key, rel in DOCS:
        table = summ['tables'][key]
        if table is None:
            continue
        by_prefix = {}
        for row in table.rows:
            if not CODE_RE.match(row['code']):
                findings.append(('EC-CODE-SHAPE',
                                 '%s:%d 的「错误码」列是 %r，不是 `<前缀>_<三位数字>` 的形状'
                                 ' —— 这一行会被静默丢掉，既不进清单也不报缺' %
                                 (rel, row['line'], row['raw_code'])))
                continue
            pre, num = row['code'].rsplit('_', 1)
            by_prefix.setdefault(pre, []).append(int(num))
        for pre in sorted(by_prefix):
            nums = by_prefix[pre]
            missing = [n for n in range(1, max(nums) + 1) if n not in nums]
            dup = sorted(n for n in set(nums) if nums.count(n) > 1)
            if missing or dup:
                findings.append(('EC-GAP-%s' % key,
                                 '%s 的 %s 编号不是从 1 起的连续整数：缺 %s、重复 %s'
                                 '（现有 %s）—— 有一行被删掉或抄重了，而这种漂移'
                                 '**行数看不出来**' %
                                 (rel, pre, missing or '无', dup or '无',
                                  sorted(nums))))

    # 3. 提取下限（抓提取器塌了，不当作计数基线）
    for key, rel in DOCS:
        table = summ['tables'][key]
        if table is None or not floors:
            continue
        if len(table.rows) < MIN_ROWS[key]:
            findings.append(('EC-THIN-%s' % key,
                             '%s 只提取到 %d 行（下限 %d）—— 提取器塌了，'
                             '不许把「文档变短了」当成结论' % (rel, len(table.rows),
                                                        MIN_ROWS[key])))

    # 4. 实现侧
    impl = scan_impl(os.path.join(root, IMPL_DIR))
    summ['impl'] = impl
    for name in impl['unparsable']:
        findings.append(('EC-IMPL-UNPARSABLE',
                         '%s/%s 解析不了（语法错误）—— 它的类会**静默退出**比对，'
                         '看起来像「本来就没有」' % (IMPL_DIR, name)))
    if not impl['codes']:
        findings.append(('EC-EMPTY-IMPL',
                         '%s/ 下一个 `code = "..."` 都没解析出来 —— 实现侧为空，'
                         '后面的类名/编造/登记四项都会空转' % IMPL_DIR))
    if floors and len(impl['codes']) < MIN_IMPL_CODES:
        findings.append(('EC-THIN-IMPL',
                         '实现侧只解析出 %d 个不同的码（下限 %d）—— 提取塌了'
                         % (len(impl['codes']), MIN_IMPL_CODES)))

    # 5. 表里的码 -> 全局清单
    contract_codes = {}
    levels = {}
    for key, rel in DOCS:
        table = summ['tables'][key]
        if table is None:
            continue
        levels[key] = {}
        for row in table.rows:
            if CODE_RE.match(row['code']):
                contract_codes.setdefault(row['code'], []).append('%s:%d' % (rel, row['line']))
            if row['level']:
                levels[key].setdefault(row['code'], row['level'])

    # 6. §3.9 的异常类 <-> 实现
    for key, rel in DOCS:
        table = summ['tables'][key]
        if table is None or not table.has_cls:
            continue
        for row in table.rows:
            cls = row['cls']
            if not cls:
                continue
            got = impl['classes'].get(cls)
            if got is None:
                findings.append(('EC-CLASS-MISSING',
                                 '%s:%d 把 %s 的异常类写成 `%s`，但 %s/ 里没有这个类'
                                 '（表里的名字是实现要满足的契约）' %
                                 (rel, row['line'], row['code'], cls, IMPL_DIR)))
            elif got[0] != row['code']:
                findings.append(('EC-CLASS-MISMATCH',
                                 '%s:%d 把 `%s` 配给 %s，而实现里 %s.code = %r'
                                 '（%s/%s:%d）—— 日志里的码会与文档里的表对不上' %
                                 (rel, row['line'], cls, row['code'], cls, got[0],
                                  IMPL_DIR, got[1], got[2])))

    # 7. 两个方向 + 两张登记表的反向
    orphan = sorted(c for c in contract_codes
                    if c not in impl['codes'] and c not in pending)
    for code in orphan:
        findings.append(('EC-CODE-ORPHAN',
                         '%s 定义了 %s，实现里没有，`PENDING` 里也没登记 —— '
                         '要么补实现，要么补登记（别忘了登记表反向也查）' %
                         ('/'.join(contract_codes[code]), code)))
    for code in sorted(pending):
        if code in impl['codes']:
            findings.append(('EC-STALE-PENDING',
                             '`PENDING` 登记了 %s，但它**已经实现了**（%s）—— '
                             '登记必须销掉，否则它从明账腐化成免检挡箭牌；'
                             '原登记理由：%s' % (code, '/'.join(impl['codes'][code]),
                                            pending[code])))
        elif code not in contract_codes:
            findings.append(('EC-STALE-PENDING',
                             '`PENDING` 登记了 %s，但三份契约的码表里已经没有它了 —— '
                             '登记随着契约一起漂了' % code))
    for code in sorted(impl['codes']):
        if code not in contract_codes and code not in impl_only:
            findings.append(('EC-CODE-INVENTED',
                             '实现里的 %s（%s）在任何契约表里都找不到，也不在 `IMPL_ONLY` '
                             '登记里 —— 自创码会让「日志里的 code」和文档里的表对不上，'
                             '而这种错没人会去查' % (code, '/'.join(impl['codes'][code]))))
    for code in sorted(impl_only):
        if code not in impl['codes']:
            findings.append(('EC-IMPL-ONLY-STALE',
                             '`IMPL_ONLY` 登记了 %s，但实现里已经没有这个码了 —— '
                             '登记必须销掉；原登记理由：%s' % (code, impl_only[code])))

    # 8. 重叠码的级别必须一致
    keys = [k for k, _ in DOCS if summ['tables'][k] is not None]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            for code in sorted(set(levels[a]) & set(levels[b])):
                if levels[a][code] != levels[b][code]:
                    findings.append(('EC-LEVEL-CONFLICT',
                                     '%s 的 %s 是 %s，%s 的同一个码是 %s —— 两张表都定义了'
                                     '它就必须一致（风控 §3.7 自己写着「两处定义必须保持一致，'
                                     '不得冲突」，而那句话原先没有判据）' %
                                     (DOC_NAME[a], code, levels[a][code],
                                      DOC_NAME[b], levels[b][code])))

    # 9. 「本契约补充 RISK_003 起」这句话本身要被判据盯住
    risk_rows = summ['tables']['RISK']
    core_rows = summ['tables']['CORE']
    if risk_rows is not None and core_rows is not None:
        nums = sorted(int(r['code'].rsplit('_', 1)[1]) for r in risk_rows.rows
                      if r['code'].startswith('RISK_') and CODE_RE.match(r['code']))
        if nums and nums != list(range(1, len(nums) + 1)):
            findings.append(('EC-RISK-RANGE',
                             '风控 §3.7 的 RISK 编号不是从 1 起的连续整数：%s' % nums))
        extra = sorted(int(r['code'].rsplit('_', 1)[1]) for r in core_rows.rows
                       if r['code'].startswith('RISK_') and CODE_RE.match(r['code'])
                       and int(r['code'].rsplit('_', 1)[1]) > 2)
        if extra:
            findings.append(('EC-RISK-RANGE',
                             '主契约码表里出现了 %s，而风控 §3.7 声称「RISK_001/RISK_002 '
                             '已在主契约中定义，本契约补充 RISK_003 起」—— 两句话只能有一句'
                             '是真的' % ', '.join('RISK_%03d' % n for n in extra)))

    # 10. 拼写
    tokens = {}

    def collect(where, text):
        for lineno, line in enumerate(text.split('\n'), 1):
            for m in TOKEN_RE.finditer(NOQA_RE.sub('#', line)):
                tokens.setdefault(m.group(0).replace('_', ''), {}).setdefault(
                    m.group(0), []).append('%s:%d' % (where, lineno))

    for key, rel in DOCS:
        if texts.get(key) is not None:
            collect(rel, texts[key])
    for name in impl['files']:
        collect('%s/%s' % (IMPL_DIR, name), impl['texts'][name])
    divergent = {n: f for n, f in tokens.items() if len(f) > 1}
    for norm in sorted(divergent):
        forms = divergent[norm]
        reg = spellings.get(norm)
        if reg is None:
            findings.append(('EC-SPELLING',
                             '%s 出现了 %d 种拼写：%s —— 同一个码两种写法，'
                             '日志里的 code 与文档里的表就对不上了' %
                             (norm, len(forms),
                              ' / '.join('%s(%s)' % (f, ', '.join(sorted(forms[f])[:3]))
                                         for f in sorted(forms)))))
            continue
        extra = sorted(set(forms) - set(reg['forms']))
        if extra:
            findings.append(('EC-SPELLING',
                             '登记过的拼写分歧 %s 里出现了**新的**拼写 %s'
                             '（登记只覆盖 %s）' % (norm, extra, list(reg['forms']))))
    for norm in sorted(spellings):
        if norm not in divergent:
            findings.append(('EC-SPELLING-STALE',
                             '登记过的拼写分歧 %s 现在没有分歧了 —— 登记必须销掉：%s'
                             % (norm, spellings[norm]['reason'])))
    summ['spelling'] = {'tokens': sum(len(locs) for forms in tokens.values()
                                      for locs in forms.values()),
                        'normalized': len(tokens), 'divergent': len(divergent),
                        'registered': len(spellings)}
    if floors and summ['spelling']['tokens'] < MIN_TOKENS:
        findings.append(('EC-THIN-SPELL',
                         '只扫到 %d 个码形状的 token（下限 %d）—— 拼写检查塌了'
                         % (summ['spelling']['tokens'], MIN_TOKENS)))

    summ['contract'] = {'codes': len(contract_codes),
                        'per_doc': {k: len(summ['tables'][k].rows)
                                    if summ['tables'][k] is not None else 0
                                    for k, _ in DOCS},
                        'overlap': sum(1 for c in contract_codes
                                       if sum(1 for v in levels.values() if c in v) > 1)}
    summ['pending'] = {'registered': len(pending),
                       'matched': len([c for c in pending
                                       if c in contract_codes and c not in impl['codes']]),
                       'stale': len([c for c in pending
                                     if c in impl['codes'] or c not in contract_codes])}
    summ['impl_only'] = {'registered': len(impl_only),
                         'matched': len([c for c in impl_only if c in impl['codes']]),
                         'stale': len([c for c in impl_only if c not in impl['codes']])}
    return summ, findings


def report(summ, findings):
    print('root:            %s' % summ['root'])
    for key, rel in DOCS:
        table = summ['tables'][key]
        if table is None:
            print('%-4s table:      （没提取到）  %s' % (key, rel))
        else:
            print('%-4s table:      rows=%d  line=%d  类名列=%s  %s'
                  % (key, len(table.rows), table.line,
                     '有' if table.has_cls else '无', rel))
    impl = summ['impl']
    print('impl scan:       %s/*.py files=%d classes=%d codes=%d unparsable=%d'
          % (IMPL_DIR, len(impl['files']), len(impl['classes']),
             len(impl['codes']), len(impl['unparsable'])))
    c = summ['contract']
    print('contract side:   codes=%d（%s）重叠=%d'
          % (c['codes'], ', '.join('%s=%d' % (k.lower(), v)
                                   for k, v in sorted(c['per_doc'].items())), c['overlap']))
    print('pending:         registered=%d matched=%d stale=%d'
          % (summ['pending']['registered'], summ['pending']['matched'],
             summ['pending']['stale']))
    print('impl-only:       registered=%d matched=%d stale=%d'
          % (summ['impl_only']['registered'], summ['impl_only']['matched'],
             summ['impl_only']['stale']))
    s = summ['spelling']
    print('spelling:        tokens=%d normalized=%d divergent=%d registered=%d'
          % (s['tokens'], s['normalized'], s['divergent'], s['registered']))
    print('detectors:       %d（每个检查码在 --selftest/--falsify 里各有一个坏样本）'
          % len(DETECTORS))
    for code, message in findings:
        print('FINDING [%s] %s' % (code, message))
    if not findings:
        print('notes:           三份契约的码表与 %s/ 双向对齐（码 / 拼写 / 级别 / 类名）'
              % IMPL_DIR)
    print('verdict: %s (%d finding(s))' % ('PASS' if not findings else 'FAIL', len(findings)))


# ── 自测：夹具（**故意极小**，所以 floors=False；下限由 thin 那个样本单独证）──
FIX_CORE = '''# 夹具：主契约

### D. 错误码完整列表

| 错误码 | 级别 | 描述 | 建议措施 |
| --- | --- | --- | --- |
| STRATEGY_001 | ERROR | 策略 ID 重复 | 检查注册逻辑 |
| BACKTEST_001 | ERROR | 回测执行失败 | 检查回测配置 |
| RISK_001 | CRITICAL | 风控拦截 | 检查风控规则 |
| RISK_002 | CRITICAL | 全局熔断触发 | 检查总回撤是否超过阈值 |

**F4. 取不到就报错。** 一律抛看板异常（公开错误码 `DASHBOARD001`）。
'''

FIX_DC = '''# 夹具：数据中心契约

### 3.9 错误码

| 错误码 | 异常类 | 级别 | 描述 | 建议措施 |
| --- | --- | --- | --- | --- |
| DATA_001 | `DataNotAvailableError` | ERROR | 数据不可见 | 检查 available_date |
| DATA_002 | `FutureDataAccessError` | CRITICAL | 未来函数 | 立即中断 |
| DATA_003 | `DataQualityError` | WARNING | 质量校验未通过 | 看 dc_quality_issue |
'''

FIX_RISK = '''# 夹具：风控契约

### 3.7 错误码

> 与主契约码表衔接：`RISK_001`、`RISK_002` 已在主契约中定义，本契约补充 `RISK_003` 起。

| 错误码 | 级别 | 描述 | 建议措施 |
| --- | --- | --- | --- |
| `RISK_001` | CRITICAL | 风控拦截 | 检查风控规则（主契约原有） |
| `RISK_002` | CRITICAL | 全局熔断触发 | 检查总回撤（主契约原有） |
| `RISK_003` | CRITICAL | 单策略熔断触发 | 显式 resume_breaker |
'''

FIX_IMPL = '''"""夹具：异常定义。"""


class QuanAutoError(Exception):
    code = "QUAN_000"


class DataCenterError(QuanAutoError):
    code = "DATA_000"


class DataStoreError(DataCenterError):
    code = "DATA_008"


class DashboardError(QuanAutoError):
    """看板读不了报告（DASHBOARD_001）。"""

    code = "DASHBOARD_001"


class DataNotAvailableError(DataCenterError):
    code = "DATA_001"


class FutureDataAccessError(DataCenterError):
    code = "DATA_002"


class DataQualityError(DataCenterError):
    code = "DATA_003"


class DuplicateStrategyError(QuanAutoError):
    code = "STRATEGY_001"


class BacktestExecutionError(QuanAutoError):
    code = "BACKTEST_001"


class RiskInterceptError(QuanAutoError):
    code = "RISK_001"


class RiskBreakerTrippedError(QuanAutoError):
    code = "RISK_002"


class RiskSingleBreakerError(QuanAutoError):
    code = "RISK_003"
'''

# 夹具自己的登记表：条目要与 FIX_IMPL 对齐，否则样本会因为**登记漂移**而红，
# 让人误以为探测器坏了。真实仓库那两张表由 --falsify 与真实运行盯着。
FIX_IMPL_ONLY = {'QUAN_000': '夹具根哨兵码', 'DATA_000': '夹具根哨兵码',
                 'DATA_008': '夹具：实现侧追加', 'DASHBOARD_001': '夹具：看板码'}
FIX_SPELLINGS = {'DASHBOARD001': {'forms': ('DASHBOARD001', 'DASHBOARD_001'),
                                  'what': '夹具', 'reason': '夹具登记：以 DASHBOARD_001 为准'}}

SANDBOX = {'root': None}


def _lay_case(core=None, dc=None, risk=None, impl=None):
    root = SANDBOX['root']
    write_text(os.path.join(root, CORE_REL), FIX_CORE if core is None else core)
    write_text(os.path.join(root, DC_REL), FIX_DC if dc is None else dc)
    write_text(os.path.join(root, RISK_REL), FIX_RISK if risk is None else risk)
    write_text(os.path.join(root, IMPL_REL), FIX_IMPL if impl is None else impl)
    return root


def sample(name, expect=(), expect_clean=False, floors=False, core=None, dc=None,
           risk=None, impl=None, pending=None, impl_only=None, spellings=None,
           root=None):
    """一个样本 = 一个坏输入 + 断言报出的**正是**预期的那个检查码。

    expect 里允许多个码（同一次变异合法地触发多条判据时，把它们都写出来 ——
    隐藏其中一条会让这个样本变弱），但至少要有 expected 的那一条。
    """
    problems = []
    # 夹具的登记表是夹具的一部分：默认值不能用真实仓库那两张（它们描述的是真契约，
    # 拿它们去比夹具会让每个样本都因为**登记漂移**而红，看起来像探测器坏了）。
    if pending is None:
        pending = {}
    if impl_only is None:
        impl_only = FIX_IMPL_ONLY
    if spellings is None:
        spellings = FIX_SPELLINGS
    if root is None:
        _lay_case(core, dc, risk, impl)
        root = SANDBOX['root']
    _, findings = audit(root, floors=floors, pending=pending,
                        impl_only=impl_only, spellings=spellings)
    codes = [c for c, _ in findings]
    if expect_clean and findings:
        problems.append('期望 0 finding，实际 %d：%s'
                        % (len(findings), '; '.join('%s %s' % (c, m[:70])
                                                    for c, m in findings[:4])))
    for want in expect:
        if want not in codes:
            problems.append('没报出预期码 %s（实报 %s）' % (want, codes or '空'))
    print('  [%s] %s' % (name, 'ok' if not problems else 'SAMPLE FAIL'))
    if problems:
        for p in problems:
            print('      %s' % p)
        for c, m in findings:
            print('      got [%s] %s' % (c, m[:150]))
    return not problems


def selftest():
    harden_stdout()
    SANDBOX['root'] = tempfile.mkdtemp(prefix='error-codes-selftest-')
    print('sandbox: %s' % SANDBOX['root'])
    ok = True
    kw = {'impl_only': FIX_IMPL_ONLY, 'spellings': FIX_SPELLINGS, 'pending': {}}

    def S(name, **extra):
        nonlocal ok
        args = dict(kw)
        args.update(extra)
        ok = sample(name, **args) and ok

    # 干净样本放第一个：**防误报**。没有它，后面每个样本的「报出了预期码」都可能
    # 只是「什么都报」。
    S('clean-fixture', expect_clean=True)

    # 空转守卫四条：文件读不到 / 文档里没有那张表
    S('empty-root', expect=('EC-EMPTY-CORE', 'EC-EMPTY-DC', 'EC-EMPTY-RISK',
                            'EC-EMPTY-IMPL'),
      root=os.path.join(SANDBOX['root'], 'does-not-exist'))
    S('no-code-table', expect=('EC-EMPTY-CORE', 'EC-EMPTY-RISK'),
      core=FIX_CORE.split('| 错误码')[0], risk=FIX_RISK.split('| 错误码')[0])
    S('empty-impl', expect=('EC-EMPTY-IMPL',), impl='"""夹具：一个类都没有。"""\n')
    S('unparsable-impl', expect=('EC-IMPL-UNPARSABLE',),
      impl=FIX_IMPL + '\nclass Broken(:\n    pass\n')
    S('thin-extraction', expect=('EC-THIN-CORE', 'EC-THIN-IMPL'), floors=True)

    # 形状 / 间距 / 表数
    S('bad-code-shape', expect=('EC-CODE-SHAPE',),
      core=FIX_CORE.replace('| STRATEGY_001 |', '| STRATEGY001 |'))
    S('numbering-gap', expect=('EC-GAP-DC',),
      dc=FIX_DC.replace('| DATA_002 | `FutureDataAccessError` | CRITICAL | 未来函数 | 立即中断 |\n', ''))
    S('two-tables', expect=('EC-TABLE-RISK',),
      risk=FIX_RISK + '\n| 错误码 | 级别 | 描述 | 建议措施 |\n| --- | --- | --- | --- |\n'
                      '| `RISK_003` | ERROR | 重复的一张表 | 无 |\n')

    # 类名两侧
    S('class-missing', expect=('EC-CLASS-MISSING',),
      dc=FIX_DC.replace('`DataNotAvailableError`', '`DataUnavailableError`'))
    S('class-mismatch', expect=('EC-CLASS-MISMATCH',),
      impl=FIX_IMPL.replace('code = "DATA_003"', 'code = "DATA_099"'))

    # 两个方向 + 两张登记表
    S('contract-code-orphan', expect=('EC-CODE-ORPHAN',),
      impl=FIX_IMPL.replace('class DuplicateStrategyError(QuanAutoError):\n    code = "STRATEGY_001"\n\n', ''))
    S('stale-pending', expect=('EC-STALE-PENDING',), pending={'STRATEGY_001': '夹具：假装它还没实现'})
    S('code-invented', expect=('EC-CODE-INVENTED',),
      impl=FIX_IMPL + '\n\nclass MadeUpError(QuanAutoError):\n    code = "DATA_777"\n')
    S('impl-only-stale', expect=('EC-IMPL-ONLY-STALE',),
      impl_only=dict(FIX_IMPL_ONLY, **{'DATA_009': '夹具：登记了但实现里没有'}))

    # 级别 / 范围
    S('level-conflict', expect=('EC-LEVEL-CONFLICT',),
      risk=FIX_RISK.replace('| `RISK_002` | CRITICAL |', '| `RISK_002` | ERROR |'))
    S('risk-range-claims-001-002', expect=('EC-RISK-RANGE',),
      core=FIX_CORE.replace('| RISK_002 | CRITICAL | 全局熔断触发 | 检查总回撤是否超过阈值 |',
                            '| RISK_002 | CRITICAL | 全局熔断触发 | 检查总回撤是否超过阈值 |\n'
                            '| RISK_003 | ERROR | 单策略熔断 | 无 |'))

    # 拼写（含反向）
    S('spelling-drift', expect=('EC-SPELLING',),
      core=FIX_CORE + '\n另见公开错误码 `BACKTEST001`。\n')
    S('spelling-registry-stale', expect=('EC-SPELLING-STALE',),
      core=FIX_CORE.replace('`DASHBOARD001`', '`DASHBOARD_001`'))

    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 2


# ── 触发测试（对**真产物**的副本）：`--selftest` 用的是夹具，证明不了真仓库那条边 ──
def falsify(argv):
    harden_stdout()
    summ, findings = audit(ROOT)
    if findings:
        print('FALSIFY FAIL: 真仓库本身就不绿（%d finding(s)）—— 先修产物，'
              '在红的输入上做变异得不出任何结论' % len(findings))
        for code, message in findings:
            print('  [%s] %s' % (code, message[:150]))
        return 2
    print('baseline:        真仓库绿（codes=%d, 契约码=%d）'
          % (len(summ['impl']['codes']), summ['contract']['codes']))

    sandbox = tempfile.mkdtemp(prefix='error-codes-falsify-')
    print('sandbox:         %s' % sandbox)
    for rel in (CORE_REL, DC_REL, RISK_REL, IMPL_REL):
        dest = os.path.join(sandbox, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dest)

    base_summ, base_findings = audit(sandbox)
    if base_findings:
        print('FALSIFY FAIL: 副本与真仓库不一致（副本 %d finding(s)）' % len(base_findings))
        return 2
    print('copy baseline:   与真仓库逐项相同（codes=%d）—— 后面的变异打在等价输入上'
          % len(base_summ['impl']['codes']))

    def case(label, steps, want, clean=False):
        # 每一步都从真仓库重新铺，免得上一轮的残留污染下一轮（前一步的污染会让
        # 后一步的结论与被测文件无关）。这里**不需要**清 __pycache__：本工具只
        # `ast.parse`，从不 import 被测模块。
        for rel in (CORE_REL, DC_REL, RISK_REL, IMPL_REL):
            shutil.copyfile(os.path.join(ROOT, rel), os.path.join(sandbox, rel))
        for step in steps:
            rel, old, new = step[0], step[1], step[2]
            cnt = step[3] if len(step) > 3 else 1
            path = os.path.join(sandbox, rel)
            write_text(path, must(read_text(path), old, new, label, cnt))
        _, found = audit(sandbox)
        codes = [c for c, _ in found]
        good = (not found) if clean else all(w in codes for w in want)
        print('  [%s] findings=%d codes=%s%s' % (label, len(found), codes or '空',
                                                '' if good else '  <== 没达到预期 %s' % (want,)))
        if not good:
            for c, m in found:
                print('      got [%s] %s' % (c, m[:150]))
        return good

    ok = True
    ok = case('class-missing', [(DC_REL, '| DATA_003 | `DataQualityError` | WARNING |',
                                 '| DATA_003 | `DataQualityIssue` | WARNING |')],
              ('EC-CLASS-MISSING',)) and ok
    ok = case('level-conflict', [(RISK_REL, '| `RISK_002` | CRITICAL |', '| `RISK_002` | ERROR |')],
              ('EC-LEVEL-CONFLICT',)) and ok
    ok = case('code-invented+orphan', [(IMPL_REL, 'code = "DATA_003"', 'code = "DATA_099"')],
              ('EC-CODE-INVENTED', 'EC-CODE-ORPHAN')) and ok
    ok = case('impl-only-stale', [(IMPL_REL, 'code = "DATA_008"', 'code = "DATA_048"')],
              ('EC-IMPL-ONLY-STALE',)) and ok
    ok = case('numbering-gap+stale-pending',
              [(RISK_REL, '| `RISK_008` | ERROR | 熔断未恢复（拒绝隐式恢复） | 熔断不会因阈值放宽自动解除，须显式 `resume_breaker` |\n', '')],
              ('EC-GAP-RISK', 'EC-STALE-PENDING')) and ok
    ok = case('contract-code-orphan',
              [(RISK_REL, '| `RISK_011` | ERROR | 权益峰值缺失 | 重启后未加载 `risk_equity_peak`，回撤熔断将失效，立即检查 |\n',
                '| `RISK_011` | ERROR | 权益峰值缺失 | 重启后未加载 `risk_equity_peak`，回撤熔断将失效，立即检查 |\n'
                '| `RISK_012` | ERROR | 新加的码 | 无 |\n')],
              ('EC-CODE-ORPHAN',)) and ok
    # 登记的分歧**消失**时必须报 EC-SPELLING-STALE，否则这条登记就长成永久免检的挡箭牌。
    # 改哪一侧有讲究：这里是「把契约侧那处拼写改对」（＝ 有人终于修好了 F4 的错字）。
    # ⚠️ 只改实现侧**已经打不到这条分支**了 —— 主契约 G10 把正确拼写写进了正文（更正记录
    # 必须写清正确拼写），于是「两边都成了错拼写」这份情形在文件里已构造不出来。实测：
    # 只改实现侧时分歧依旧（G10 里那个正确拼写还在），报出来的是 EC-CODE-INVENTED +
    # EC-IMPL-ONLY-STALE —— 那个结果看着像「探测器根本没写」，实际是**变异没打到分支**，
    # 两者在报告里长得一模一样。
    # 次数写死 4（F4 两处 + G10 两处）：文档里再冒出第 5 处就响亮地失败，逼人重看一眼。
    ok = case('spelling-registry-stale',
              [(CORE_REL, 'DASHBOARD001', 'DASHBOARD_001', 4)],
              ('EC-SPELLING-STALE',)) and ok
    # 控制组：副本不动 ⇒ 必须 0 finding。没有它，上面每一条「报出了预期码」
    # 都可能是「副本本身就被这个变异碰坏了、什么都报」。
    ok = case('control-no-mutation', [], (), clean=True) and ok

    print('FALSIFY %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 2


def main(argv):
    harden_stdout()
    if '--selftest' in argv:
        return selftest()
    if '--falsify' in argv:
        return falsify(argv)
    rest = [a for a in argv if not a.startswith('-')]
    root = os.path.abspath(rest[0]) if rest else ROOT
    summ, findings = audit(root)
    report(summ, findings)
    return 0 if not findings else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
