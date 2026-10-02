#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""design-artifacts -- 图纸层（`docs/low fidelity/` + `docs/high fidelity/`）的门禁（tier A）。

它回的是一个具体问题：**这一层此前一条判据都没有。**
两份低保真产物 + 三份高保真产物在文档里长期被写成「没有牙」「不被任何门禁读取」
「不解除任何缺口」—— 前两句从 2026-10-02 起不再成立（本门禁读它们）；**第三句仍然成立**。

本门禁只主张下面这些事，多一件都不主张：

  1. 三份**手写**图纸（`docs/low fidelity/index.html` / `docs/low fidelity/prototype.html` /
     `docs/high fidelity/design.html`）**内部自洽**：`<use href="#x">` 指向的 `<symbol id=x>`
     真的存在、页内 `href="#x"` 的目标真的存在、`id` 不重复。
  2. 图纸的**骨架计数**与登记表（`tools/design-artifacts.json`）一致，**且这些计数在
     README 里逐字写对**（`DA-COUNT`：登记表 `expected` == 现测值，且按模板格式化出来的串
     **逐字**出现在指名的 README 里）—— 这是本仓库「文档会漂」那条老账的机器化。
  3. 「已交付 / 提案」的**边界**没被互串：`design.html` 里 `.frame` 十四个、`.proposal` 十三个、
     唯一 `delivered` 帧是 `id="f5"`；低保真追溯表里唯一被 `<b>` 标为已交付的是 F5。
  4. 两张图纸**互相对得上**：低保真追溯表每一行的「PRD §」数字序列，是高保真同编号帧
     `.f-trace` 里数字序列的**前缀**（前者写「这帧回哪几条 PRD」，后者把同一条线画得更全）；
     三份产物的**屏集**一致。
  5. F5 / F6 两帧的**内容真的来自上游**，不是手抄：F5 段必须按原序命中 `quanauto/dashboard.py`
     的指标标签，且那份渲染夹具里的展示串逐字在内；图纸里**七处「规则表面」**
     （F6 的两张表 / A3 托盘弹出面板 / F1 拦截原因 Top3 / §09 的两张手机列表 /
     M1 闸门摘要）必须与 `quanauto/risk.py` 的 `RULE_SPECS` + `RULE_DECISION` +
     `BLACKLIST_RULE_ID` **逐行逐列**相等（行序 / 单位 / 收紧方向 / 默认阈值 / 判定 / 严重度；
     名单型规则在图纸里是单独一张表，它的判定来自 `RiskViolation` 而不是 `RULE_SPECS`）。
     ⚠️ 「规则 id 在稿里出现过」**不构成**对拍 —— 那是加厚前的口径，稿子把行序、单位、
     方向、阈值、判定、严重度**六样里任意几样抄错**它都全绿（实测：把 `0.10` 改成 `0.11`
     它一句话都不说）。「名字出现过」当覆盖判据是本仓库被咬过多次的那口井。
  6. 手机屏那两处的 `.ph-lab` **自己报的条数**与它下面那张列表的行数一致（`DA-RULE-COUNT`）——
     标签是一句**声明**，声明与产物是两件事，它们之间要有一条判据。
  7. `docs/high fidelity/dashboard.html` 是**派生文件**：它与 `tools/gen_high_fidelity.py`
     现算的字节一致（`DA-FRESH`），不新鲜就报。

它**不**主张、也测不到的事（写在这里是为了别让下一个人把它读大）：

  * **布局 / 视觉**照旧零覆盖 —— 本门禁一行几何都不查（静态文本里几何量恒为空）。
  * 三份手写图纸**没有生成器**，所以它们**画了什么、画得像不像**无人管；本门禁只查骨架、
    「已交付 / 提案」的标记、跨图纸一致性，以及 F5 / F6 两帧有没有抄错上游。
  * **不判图纸没画出来的列**：M1 / M2 把「单位 / 方向 / 严重度」丢在竖屏之外（那是排版决定，
    不是抄错）；F1 的 Top3 与 M1 的闸门摘要只**摘一段**（`subset` 口径：只逐行判对错 +
    id 不重复，条数与行序不判）。⇒「没判」和「判过了」是两件不同的事，这里写清是前者。
  * **不判 `—` 与「含义」列**：`—` 是「本格没有真值」的显式占位（名单型规则的阈值就是这样，
    竖屏那两处也在用它）；`含义` 列在图纸里印的是**缩写**（`单策略回撤熔断阈值`），
    而上游 `RuleSpec.description` 是带算式的长文本 ⇒ 那一列不是可对拍的值，
    拿它对拍等于拿「散文抄没抄对」当判据。
  * `platform/` 那几条门禁一条都没碰，**也不因此构成平台层开工**（裁决 Q3 仍有效）。
  * 图纸里的 `proposal` 帧**仍然是提案**。`DA-FRAME-STATUS` 恰恰是在**钉死**这一点，
    不是在夸它们已交付。

⚠️ 别在这里抄计数（`GATE-COUNT` 禁的事）：本文件的计数一律从登记表现取，README 那边的数由
`DA-COUNT` 逐条对拍。改计数只改登记表 + README，别改这个文件。

本门禁**只读源码**：不起服务、不打开页面、不启动 JVM、不跑 npm / mvn、不走网络。
唯一的例外是 `DA-FRESH` 会在内存里调一次 `tools/gen_high_fidelity.py` 的 `build()`
（实测无副作用、不写盘）；为防它哪天变成有副作用的，`collect()` 在调用前后对
`docs/high fidelity/dashboard.html` 的字节各取一次指纹，变了就报出来。

⚠️ `DA-FRESH` 的字节比较前会把「真源提交」后面那个短 hash **两侧一起归一化**：git 的 `%h`
长度依赖本机对象库（`core.abbrev=auto`），拿它当判据就是「判据依赖环境」——本仓库已被这个
坑咬过一次（`.venv/` 那次：本机绿、克隆红）。归一化只吃掉那一个 token；**声明「有 / 没有
未提交改动」的那句散文不在豁免范围内**，所以真源一改、产物不重生成，它照红。

退出码：0 = 无 issue；1 = 有 issue（或输入缺失）；2 = `--selftest` 里自己的样本没打到靶子。
"""

import ast
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_REL = 'tools/design-artifacts.json'
GENERATOR_REL = 'tools/gen_high_fidelity.py'

DASH = '\u2014'

TAG_RE = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>')
CLASS_RE = re.compile(r'class="([^"]*)"')
ID_ATTR_RE = re.compile(r'(?<![-\w])id="([^"]*)"')
TITLE_ATTR_RE = re.compile(r'(?<![-\w])title="([^"]*)"')
HAS_DISABLED_RE = re.compile(r'(?<![-\w])disabled(?![-\w])')
HREF_RE = re.compile(r'(?<![-\w])href="#([^"]*)"')
USE_RE = re.compile(r'<use\b[^>]*href="#([^"]*)"')
SYMBOL_RE = re.compile(r'<symbol\b[^>]*\bid="([^"]+)"')
TD_RE = re.compile(r'<td\b[^>]*>(.*?)</td>', re.S)
TAG_STRIP_RE = re.compile(r'<[^>]+>')
SPAN_TAG_RE = re.compile(r'<(/?)span\b[^>]*>')
FTRACE_RE = re.compile(r'<span class="f-trace">')
NUM_RE = re.compile(r'\d+(?:\.\d+)?')
PLACEHOLDER_RE = re.compile(r'\[([^\[\]\n]+)\]')
IDENT_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
HASH_RE = re.compile(r'(真源提交 <code>)[0-9a-f]{7,40}(</code>)')
CSS_SECTION_RE = re.compile(r'/\* ={4,} \d\d . ')
MEDIA_RE = re.compile(r'@media\s*\(', re.M)
TR_RE = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.S)
CELL_RE = re.compile(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', re.S)
WF_TABLE_RE = re.compile(r'<table class="wf">')
TABLE_RE = re.compile(r'<table\b[^>]*>(.*?)</table>', re.S)
PH_LAB_RE = re.compile(r'<div class="ph-lab">(.*?)</div>', re.S)
PH_ROW_RE = re.compile(r'<div class="ph-row">(.*?)</div>', re.S)
NM_RE = re.compile(r'<span class="nm">(.*?)</span>', re.S)
TH_RE = re.compile(r'<span class="th">(.*?)</span>', re.S)
BADGE_RE = re.compile(r'<span class="badge[^"]*">(.*?)</span>', re.S)
DECLARED_RE = re.compile(r'(\d+)\s*条')

#: `.ph-row` 里三种格子的抽取器 —— `RULE_SURFACES[...]['cols']` 的值指向这里的键。
#: 单列成一张表（而不是在解析里写 if/else）是为了让「再加一种格子」只改一处。
PH_TOKENS = {'nm': NM_RE, 'badge': BADGE_RE, 'th': TH_RE}

STRIP_COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
STRIP_STYLE_RE = re.compile(r'<style\b.*?</style>', re.S | re.I)
STRIP_SCRIPT_RE = re.compile(r'<script\b.*?</script>', re.S | re.I)

#: 上游的收紧方向枚举 → 图纸上印的形态。⚠️ 这张表**不是**近似：若上游出现一个表里没有的
#: 枚举值，判据**拒绝通过**而不是跳过那一格 —— 跳过等于静默放行一个新方向。
DIRECTION_TEXT = {'DECREASE': '收紧 \u2193'}

#: 图纸里的「规则表面」——**每一处**在稿子里都声称「这里的规则取自仓库真值」。
#: 判据跟着每一处**真正画出来的列**走（`cols`），不跟着「本可以画的列」走。
#: `locate` 是**内容**定位键（表头必须同时含这几格 / `.ph-lab` 文本必须含这个串），
#: 不是行号也不是「第几张表」—— 行号一插内容就漂，而漂了没人报。
#: `expect`：`all` = `RULE_SPECS` 全部（按行序）· `all+blacklist` = 六条数值型 + 名单型 ·
#: `blacklist` = 只有名单型那一条 · `subset` = 摘一段（逐行判对错 + id 不重复）。
#: ⚠️ 往 `cols` 里加一列 = 那一列**从此逐格被对拍**；不加 = 那一列**没人管**（写在这里，
#: 好让下一个人知道「没判」是选择，不是遗漏）。
RULE_SURFACES = (
    {'key': 'f6-num', 'name': 'F6 数值型规则表', 'kind': 'table',
     'locate': ('规则 id', '默认阈值'),
     'cols': {'rule_id': '规则 id', 'unit': '单位', 'direction': '方向',
              'threshold': '默认阈值', 'action': '判定', 'severity': '严重度'},
     'expect': 'all'},
    {'key': 'f6-list', 'name': 'F6 名单型规则表', 'kind': 'table',
     'locate': ('规则 id', '类型'),
     'cols': {'rule_id': '规则 id', 'action': '判定'}, 'expect': 'blacklist'},
    {'key': 'a3-panel', 'name': 'A3 托盘弹出面板', 'kind': 'table',
     'locate': ('规则', '判定'),
     'cols': {'rule_id': '规则', 'action': '判定'}, 'expect': 'all+blacklist'},
    {'key': 'f1-top3', 'name': 'F1 拦截原因 Top3', 'kind': 'table',
     'locate': ('命中规则', '裁决'),
     'cols': {'rule_id': '命中规则', 'action': '裁决'}, 'expect': 'subset'},
    {'key': 'm2-num', 'name': 'M2 手机竖屏·数值型', 'kind': 'phlist', 'locate': '数值型规则',
     'cols': {'rule_id': 'nm', 'action': 'badge', 'threshold': 'th'}, 'expect': 'all'},
    {'key': 'm2-list', 'name': 'M2 手机竖屏·名单型', 'kind': 'phlist', 'locate': '名单型规则',
     'cols': {'rule_id': 'nm', 'action': 'badge'}, 'expect': 'blacklist'},
    {'key': 'm1-summary', 'name': 'M1 手机竖屏·闸门摘要', 'kind': 'phlist', 'locate': '闸门摘要',
     'cols': {'rule_id': 'nm', 'action': 'badge', 'threshold': 'th'}, 'expect': 'subset'},
)


class InputMissing(Exception):
    """判据要读的文件不在 —— 这是 setup 故障，不是产物缺陷。"""


def afold(s):
    """打印前把非 ASCII 折成 '.'：这台控制台是 cp936，中文与 U+2212 都会炸。"""
    return re.sub(r'[^\x20-\x7e]', '.', s)


def lf(raw):
    """读入即归一化 CRLF。裸 `\\n` 正则在 CRLF 上**静默失配**（本仓库踩过：提取 0 字符却全绿）。"""
    return raw.decode('utf-8').replace('\r\n', '\n')


def read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def repo_path(root, rel):
    return os.path.join(root, *rel.split('/'))


# ---------------------------------------------------------------- HTML 小工具

def elems(text):
    """把所有开始标签拆成 (tag, classes, id, attrs, pos)。属性里的 `>` 由引号组兜住。"""
    out = []
    for m in TAG_RE.finditer(text):
        attrs = m.group(2)
        cm = CLASS_RE.search(attrs)
        im = ID_ATTR_RE.search(attrs)
        out.append({'tag': m.group(1).lower(),
                    'classes': cm.group(1).split() if cm else [],
                    'id': im.group(1) if im else None,
                    'attrs': attrs,
                    'pos': m.start()})
    return out


def span_at(text, start):
    """从某个 `<span` 的起点取回**配平的整个元素**。

    不能用 `<span class="f-trace">(.*?)</span>`：f-trace 里面还嵌着 `.tr-pill` 那种内层
    span，非贪婪会在内层 `</span>` 上截断，把尾部的 `§N` 数字丢掉（而那正是本判据要读到的东西）。
    """
    depth = 0
    for m in SPAN_TAG_RE.finditer(text, start):
        if m.group(1) == '':
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return text[start:m.end()]
    return text[start:]


def strip_body(text):
    """剥掉注释 / 样式 / 脚本，只留正文 —— 方括号占位符按这个口径数。"""
    s = STRIP_COMMENT_RE.sub('', text)
    s = STRIP_STYLE_RE.sub('', s)
    return STRIP_SCRIPT_RE.sub('', s)


def elem_span(text, start, tag):
    """从某个 `<tag` 的起点取回**配平的整个元素**（按同名标签计数）。

    `span_at` 是给 `<span class="f-trace">` 用的，里面藏着内层 span；`.ph-list` 是同一个
    问题的另一副面孔（里面藏着 `.ph-row`）。非贪婪 `(.*?)</div>` 会在第一个内层 `</div>`
    上截断，于是「这张列表有几行」永远是 1 行 —— 而那正是判据要读到的东西。
    """
    open_re = re.compile(r'<%s\b[^>]*>' % tag)
    close_re = re.compile(r'</%s>' % tag)
    depth = 0
    i = start
    while i < len(text):
        o = open_re.search(text, i)
        c = close_re.search(text, i)
        if c is None:
            return text[start:]
        if o is not None and o.start() < c.start():
            depth += 1
            i = o.end()
        else:
            depth -= 1
            i = c.end()
            if depth == 0:
                return text[start:i]
    return text[start:]


def rule_tables(text):
    """把每张表解析成 `{'start', 'header': [...], 'rows': [[...]]}`。

    表头与每格都**剥掉标签取文本**：图纸里这些格子常常包着 `<span class="badge">` /
    `<span class="th">`，拿原始 HTML 去比会把「判定」列全判错（而且错得很有说服力）。
    """
    out = []
    for m in TABLE_RE.finditer(text):
        rows = TR_RE.findall(m.group(1))
        if len(rows) < 2:
            continue
        out.append({'start': m.start(),
                    'header': [TAG_STRIP_RE.sub('', c).strip() for c in CELL_RE.findall(rows[0])],
                    'rows': [[TAG_STRIP_RE.sub('', c).strip() for c in CELL_RE.findall(r)]
                             for r in rows[1:]]})
    return out


def ph_list_after(text, needle):
    """紧跟某个 `.ph-lab` 标签之后的那个 `.ph-list`（手机屏的行集合）。

    返回 `(标签文本, 列表块, 未剥标签的列表块, 块起点)`；找不到返回 `None` —— 由调用方报
    `DA-EXTRACT`（**不跳过**：一处表面定位不到就是那一处没有判据，「没有」与「判过了」
    不能长得一样）。第三个元素是**带标签**的块：判据取的是人眼看到的文本，但自测要往里面
    做变异，剥过标签的原字符串没法写回去。
    """
    for m in PH_LAB_RE.finditer(text):
        label = TAG_STRIP_RE.sub('', m.group(1)).strip()
        if needle not in label:
            continue
        j = text.find('<div class="ph-list">', m.end())
        if j < 0:
            return None
        raw = elem_span(text, j, 'div')
        return label, raw, j
    return None


def declared_count(label):
    """`.ph-lab` 自称「下面那张列表有几行」。

    ⚠️ 取**最后一个** `N 条`，不取第一个：M1 的标签是「闸门摘要 · 7 条里的 4 条」——
    第一个数（7）是**规则总数**，最后一个（4）才是「这张列表露了几条」。取错一个，
    一条**正确的**标签会被判红（那比漏报更糟：它教人放宽判据）。
    """
    ms = DECLARED_RE.findall(label or '')
    return int(ms[-1]) if ms else None


def frame_spans(text):
    """`.frame` 的**块**（从自己的开始标签到下一个 `.frame` 的开始标签）。

    F1~F8 是 `<article class="frame ..." id="fN">`，彼此是兄弟、不互相嵌套 ——
    这一点由 `DA-FRAME-STATUS` 顺带钉住（每帧的块里必须**恰好**一个 `.f-trace`）。
    """
    starts = [e for e in elems(text) if 'frame' in e['classes']]
    out = []
    for i, e in enumerate(starts):
        end = starts[i + 1]['pos'] if i + 1 < len(starts) else len(text)
        out.append({'id': e['id'], 'classes': e['classes'], 'text': text[e['pos']:end],
                    'span': (e['pos'], end)})
    return out


def trace_digits(block):
    """帧块里 `.f-trace` 内按文档序的全部数字（内层 span 的文本也算）。"""
    m = FTRACE_RE.search(block)
    if not m:
        return None
    return NUM_RE.findall(TAG_STRIP_RE.sub('', span_at(block, m.start())))


def parse_lowfi_trace(text, section_id):
    """低保真「追溯表」逐行解析，返回每行的帧号 / PRD 数字 / 是否已交付 / PRD 格的绝对区间。

    列位置**按表头文本定位**，不写死下标（表列一重排，写死的下标会静默指错列）。
    """
    i = text.find('<h2 id="%s"' % section_id)
    if i < 0:
        return []
    j = WF_TABLE_RE.search(text, i)
    if not j:
        return []
    end = text.find('</table>', j.start())
    if end < 0:
        return []
    # ⚠️ 元组第二个元素必须是 **body 的绝对起点**（= j.start() + m.start(1)），不是 <tr> 匹配的
    # **终点**：下面每个单元格的偏移都是相对 body 算的，加到 `m.end()`（`</tr>` 之后）上会整体
    # 落到**下一行**去（实测差一整行 ≈192 字符）—— 于是 `prd_span` 指到下一行的格子上，
    # 自测的变异把整行切坏、表行从 8 变 7（这一条是靠自测抓到的）。
    rows = [(j.start() + m.start(), j.start() + m.start(1), m.group(1))
            for m in TR_RE.finditer(text[j.start():end])]
    if not rows:
        return []
    hdr_cells = [(rows[0][1] + m.start(1), rows[0][1] + m.end(1), m.group(1))
                 for m in CELL_RE.finditer(rows[0][2])]
    idx = {}
    for k, (_, _, raw) in enumerate(hdr_cells):
        plain = TAG_STRIP_RE.sub('', raw)
        # 表头是「帧 / 主要回答什么问题 / PRD 追溯 / 路线图阶段 / 交付状态」。
        # 写成「编号」会**一条都取不到**（实测：0 行 ⇒ 后面整条链空转），所以两样都认。
        if '帧' in plain or '编号' in plain:
            idx['fid'] = k
        elif 'PRD' in plain:
            idx['prd'] = k
        elif '交付' in plain:
            idx['status'] = k
    if len(idx) != 3:
        return []
    out = []
    for _, bstart, body in rows[1:]:
        cells = [(bstart + m.start(1), bstart + m.end(1), m.group(1))
                 for m in CELL_RE.finditer(body)]
        if len(cells) <= max(idx.values()):
            continue
        fm = re.search(r'F\d+', TAG_STRIP_RE.sub('', cells[idx['fid']][2]))
        if not fm:
            continue
        out.append({'fid': fm.group(0),
                    'prd': NUM_RE.findall(TAG_STRIP_RE.sub('', cells[idx['prd']][2])),
                    'prd_span': (cells[idx['prd']][0], cells[idx['prd']][1]),
                    'delivered': '<b>' in cells[idx['status']][2]})
    return out


# ---------------------------------------------------------------- 上游（只解析源码）

def _literal_assign(src, name):
    """取一个模块级赋值的 AST 节点。**不 import** —— 读常量用 import 等于执行被测文件。

    `Assign` 与 `AnnAssign` 都要认：本仓库的 `METRIC_SPECS` / `RULE_SPECS` 都带类型注解
    （`METRIC_SPECS: Tuple[MetricSpec, ...] = (...)`），只认 `Assign` 会**提取到 0 条**。
    """
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return node.value
            continue
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
    return None


def _call_first_args(node, fname):
    """`NAME = (F(...), F(...))` 里每个 `F(...)` 的**第一个位置参**的常量值。"""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return []
    out = []
    for elt in node.elts:
        if (isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name)
                and elt.func.id == fname and elt.args
                and isinstance(elt.args[0], ast.Constant)):
            out.append(elt.args[0].value)
    return out


def metric_labels(src):
    """`quanauto/dashboard.py` 的 `METRIC_SPECS` 标签（第三个位置参）。"""
    node = _literal_assign(src, 'METRIC_SPECS')
    if not isinstance(node, (ast.Tuple, ast.List)):
        return []
    out = []
    for elt in node.elts:
        if (isinstance(elt, ast.Call) and elt.args and len(elt.args) >= 3
                and isinstance(elt.args[2], ast.Constant)):
            out.append(elt.args[2].value)
    return out


def rule_ids(src):
    """`quanauto/risk.py` 的规则 id（`RULE_SPECS` 每个 `RuleSpec(...)` 的第一个位置参）。

    `GLOBAL_RULE_IDS` 是同一份清单的顺序副本，读 `RULE_SPECS` 等于读它的源头。
    """
    return _call_first_args(_literal_assign(src, 'RULE_SPECS'), 'RuleSpec')


def _enum_members(src, name):
    """某个枚举类的成员名集合。

    用来把「裁决动作」钉在**真的枚举成员**上。不查这个的话，判据比的就只是「字符串相等」
    —— `REJECT` 到底是不是本仓库的一个合法裁决动作，它根本不知道。
    """
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            for st in node.body:
                if isinstance(st, ast.Assign):
                    out.update(t.id for t in st.targets
                               if isinstance(t, ast.Name) and t.id.isupper())
    return out


def _const_or_attr(node):
    """`0.10` / `'RATIO'` 这类常量，或 `RuleUnitEnum.RATIO` 这类**枚举成员访问**。

    ⚠️ 上游的 `unit` / `direction` / 裁决动作都是 `Enum` 成员（`ast.Attribute`）而不是字面量 ——
    只认 `ast.Constant` 的话 `RULE_SPECS` 一条都抽不到（实测 `specs=0`），而「抽到 0 条」在报告里
    长成 `specs=0` 这一串数字，**不**是一眼可见的错误（下面的空转守卫才是接住它的那条网）。
    取 `.attr`（成员名 `RATIO` / `DECREASE` / `REDUCE`）而不是反解源码：
    前者与图纸上印的字对齐，后者会带上 `RuleUnitEnum.` 前缀。
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def rule_model(src):
    """`quanauto/risk.py` 的规则真值：id / 单位 / 收紧方向 / 默认阈值 + 判定 + 严重度 + 名单型。

    ⚠️ 上游事实有**两条来路**：`RULE_SPECS`（数值口径）与 `RULE_DECISION` +
    `BLACKLIST_RULE_ID`（裁决口径）。图纸的「判定」「严重度」两列抄的是后者 —— 加厚前
    这一路上游**连读都没读**，于是「抄错判定」与「抄对判定」在报告里长得一模一样。

    阈值按**原样常量**交给判据（上游就是 `0.10` / `3`，图纸印的也是这两个形态），
    比较时按数值比：字符串比会把 `3` 与 `3.0` 判成不一致，而它俩说的是同一件事。

    名单型的裁决动作取自真正的构造点
    （`RiskViolation(rule_id=BLACKLIST_RULE_ID, …, action=RiskActionEnum.REJECT, …)`），
    不是抄自 `RULE_DECISION`（那张表里没有它）。

    返回 `{'specs': [{'rule_id','unit','direction','threshold'}, …],
          'decisions': {rule_id: (action, severity)}, 'blacklist': (id, action) or None}`。
    """
    specs = []
    node = _literal_assign(src, 'RULE_SPECS')
    if isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            if not (isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name)
                    and elt.func.id == 'RuleSpec' and len(elt.args) >= 5):
                continue
            a = elt.args
            vals = {name: _const_or_attr(a[k])
                    for k, name in ((0, 'rule_id'), (2, 'unit'),
                                    (3, 'direction'), (4, 'threshold'))}
            if any(v is None for v in vals.values()):
                continue
            specs.append(vals)

    decisions = {}
    dn = _literal_assign(src, 'RULE_DECISION')
    if isinstance(dn, ast.Dict):
        for k, v in zip(dn.keys, dn.values):
            # ⚠️ 键必须是**字符串常量**：本文件里还有一个 `{RiskActionEnum.PASS: 0, …}` 形状的
            # 顺序字典（它的键是 Attribute）—— 不卡这一条，两张表会混进同一本账。
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Tuple) and len(v.elts) == 2):
                continue
            pair = tuple(_const_or_attr(e) for e in v.elts)
            if None not in pair:
                decisions[k.value] = pair

    black = None
    bn = _literal_assign(src, 'BLACKLIST_RULE_ID')
    if isinstance(bn, ast.Constant):
        actions = _enum_members(src, 'RiskActionEnum')
        found = []
        for call in ast.walk(ast.parse(src)):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == 'RiskViolation'):
                continue
            kw = {w.arg: w.value for w in call.keywords}
            rid, act = kw.get('rule_id'), kw.get('action')
            if (isinstance(rid, ast.Name) and rid.id == 'BLACKLIST_RULE_ID'
                    and isinstance(act, ast.Attribute) and act.attr in actions):
                found.append(act.attr)
        # 一个名单型规则该只有**一个**裁决动作；多了就是上游自己没说清，这时宁可判「提取不到」
        # （判据会报 DA-EXTRACT），也不耍小聪明挑一个（挑错了没人看得出来）。
        uniq = sorted(set(found))
        black = (bn.value, uniq[0] if len(uniq) == 1 else None)
    return {'specs': specs, 'decisions': decisions, 'blacklist': black}


def load_generator(root):
    """加载 `tools/gen_high_fidelity.py`。

    它自己带 `__main__` 守卫（本轮实测过），所以 exec 它不会顺手把产物写一遍。
    但那是个**外部前提**，不能靠信任：这里显式断言守卫在，不在就抛 —— 否则一门「只读」
    的门禁会在真实仓库上真的写一次盘（本仓库被同族问题咬过：读常量那行把清理脚本跑了一遍）。
    """
    path = repo_path(root, GENERATOR_REL)
    with open(path, 'r', encoding='utf-8') as fh:
        src = fh.read()
    if 'if __name__ ==' not in src:
        raise InputMissing('%s has no __main__ guard; loading it would execute it' % GENERATOR_REL)
    spec = importlib.util.spec_from_file_location('_design_artifacts_gen', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- I/O 层

def load_registry(root):
    with open(repo_path(root, REGISTRY_REL), 'r', encoding='utf-8') as fh:
        return json.load(fh)


def collect(root, reg):
    """读盘 + 现算。这是**唯一**的 I/O 层；判据全部在 judge() 里对纯数据做。"""
    art = {'root': root, 'text': {}, 'bytes': {}, 'probes': {}, 'stats': {},
           'notes': [], 'board_error': None, 'board_expected': None, 'docs': {}}
    for key in sorted(reg['artifacts']):
        rel = reg['artifacts'][key]
        path = repo_path(root, rel)
        if not os.path.exists(path):
            raise InputMissing(rel)
        raw = read_bytes(path)
        art['bytes'][rel] = raw
        art['text'][rel] = lf(raw)
        art['stats'][key + '_bytes'] = len(raw)

    # 登记表点名的 README 也在这里读进来：judge() 是**纯函数**，不许自己开文件。
    # （否则 DA-COUNT 的文档侧在自测里根本造不出样本：内存里的改动看不见。）
    for rel in sorted({c['doc'] for c in reg.get('claims', [])}):
        path = repo_path(root, rel)
        if os.path.exists(path):
            art['docs'][rel] = lf(read_bytes(path))

    up = reg['upstream']
    for rel in [up['metrics'], up['rules'], up['fixture'], up['styles']]:
        if not os.path.exists(repo_path(root, rel)):
            raise InputMissing(rel)

    with open(repo_path(root, up['metrics']), 'r', encoding='utf-8') as fh:
        art['metric_labels'] = metric_labels(fh.read())
    with open(repo_path(root, up['rules']), 'r', encoding='utf-8') as fh:
        rules_src = fh.read()
    art['rule_ids'] = rule_ids(rules_src)
    art['rule_model'] = rule_model(rules_src)
    with open(repo_path(root, up['fixture']), 'r', encoding='utf-8') as fh:
        fx = json.load(fh)
    art['fixture_texts'] = [m.get('text') for m in fx.get('metrics', []) if isinstance(m, dict)]
    art['styles_raw'] = read_bytes(repo_path(root, up['styles']))
    art['styles_text'] = lf(art['styles_raw'])

    board = reg['artifacts']['board']
    before = read_bytes(repo_path(root, board))
    gen = load_generator(root)
    try:
        doc, checks = gen.build()
        art['board_expected'] = doc.replace('\n', '\r\n').encode('utf-8')
        art['board_checks'] = checks
    except Exception as exc:                      # noqa: BLE001 - 生成器坏了要报，不要崩
        art['board_error'] = '%s: %s' % (type(exc).__name__, exc)
    after = read_bytes(repo_path(root, board))
    if before != after:
        art['notes'].append('WARNING: %s changed while build() ran -- the generator is not '
                            'side-effect free any more, DA-FRESH cannot be trusted' % board)
    return art


# ---------------------------------------------------------------- 现测（纯函数）

def rule_surfaces(text):
    """把登记的每一处「规则表面」解析成 `{'label','rows','raw','span','why'}`。

    ⚠️ 定位靠**内容签名**（表头必须同时含这几格 / `.ph-lab` 文本必须含这个串），不靠行号，
    也不靠「第几张表」：行号一插内容就漂，而漂了没人报（本仓库被咬过多次）。
    ⚠️ 定位不到时**返回空行**而不是 raise：判据那边把它记成 `DA-EXTRACT` ——
    「提取为空」必须判 FAIL，否则后面所有探测项都在空转却打印 PASS（本仓库的老账）。
    """
    out = {}
    tables = rule_tables(text)
    for surf in RULE_SURFACES:
        rec = {'label': None, 'rows': [], 'raw': '', 'span': None, 'why': ''}
        if surf['kind'] == 'table':
            for tb in tables:
                if all(h in tb['header'] for h in surf['locate']):
                    idx = {k: tb['header'].index(v) for k, v in surf['cols'].items()}
                    rows = []
                    for r in tb['rows']:
                        if max(idx.values()) >= len(r):
                            continue
                        rows.append({k: r[i] for k, i in idx.items()})
                    s = tb['start']
                    raw = elem_span(text, s, 'table')
                    rec.update({'rows': rows, 'raw': raw, 'span': (s, s + len(raw))})
                    break
            if not rec['rows']:
                rec['why'] = 'no <table> in design.html has all of the header(s) %s' % (surf['locate'],)
        else:
            got = ph_list_after(text, surf['locate'])
            if got:
                label, raw, j = got
                rows = []
                for rm in PH_ROW_RE.finditer(raw):
                    # ⚠️ 行字典必须按**逻辑列名**（rule_id / action / threshold）建键：
                    # `cols` 的值只是**在图纸里长什么样**（nm / badge / th），判据那边是按逻辑名
                    # 取的。按 token 建键的话，`m2-list` 的 badge 永远读到空串 —— 而且报出来的是
                    # 「图纸写的是 ''」，看起来像图纸的错，不是判据的错。
                    cells = {}
                    for col, token in surf['cols'].items():
                        hit = PH_TOKENS[token].search(rm.group(1))
                        cells[col] = TAG_STRIP_RE.sub('', hit.group(1)).strip() if hit else ''
                    rows.append(cells)
                rec.update({'label': label, 'rows': rows, 'raw': raw,
                            'span': (j, j + len(raw))})
            if not rec['rows']:
                rec['why'] = 'no .ph-list right after a .ph-lab containing %r' % (surf['locate'],)
        out[surf['name']] = rec
    return out


def measure(art, reg):
    A = reg['artifacts']
    hi, lo, pr, bo = A['high'], A['low'], A['proto'], A['board']
    p = art['probes']

    # ---- 高保真 design.html
    t = art['text'][hi]
    el = elems(t)
    p['high_bytes'] = len(art['bytes'][hi])
    p['high_lines'] = len(t.splitlines())
    p['high_frames'] = sum(1 for e in el if 'frame' in e['classes'])
    p['high_proposal'] = sum(1 for e in el if 'proposal' in e['classes'])
    p['high_delivered_ids'] = sorted(e['id'] or '(no-id)' for e in el if 'delivered' in e['classes'])
    p['high_sections'] = sum(1 for e in el if e['tag'] == 'section')
    p['high_pills'] = sum(1 for e in el if 'pill' in e['classes'])
    p['high_buttons'] = sum(1 for e in el if e['tag'] == 'button')
    p['high_disabled'] = sum(1 for e in el if HAS_DISABLED_RE.search(e['attrs']))
    p['high_clickable'] = sum(1 for e in el if e['tag'] == 'button'
                              and not HAS_DISABLED_RE.search(e['attrs']))
    p['high_disabled_no_title'] = sum(1 for e in el
                                      if HAS_DISABLED_RE.search(e['attrs'])
                                      and not TITLE_ATTR_RE.search(e['attrs']))
    p['high_css_sections'] = len(CSS_SECTION_RE.findall(t))
    p['high_style'] = len(re.findall(r'<style\b', t))
    p['high_script'] = len(re.findall(r'<script\b', t))
    p['high_link'] = len(re.findall(r'<link\b', t))
    p['high_external_urls'] = len(re.findall(r'https?://', t))
    p['high_fontface'] = len(re.findall(r'@font-face', t))
    p['high_media'] = len(MEDIA_RE.findall(t))
    sym = SYMBOL_RE.findall(t)
    use = USE_RE.findall(t)
    p['high_symbols'] = len(sym)
    p['high_uses'] = len(use)
    p['high_use_missing'] = len(set(use) - set(sym))
    p['high_unused_symbols'] = sorted(set(sym) - set(use))
    ids = [e['id'] for e in el if e['id']]
    p['high_ids'] = len(ids)
    p['high_dup_ids'] = sorted({i for i in ids if ids.count(i) > 1})
    p['high_dup_ids_len'] = len(p['high_dup_ids'])
    p['high_id_set'] = set(ids)
    hrefs = HREF_RE.findall(t)
    p['high_anchor_missing'] = sorted(set(hrefs) - set(ids))
    p['high_anchor_count'] = len(hrefs)
    # ⚠️ 原型用 `href="#/f1"` 这种 **hash 路由**（`#` 后面带斜杠），它指向的是 JS 的屏幕
    # 切换而不是元素 id。把它算进「锚点必须有目标」会报 9 条假 FINDING（实测）。
    links = [m.group(1) for m in re.finditer(r'<a\b[^>]*href="#([^"]*)"', t)]
    p['high_anchor_links'] = len(links)
    p['high_route_links'] = [h for h in links if '/' in h]
    p['high_anchor_links_plain'] = len([h for h in links if '/' not in h])
    p['high_unused_count'] = len(p['high_unused_symbols'])
    cells = [TAG_STRIP_RE.sub('', m.group(1)) for m in TD_RE.finditer(t)]
    p['high_td'] = len(cells)
    p['high_dash_contains'] = sum(1 for c in cells if DASH in c)
    p['high_dash_only'] = sum(1 for c in cells if c.strip() == DASH)
    body = strip_body(t)
    ph = PLACEHOLDER_RE.findall(body)
    p['high_bracket_places'] = len(ph)
    p['high_bracket_kinds'] = len(set(ph))
    p['high_bracket_n'] = ph.count('n')
    phw = PLACEHOLDER_RE.findall(t)
    p['high_bracket_whole_kinds'] = len(set(phw))
    p['high_bracket_whole_places'] = len(phw)

    frames = frame_spans(t)
    by_id = {}
    for fr in frames:
        if fr['id']:
            by_id[fr['id']] = fr
    p['high_frame_ids'] = sorted(by_id)
    p['high_trace'] = {}
    p['high_trace_count'] = {}
    for fid in reg['wf']['frames']:
        fr = by_id.get('f' + fid[1:])
        if not fr:
            continue
        m = FTRACE_RE.search(fr['text'])
        p['high_trace_count'][fid] = fr['text'].count('class="f-trace"')
        if not m:
            continue
        p['high_trace'][fid] = NUM_RE.findall(TAG_STRIP_RE.sub('', span_at(fr['text'], m.start())))
    p['f5_span'] = by_id['f5']['span'] if 'f5' in by_id else None
    p['f6_span'] = by_id['f6']['span'] if 'f6' in by_id else None
    p['rule_blocks'] = rule_surfaces(t)
    p['rule_row_count'] = sum(len(v['rows']) for v in p['rule_blocks'].values())
    p['rule_surface_count'] = len(p['rule_blocks'])

    # ---- 低保真 index.html
    t = art['text'][lo]
    p['low_bytes'] = len(art['bytes'][lo])
    wf = re.findall(r'data-wf-frame="([^"]*)"', t)
    p['low_wf_frames'] = wf
    p['low_wf_frame_count'] = len(wf)
    p['low_regions'] = len(re.findall(r'data-wf-region="([^"]*)"', t))
    p['low_trace'] = parse_lowfi_trace(t, reg['frames']['lowfi_trace_section'])
    p['low_trace_count'] = len(p['low_trace'])

    # ---- 低保真 prototype.html
    # ⚠️ 属性值里有两种**假阳性**：`data-proto-nav="['F1','F2',…]"` 整个清单是一个字符串
    # （要先切出 `'X'`），`data-proto-screen="' + state.screen + '"` 是跑在浏览器里的拼接片段
    # （`[^"]*` 会把它整段捕获）。所以 nav 按引号切、screen 只留「像标识符」的那些。
    t = art['text'][pr]
    p['proto_bytes'] = len(art['bytes'][pr])
    p['proto_probes'] = len(set(re.findall(r'probe\(\s*"P-(\d+)"', t)))
    p['proto_nav'] = []
    for v in re.findall(r'data-proto-nav="([^"]*)"', t):
        p['proto_nav'].extend(re.findall(r"'([^']*)'", v))
    p['proto_screens'] = [s for s in re.findall(r'data-proto-screen="([^"]*)"', t)
                          if IDENT_RE.match(s)] + p['proto_nav']

    # ---- 高保真 dashboard.html（派生）
    p['board_bytes'] = len(art['bytes'][bo])
    p['board_expected_bytes'] = len(art['board_expected'] or b'')

    # ---- 上游
    p['metric_labels'] = art['metric_labels']
    p['metric_label_count'] = len(art['metric_labels'])
    p['rule_ids'] = art['rule_ids']
    p['rule_id_count'] = len(art['rule_ids'])
    model = art['rule_model']
    p['rule_specs'] = model['specs']
    p['rule_spec_count'] = len(model['specs'])
    p['rule_decisions'] = model['decisions']
    p['rule_decision_count'] = len(model['decisions'])
    p['blacklist_rule'] = model['blacklist']
    p['fixture_texts'] = art['fixture_texts']
    p['fixture_text_count'] = len(art['fixture_texts'])
    p['styles_bytes'] = len(art['styles_raw'])
    p['styles_media'] = len(MEDIA_RE.findall(art['styles_text']))
    return art


# ---------------------------------------------------------------- 判据层

def _cell_ok(col, upstream, shown):
    """图纸某一格的显示文本是否等于上游真值。

    阈值按**数值**比（上游是 `0.10` 这种 float，图纸可能印 `0.10` 也可能印 `3`，
    而 `3` 与 `3.0` 说的是同一件事）；其余按**字符串相等**比 —— 相等而不是包含：
    包含判据会被「值后面多抄一段」蒙混过关，而相等不会。
    """
    if col == 'threshold':
        try:
            return abs(float(shown) - float(upstream)) < 1e-9
        except (TypeError, ValueError):
            return False
    if col == 'direction':
        return shown == DIRECTION_TEXT.get(upstream)
    return shown == upstream


def judge(art, reg):
    """纯函数：只吃 art（已现测）与 reg，吐出 [(code, msg)]。"""
    f = []
    p = art['probes']
    A = reg['artifacts']

    def add(code, msg):
        f.append((code, msg))

    # ---- DA-EXTRACT：提取为空 = 后面全是空转，一律拒绝通过
    for name in ('high_frames', 'high_symbols', 'high_uses', 'high_anchor_count',
                 'high_anchor_links', 'high_td', 'high_bracket_places', 'high_css_sections',
                 'low_wf_frame_count', 'low_regions', 'low_trace_count', 'proto_probes',
                 'metric_label_count', 'rule_id_count', 'fixture_text_count', 'styles_bytes',
                 'rule_spec_count', 'rule_decision_count', 'rule_row_count'):
        if not p.get(name):
            add('DA-EXTRACT', 'extraction `%s` came back empty -- every later check would '
                              'run on nothing; refusing to pass' % name)
    want_frames = reg['wf']['frames']
    if len(p['low_trace']) != len(want_frames):
        add('DA-EXTRACT', 'low-fi trace table gave %d data row(s), expected %d'
            % (len(p['low_trace']), len(want_frames)))
    if len(p['high_trace']) != len(want_frames):
        add('DA-EXTRACT', 'design.html gave %d frame trace(s), expected %d'
            % (len(p['high_trace']), len(want_frames)))
    if not reg.get('claims'):
        add('DA-EXTRACT', 'registry has no claims -- DA-COUNT would be vacuous')
    for c in reg.get('claims', []):
        if not isinstance(c.get('expected'), int):
            add('DA-EXTRACT', 'claim `%s` has a non-integer expected value' % c.get('probe'))
    if p['f5_span'] is None:
        add('DA-EXTRACT', 'design.html has no frame with id="f5"')

    # ---- DA-USE-UNDEF / DA-ANCHOR-UNDEF / DA-DUP-ID：三份手写图纸各自内部自洽
    for key in ('high', 'low', 'proto'):
        rel = A[key]
        text = art['text'][rel]
        el = elems(text)
        sym = set(SYMBOL_RE.findall(text))
        for tgt in sorted(set(USE_RE.findall(text)) - sym):
            add('DA-USE-UNDEF', '%s uses <use href="#%s"> but no <symbol id="%s"> exists'
                % (rel, tgt, tgt))
        ids = [e['id'] for e in el if e['id']]
        for tgt in sorted(set(HREF_RE.findall(text)) - set(ids)):
            if '/' in tgt:
                # `#/f1` 是 hash 路由（原型用 JS 切屏），不是元素锚点 —— 报了就是假 FINDING。
                continue
            add('DA-ANCHOR-UNDEF', '%s links href="#%s" but nothing declares that id'
                % (rel, tgt))
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            add('DA-DUP-ID', '%s declares id="%s" %d times' % (rel, dup, ids.count(dup)))

    # ---- DA-FRAME-STATUS：已交付 / 提案的边界不许互串
    if p['high_frames'] != reg['frames']['total']:
        add('DA-FRAME-STATUS', '%s has %d .frame element(s), registry says %d'
            % (A['high'], p['high_frames'], reg['frames']['total']))
    if p['high_proposal'] != reg['frames']['proposal']:
        add('DA-FRAME-STATUS', '%s has %d .proposal element(s), registry says %d'
            % (A['high'], p['high_proposal'], reg['frames']['proposal']))
    if p['high_delivered_ids'] != reg['frames']['delivered_ids']:
        add('DA-FRAME-STATUS', '%s delivered frame id(s) = %s, registry says %s'
            % (A['high'], p['high_delivered_ids'], reg['frames']['delivered_ids']))
    for fid, cnt in sorted(p['high_trace_count'].items()):
        if cnt != 1:
            add('DA-FRAME-STATUS', 'frame %s in %s holds %d .f-trace element(s), expected 1 '
                                   '(frames must not nest)' % (fid, A['high'], cnt))
    low_ok = [r['fid'] for r in p['low_trace'] if r['delivered']]
    if low_ok != reg['frames']['lowfi_delivered']:
        add('DA-FRAME-STATUS', 'low-fi trace marks %s as delivered, registry says %s'
            % (low_ok, reg['frames']['lowfi_delivered']))

    # ---- DA-TRACE-PRD：低保真写「回哪几条 PRD」，高保真把同一条线画得更全
    for row in p['low_trace']:
        fid = row['fid']
        got = p['high_trace'].get(fid)
        if got is None:
            add('DA-TRACE-PRD', 'no .f-trace found for frame %s in %s' % (fid, A['high']))
            continue
        want = row['prd']
        if not want:
            # 逐帧的空转守卫：PRD 格没解析出数字时，下面的前缀比较恒真。
            add('DA-TRACE-PRD', 'frame %s: low-fi PRD cell yielded no digits, so the prefix '
                                'test would be vacuous' % fid)
            continue
        if got[:len(want)] != want:
            add('DA-TRACE-PRD', 'frame %s: low-fi PRD digits %s is not a prefix of the '
                                'design.html .f-trace digits %s' % (fid, want, got))

    # ---- DA-WF-REGION / DA-SCREEN-SET：三份产物的抓手/屏集要对得上
    if p['low_regions'] != reg['wf']['regions']:
        add('DA-WF-REGION', '%s has %d data-wf-region attribute(s), registry says %d'
            % (A['low'], p['low_regions'], reg['wf']['regions']))
    if p['low_wf_frame_count'] != len(reg['wf']['frames']):
        add('DA-WF-REGION', '%s has %d data-wf-frame attribute(s), registry says %d'
            % (A['low'], p['low_wf_frame_count'], len(reg['wf']['frames'])))
    proto_f = [s for s in p['proto_screens'] if re.match(r'^F\d+$', s)]
    if set(p['low_wf_frames']) != set(proto_f):
        add('DA-SCREEN-SET', 'low-fi frames %s != prototype F-screens %s'
            % (sorted(set(p['low_wf_frames'])), sorted(set(proto_f))))
    if set(p['proto_screens']) != set(reg['proto']['screens']):
        add('DA-SCREEN-SET', '%s screens %s != registry %s'
            % (A['proto'], sorted(set(p['proto_screens'])), sorted(reg['proto']['screens'])))

    # ---- DA-UPSTREAM-F5：F5 段的两半（指标标签按原序 + 夹具展示串逐字在内）
    labels = p['metric_labels']
    fx = [t for t in p['fixture_texts'] if t]
    f5 = art['text'][A['high']][p['f5_span'][0]:p['f5_span'][1]] if p['f5_span'] else ''
    at = 0
    missing = []
    for lab in labels:
        i = f5.find(lab, at)
        if i < 0:
            missing.append(lab)
        else:
            at = i + len(lab)
    if missing:
        add('DA-UPSTREAM-F5', 'frame f5 does not carry the METRIC_SPECS labels in order; '
                              'missing %d of %d, first missing index %d'
            % (len(missing), len(labels), labels.index(missing[0])))
    gone = [t for t in fx if t not in f5]
    if gone:
        add('DA-UPSTREAM-F5', 'frame f5 is missing %d of %d fixture display string(s) verbatim; '
                              'first missing index %d'
            % (len(gone), len(fx), fx.index(gone[0])))

    # ---- DA-UPSTREAM-RULES / DA-RULE-COUNT：图纸里**每一处规则表面**都必须是
    #      `quanauto/risk.py` 的逐行逐列投影，而不是手抄。
    #      ⚠️ 加厚前这里只要求「6 条规则 id 在 F6 段里出现过」——那是本仓库铁律 ⑥ 点名禁止的
    #      「名字出现过当覆盖判据」：行序、单位、方向、阈值、判定、严重度**六样都可以是错的**，
    #      而它一声不响（实测：`0.10` 改成 `0.11` 全绿）。
    exp_rows = []
    for sp in p['rule_specs']:
        dec = p['rule_decisions'].get(sp['rule_id']) or (None, None)
        exp_rows.append({'rule_id': sp['rule_id'], 'unit': sp['unit'],
                         'direction': sp['direction'], 'threshold': sp['threshold'],
                         'action': dec[0], 'severity': dec[1]})
    by_id = {r['rule_id']: r for r in exp_rows}
    bl = p['blacklist_rule']
    bl_row = {'rule_id': bl[0], 'action': bl[1]} if bl and bl[1] else None
    for surf in RULE_SURFACES:
        key, name = surf['key'], surf['name']
        rec = p['rule_blocks'].get(name) or {}
        rows = rec.get('rows') or []
        if not rows:
            add('DA-EXTRACT', 'rule surface `%s` could not be located: %s'
                % (key, rec.get('why') or 'no rows parsed'))
            continue
        if key.startswith('f6-'):
            # 这两处表面声称的是「F6 里的表」⇒ 必须真的在那一帧里。只按表头签名找的话，
            # 把 `id="f6"` 抹掉、或把表搬到另一帧去，它依旧找得到 ⇒ 那一帧没了而判据照样绿。
            fspan, span = p['f6_span'], rec.get('span')
            if (fspan is None or span is None or span[0] < fspan[0] or span[1] > fspan[1]):
                add('DA-EXTRACT', 'surface `%s` is not inside the frame with id="f6" -- the frame '
                                  'that claim is about is gone, or the table moved out of it'
                    % key)
        if surf['expect'] == 'subset':
            # 摘一段：上游没说「一定要全列」⇒ **条数与行序不判**，只判「每一行都对」+ id 不重复。
            # 这是写下来的口径（`cols` / `expect` 就在登记表旁边），不是漏判。
            pairs = []
            seen = set()
            for i, row in enumerate(rows):
                rid = row.get('rule_id') or ''
                if rid in seen:
                    add('DA-UPSTREAM-RULES', 'surface `%s` row %d repeats rule id %r'
                        % (key, i + 1, rid))
                seen.add(rid)
                exp = by_id.get(rid) or (bl_row if bl_row and bl_row['rule_id'] == rid else None)
                if exp is None:
                    add('DA-UPSTREAM-RULES', 'surface `%s` row %d draws rule id %r, which is '
                                             'not in quanauto/risk.py' % (key, i + 1, rid))
                    continue
                pairs.append((i + 1, row, exp))
        else:
            exp_list = list(exp_rows)
            if surf['expect'] == 'all+blacklist':
                exp_list = exp_list + ([bl_row] if bl_row else [])
            elif surf['expect'] == 'blacklist':
                exp_list = [bl_row] if bl_row else []
            if not exp_list:
                add('DA-EXTRACT', 'surface `%s`: upstream yielded no rule row to compare -- the '
                                  'row-by-row test would be vacuous' % key)
                continue
            if len(rows) != len(exp_list):
                add('DA-UPSTREAM-RULES', 'surface `%s` draws %d data row(s); quanauto/risk.py '
                                         'implies %d' % (key, len(rows), len(exp_list)))
            pairs = [(i + 1, r, exp_list[i]) for i, r in enumerate(rows) if i < len(exp_list)]
        for num, got, exp in pairs:
            for col in sorted(surf['cols']):
                up = exp.get(col)
                if up is None:
                    # 上游这一列没有真值（名单型规则没有阈值 / 上游没给裁决）⇒ 本列**不判**。
                    # 这是「上游缺」，不是「提取失败」；提取失败会让 rows 为空并报 DA-EXTRACT。
                    continue
                if col == 'direction' and up not in DIRECTION_TEXT:
                    add('DA-UPSTREAM-RULES', 'surface `%s` row %d: upstream tightening_direction '
                                             '%r has no form on this drawing -- refusing to '
                                             'pass rather than silently skipping the column'
                        % (key, num, up))
                    continue
                shown = got.get(col, '')
                if not _cell_ok(col, up, shown):
                    add('DA-UPSTREAM-RULES', 'surface `%s` row %d, column %s: the drawing says '
                                             '%r but quanauto/risk.py says %r'
                        % (key, num, surf['cols'][col], afold(shown), up))
        # `.ph-lab` 自称的条数与它下面那张列表的行数，是一对**声明 / 产物**：
        # 声明写错与列表画错是两回事，但它们之间得有一条判据（不然那句声明没人管）。
        if surf['kind'] == 'phlist':
            declared = declared_count(rec.get('label'))
            if declared is not None and declared != len(rows):
                add('DA-RULE-COUNT', 'surface `%s`: the .ph-lab declares %d rule row(s) but the '
                                     'list under it holds %d' % (key, declared, len(rows)))

    # ---- DA-COUNT：登记表 == 现测值，且这个值在指名的 README 里逐字写着
    for c in reg.get('claims', []):
        name = c['probe']
        if name not in p:
            add('DA-COUNT', 'claim `%s` names a probe that does not exist' % name)
            continue
        live = p[name]
        if live != c['expected']:
            add('DA-COUNT', '`%s` measured %r but registry says %r (%s)'
                % (name, live, c['expected'], c.get('caliber', 'no caliber given')))
        if c['doc'] not in art['docs']:
            add('DA-COUNT', '`%s` names a README that does not exist: %s' % (name, c['doc']))
            continue
        vals = {'value': c['expected']}
        if 'probe2' in c:
            vals['value2'] = p.get(c['probe2'])
        try:
            needle = c['template'].format(**vals)
        except (KeyError, IndexError, ValueError) as exc:
            add('DA-COUNT', '`%s` template cannot be formatted (%s)' % (name, exc))
            continue
        body = art['docs'][c['doc']]
        if needle not in body:
            add('DA-COUNT', '`%s`: %s does not contain %r verbatim -- the doc and the artifact '
                            'disagree (%s)' % (name, c['doc'], needle, c.get('caliber', '')))
        # 反方向也要查：只查「该在的在不在」是单向判据，旧数字留着不动就永远隐身
        # （本仓库的 docx 派生 md 正是被这条咬过：只查 missing=0，追加物被抹掉照样绿）。
        stale = c.get('forbidden') or []
        if isinstance(stale, str):
            stale = [stale]
        for bad in stale:
            if bad in body:
                add('DA-COUNT', '`%s`: %s still carries the stale string %r -- fix the doc, '
                                'not the registry' % (name, c['doc'], bad))

    # ---- DA-FRESH：派生文件必须与生成器现算的字节一致
    board = A['board']
    if art.get('board_error'):
        add('DA-FRESH', '%s could not be regenerated: %s' % (GENERATOR_REL, art['board_error']))
    elif art['board_expected'] is None:
        add('DA-FRESH', 'no expected bytes for %s' % board)
    else:
        disk = art['bytes'][board].decode('utf-8')
        want = art['board_expected'].decode('utf-8')
        if HASH_RE.sub(r'\1H\2', disk) == disk:
            add('DA-FRESH', '%s has no "真源提交 <code>hash</code>" header -- the freshness '
                            'anchor is gone, so byte equality cannot be judged' % board)
        elif HASH_RE.sub(r'\1H\2', disk) != HASH_RE.sub(r'\1H\2', want):
            i = 0
            a = HASH_RE.sub(r'\1H\2', disk)
            b = HASH_RE.sub(r'\1H\2', want)
            while i < min(len(a), len(b)) and a[i] == b[i]:
                i += 1
            add('DA-FRESH', '%s is stale: first difference at char %d of %d/%d -- rerun %s'
                % (board, i, len(a), len(b), GENERATOR_REL))
    return f


# ---------------------------------------------------------------- 自测

def _synth(base, reg, texts, board_raw=None, docs=None, upstream=None):
    art = {'root': base['root'], 'text': texts, 'probes': {}, 'stats': {},
           'notes': [], 'board_error': None, 'board_expected': base['board_expected'],
           'board_checks': base.get('board_checks'),
           'metric_labels': base['metric_labels'], 'rule_ids': base['rule_ids'],
           'rule_model': dict(base['rule_model']),
           'fixture_texts': base['fixture_texts'],
           'styles_raw': base['styles_raw'], 'styles_text': base['styles_text'],
           'docs': dict(base.get('docs') or {})}
    # 上游那一侧也要能造样本：AST 读写回一个空列表（模拟「提取不到规则」）必须是 DA-EXTRACT，
    # 而不是「两边都没内容 ⇒ 视作一致」。
    if upstream:
        art['rule_model'].update(upstream)
    if docs:
        art['docs'].update(docs)
    # 磁盘是 CRLF（`.gitattributes` 是 `* -text`），而 text 是 LF 归一化过的 ⇒ 直接 encode 会每个换行
    # 少一个 CR，字节数判据（high_bytes / low_bytes / proto_bytes / board_bytes / DA-FRESH）当场误报。
    # 实测：不还原时「正样本」会冒出 5 条 DA-COUNT/DA-FRESH 假 FINDING。
    art['bytes'] = {rel: t.replace('\n', '\r\n').encode('utf-8') for rel, t in texts.items()}
    if board_raw is not None:
        art['bytes'][reg['artifacts']['board']] = board_raw
    measure(art, reg)
    return art


def _mut(text, old, new, expect, tag):
    """变异必须**打到靶子**：命中数不等于期望就判失败并返回 None（不许当 no-op 悄悄过去）。"""
    n = text.count(old)
    if n != expect:
        print('    MISS %s: anchor %r hit %d time(s), expected %d -- mutation not applied'
              % (tag, afold(old), n, expect))
        return None
    out = text.replace(old, new)
    if out == text:
        print('    MISS %s: mutation was a no-op' % tag)
        return None
    print('    applied: %s (%r -> %r x%d)' % (tag, afold(old), afold(new), n))
    return out


def selftest():
    """三类样本：正样本（真产物 -> 0 条）· 一类一条负样本 · 变异打到靶子。

    正样本是关键的一半：「干净时 0 条」证明没有探测器在空转，于是负样本里报出的那个码
    才**可归因于那次变异**（否则「探测器不存在」与「变异打了空分支」长得一模一样）。
    """
    try:
        reg = load_registry(ROOT)
        base = measure(collect(ROOT, reg), reg)
    except (InputMissing, OSError, ValueError, KeyError) as exc:
        print('SELFTEST FAIL: cannot load the real artifacts: %s' % afold(str(exc)))
        return 1

    A = reg['artifacts']
    HI, LO, PR, BO = A['high'], A['low'], A['proto'], A['board']
    real = dict(base['text'])
    ok = True
    seen = set()

    def scenario(tag, texts, want, board_raw=None, docs=None, upstream=None,
                 want_msg=None):
        """`want_msg` 是可选的**靶子断言**：光有检查码不够 —— 同一个码可能由**别的**表面报出来
        （那样这次变异等于没打到靶子，而报告长得一模一样）。
        """
        nonlocal ok
        art = _synth(base, reg, texts, board_raw, docs, upstream)
        found = judge(art, reg)
        codes = [c for c, _ in found]
        seen.update(codes)
        if want is None:
            good = not codes
        else:
            good = want in codes
        if good and want_msg is not None:
            good = any(want_msg in msg for _, msg in found)
            if not good:
                print('    MISS %s: no finding mentions %r -- the right code fired for the '
                      'wrong target' % (tag, afold(want_msg)))
        print('  [%-30s] issues=%d codes=%s %s'
              % (tag, len(codes), sorted(set(codes)) or '[]', 'OK' if good else 'MISSED'))
        if not good:
            ok = False
        return good

    print('CONTROL (real artifacts, not asserted clean just here):')
    scenario('POSITIVE-clean-real', dict(real), None)

    print('NEG samples (one per detector):')

    # 1) DA-EXTRACT：把 37 个 <symbol> 的 id 全废掉 -> 符号提取为空
    t = dict(real)
    t[HI], n = re.subn(r'<symbol\b([^>]*?)\bid="', r'<symbol\1data-sym="', t[HI])
    if n != base['probes']['high_symbols']:
        print('    MISS DA-EXTRACT: rewrote %d symbol(s), expected %d'
              % (n, base['probes']['high_symbols']))
        ok = False
    else:
        scenario('NEG-extract-symbols-empty', t, 'DA-EXTRACT')

    # 2) DA-USE-UNDEF：让一个**被用到**的 symbol 找不到定义
    used = sorted(set(SYMBOL_RE.findall(real[HI])) - set(base['probes']['high_unused_symbols']))
    t = dict(real)
    t[HI], n = re.subn(r'href="#%s"' % re.escape(used[0]), 'href="#%s-zz"' % re.escape(used[0]),
                       t[HI], count=1)
    if n != 1:
        print('    MISS DA-USE-UNDEF: could not rewrite href of symbol %r' % used[0])
        ok = False
    else:
        scenario('NEG-use-undefined', t, 'DA-USE-UNDEF')

    # 3) DA-ANCHOR-UNDEF：让一个页内锚点的目标消失（挑非 symbol 的目标，避开 #2 的探测器）
    targets = [x for x in sorted(set(HREF_RE.findall(real[HI]))) if x not in set(SYMBOL_RE.findall(real[HI]))]
    t = dict(real)
    t[HI], n = re.subn(r'href="#%s"' % re.escape(targets[0]), 'href="#%s-zz"' % re.escape(targets[0]),
                       t[HI], count=1)
    if n != 1:
        print('    MISS DA-ANCHOR-UNDEF: could not rewrite href %r' % targets[0])
        ok = False
    else:
        scenario('NEG-anchor-undefined', t, 'DA-ANCHOR-UNDEF')

    # 4) DA-DUP-ID：把一个 id 加到另一个元素上
    uniq = [i for i in sorted(base['probes']['high_id_set']) if real[HI].count('id="%s"' % i) == 1]
    t = dict(real)
    old_attr, new_attr = 'id="%s"' % uniq[-1], 'id="%s"' % uniq[-2]
    t[HI] = _mut(t[HI], old_attr, new_attr, 1, 'DA-DUP-ID')
    if t[HI] is None:
        ok = False
    else:
        scenario('NEG-dup-id', t, 'DA-DUP-ID')

    # 5) DA-FRAME-STATUS：**每一帧**的「提案」章都换成「已交付」——守卫成立的条件是"全部变形"
    t = dict(real)
    t[HI], n = re.subn(r'class="frame proposal', 'class="frame delivered', t[HI])
    if n != base['probes']['high_proposal']:
        print('    MISS DA-FRAME-STATUS: rewrote %d frame(s), expected %d'
              % (n, base['probes']['high_proposal']))
        ok = False
    else:
        scenario('NEG-frame-status-flipped', t, 'DA-FRAME-STATUS')

    # 6) DA-TRACE-PRD：把低保真某一行追溯表的 PRD 格改掉（按绝对区间切，不猜文本）
    row = [r for r in base['probes']['low_trace'] if r['prd']]
    t = dict(real)
    if not row:
        print('    MISS DA-TRACE-PRD: no low-fi trace row had any PRD digit')
        ok = False
    else:
        s, e = row[0]['prd_span']
        t[LO] = t[LO][:s] + '9' + t[LO][e:]
        if t[LO] == real[LO]:
            print('    MISS DA-TRACE-PRD: slice replace was a no-op')
            ok = False
        else:
            print('    applied: DA-TRACE-PRD (frame %s PRD cell %d..%d -> 9)'
                  % (row[0]['fid'], s, e))
            scenario('NEG-trace-prd-broken', t, 'DA-TRACE-PRD')

    # 7) DA-WF-REGION：删掉一个 data-wf-region
    t = dict(real)
    t[LO], n = re.subn(r'\s*data-wf-region="[^"]*"', '', t[LO], count=1)
    if n != 1:
        print('    MISS DA-WF-REGION: could not delete one data-wf-region attribute')
        ok = False
    else:
        scenario('NEG-wf-region-short', t, 'DA-WF-REGION')

    # 8) DA-SCREEN-SET：低保真少一屏
    t = dict(real)
    t[LO], n = re.subn(r'\s*data-wf-frame="F7"', '', t[LO], count=1)
    if n != 1:
        print('    MISS DA-SCREEN-SET: could not delete data-wf-frame="F7"')
        ok = False
    else:
        scenario('NEG-screen-set-mismatch', t, 'DA-SCREEN-SET')

    # 9) DA-UPSTREAM-F5：把 F5 段里一个**只出现一次**的指标标签改坏（只加后缀改不坏 ——
    #    find() 仍然命中那段前缀，守卫会正确地保持沉默，看起来就像它根本不存在）
    s, e = base['probes']['f5_span']
    blk = real[HI][s:e]
    cand = [l for l in base['metric_labels'] if blk.count(l) == 1]
    t = dict(real)
    if not cand:
        print('    MISS DA-UPSTREAM-F5: no METRIC_SPECS label occurs exactly once in frame f5')
        ok = False
    else:
        mangled = cand[0][:-1] + 'Q'
        t[HI] = real[HI][:s] + blk.replace(cand[0], mangled) + real[HI][e:]
        if t[HI] == real[HI]:
            print('    MISS DA-UPSTREAM-F5: mangling the label was a no-op')
            ok = False
        else:
            print('    applied: DA-UPSTREAM-F5 (label index %d mangled)'
                  % base['metric_labels'].index(cand[0]))
            scenario('NEG-upstream-f5-label', t, 'DA-UPSTREAM-F5')

    # 10) DA-UPSTREAM-RULES：**每个登记的规则表面各一条负样本**。逐个手写会漏掉将来新加的
    #     表面（那时「新表面没人测」与「新表面没判据」长得一模一样），所以按 RULE_SURFACES 自动造。
    #     变异把该表面**第一行的规则 id**换成哨兵：判据比的是**相等**（不是前缀搜索），整串换掉就行；
    #     命中数不是 1 就判 HARNESS 失败（打不到靶子的变异不算证据）。
    for surf in RULE_SURFACES:
        rec = base['probes']['rule_blocks'].get(surf['name']) or {}
        rows, span = rec.get('rows') or [], rec.get('span')
        shown = rows[0].get('rule_id') if rows else ''
        if not span or not shown:
            print('    MISS DA-UPSTREAM-RULES(%s): the surface was not located in the real '
                  'artifacts' % surf['key'])
            ok = False
            continue
        s, e = span
        blk = real[HI][s:e]
        hit = blk.count(shown)
        if hit != 1:
            print('    MISS DA-UPSTREAM-RULES(%s): rule id %r occurs %d time(s) in its own '
                  'block' % (surf['key'], afold(shown), hit))
            ok = False
            continue
        t = dict(real)
        t[HI] = real[HI][:s] + blk.replace(shown, shown + 'ZZ') + real[HI][e:]
        if t[HI] == real[HI]:
            print('    MISS DA-UPSTREAM-RULES(%s): mutation was a no-op' % surf['key'])
            ok = False
            continue
        print('    applied: DA-UPSTREAM-RULES(%s) rule id %r -> sentinel'
              % (surf['key'], afold(shown)))
        # 靶子断言：报出的那条必须**点名这一处表面** —— 同一个码从别的表面冒出来也算没打到。
        scenario('NEG-rules-id-%s' % surf['key'], t, 'DA-UPSTREAM-RULES',
                 want_msg='surface `%s`' % surf['key'])

    f6rec = base['probes']['rule_blocks'].get('F6 数值型规则表') or {}
    f6blk = real[HI][f6rec['span'][0]:f6rec['span'][1]] if f6rec.get('span') else ''

    # 10b) 行序：把 F6 数值表的**前两行对调**。旧口径只要求「id 出现过」⇒ 对调之后它照样全绿；
    #      这一条是「逐行逐列」相对「名字出现过」的最直接增量。
    a, b = [r['rule_id'] for r in (f6rec.get('rows') or [])[:2]] or ['', '']
    if not f6blk or a == b or f6blk.count(a) != 1 or f6blk.count(b) != 1:
        print('    MISS DA-UPSTREAM-RULES(order): cannot swap the first two RULE_SPECS rows')
        ok = False
    else:
        t = dict(real)
        s, e = f6rec['span']
        swapped = f6blk.replace(a, '\x00').replace(b, a).replace('\x00', b)
        t[HI] = real[HI][:s] + swapped + real[HI][e:]
        if swapped == f6blk:
            print('    MISS DA-UPSTREAM-RULES(order): swap was a no-op')
            ok = False
        else:
            print('    applied: DA-UPSTREAM-RULES(order) swapped %s <-> %s' % (a, b))
            scenario('NEG-rules-order-swap', t, 'DA-UPSTREAM-RULES')

    # 10c) 默认阈值：F6 数值表里 max_sector_pct 那一行的 0.30 -> 0.31。
    #      这一格旧口径**根本不看**（它只看 id）⇒ 这是新牙的直接证据。
    if f6blk.count('0.30') != 1:
        print('    MISS DA-UPSTREAM-RULES(threshold): 0.30 occurs %d time(s) in the F6 numeric '
              'table' % f6blk.count('0.30'))
        ok = False
    else:
        t = dict(real)
        s, e = f6rec['span']
        t[HI] = real[HI][:s] + f6blk.replace('0.30', '0.31') + real[HI][e:]
        print('    applied: DA-UPSTREAM-RULES(threshold) 0.30 -> 0.31')
        scenario('NEG-rules-threshold', t, 'DA-UPSTREAM-RULES', want_msg='column 默认阈值')

    # 10d) 名单型规则：F6 那张单独的表里 REJECT -> HALT。名单型的判定**不在** `RULE_SPECS`
    #      里（它来自 RiskViolation(rule_id=BLACKLIST_RULE_ID, action=RiskActionEnum.REJECT)）
    #      ⇒ 这条证明那一路真值也被钉住了。
    lrec = base['probes']['rule_blocks'].get('F6 名单型规则表') or {}
    lblk = real[HI][lrec['span'][0]:lrec['span'][1]] if lrec.get('span') else ''
    if lblk.count('REJECT') != 1:
        print('    MISS DA-UPSTREAM-RULES(blacklist): REJECT occurs %d time(s) in the F6 '
              'list-type table' % lblk.count('REJECT'))
        ok = False
    else:
        t = dict(real)
        s, e = lrec['span']
        t[HI] = real[HI][:s] + lblk.replace('REJECT', 'HALT') + real[HI][e:]
        print('    applied: DA-UPSTREAM-RULES(blacklist) REJECT -> HALT')
        scenario('NEG-rules-blacklist', t, 'DA-UPSTREAM-RULES')

    # 10e) 空转：F6 数值表的表头被改一个字 ⇒ 那一处表面**定位不到**。定位不到必须报 DA-EXTRACT
    #      （提取为空 = 后面的判据全在空转），**不是**「跳过这一处」。
    t = dict(real)
    t[HI] = _mut(t[HI], '<th class="n">默认阈值</th>', '<th class="n">阈值</th>', 1,
                 'DA-EXTRACT-table-locate')
    if t[HI] is None:
        ok = False
    else:
        scenario('NEG-rules-table-gone', t, 'DA-EXTRACT')

    # 10f) 空转：§09 那张数值表的 `.ph-lab` 标签被改掉 ⇒ 同一处表面定位不到
    #      （这一条同时证明标签是**定位键**，不是装饰）。
    #      ⚠️ 变异必须把定位键**改坏**，不能只是加尾巴：第一版写的是「数值型规则」→
    #      「数值型规则ZZ」，而后者**仍然包含**前者 ⇒ 表面照样定位得到、报出的是别的码，
    #      看上去就像这条守卫根本不存在（前缀陷阱，本仓库已经被咬过一次）。
    t = dict(real)
    t[HI] = _mut(t[HI], '数值型规则 · 6 条', '数值规则 · 6 条', 1, 'DA-EXTRACT-phlab-locate')
    if t[HI] is None:
        ok = False
    else:
        scenario('NEG-rules-phlab-gone', t, 'DA-EXTRACT')

    # 10g) 空转：**上游**读成空（AST 抽不到任何 RuleSpec）⇒ 也必须是 DA-EXTRACT，
    #      而不是「两边都没内容 ⇒ 视作一致」（那是空转里最危险的形态：报告比真通过还干净）。
    scenario('NEG-rules-upstream-empty', dict(real), 'DA-EXTRACT', upstream={'specs': []})

    # 10h) DA-RULE-COUNT：M1 的 `.ph-lab` 自称「7 条里的 4 条」⇒ 改成 5 条（声明与产物就不是一回事了）
    t = dict(real)
    t[HI] = _mut(t[HI], '7 条里的 4 条', '7 条里的 5 条', 1, 'DA-RULE-COUNT')
    if t[HI] is None:
        ok = False
    else:
        scenario('NEG-rules-count-declared', t, 'DA-RULE-COUNT',
                 want_msg='surface `m1-summary`')

    # 10i) 方向：F6 数值表**第一行**的方向格（「收紧 ↓」）改成「放宽 ↑」。
    #      这一格要经过 DIRECTION_TEXT 那张「上游枚举 → 图纸形态」映射表 ——
    #      上面的 0.30->0.31 走的是数值比较那一路，**测不到这条分支**。
    #      按绝对偏移只换第一处（同一个「收紧 ↓」在表里出现 6 次，按文本替换会一口气改 6 行，
    #      那样报出的就不是「第一行那一格」了）。
    ARROW = '\u6536\u7d27 \u2193'
    i0 = real[HI].find(ARROW, f6rec['span'][0]) if f6rec.get('span') else -1
    if i0 < 0 or i0 + len(ARROW) > (f6rec['span'][1] if f6rec.get('span') else 0):
        print('    MISS DA-UPSTREAM-RULES(direction): no direction cell inside the F6 numeric '
              'table')
        ok = False
    else:
        t = dict(real)
        t[HI] = real[HI][:i0] + '\u653e\u5bbd \u2191' + real[HI][i0 + len(ARROW):]
        if t[HI] == real[HI]:
            print('    MISS DA-UPSTREAM-RULES(direction): mutation was a no-op')
            ok = False
        else:
            print('    applied: DA-UPSTREAM-RULES(direction) first direction cell flipped')
            scenario('NEG-rules-direction', t, 'DA-UPSTREAM-RULES',
                     want_msg='column 方向')

    # 11) DA-COUNT 的两个靶子：① 读数被改 ② 那一句被删掉（文档侧在 art['docs'] 里改）
    c0 = [c for c in reg['claims'] if c['probe'] == 'high_bytes']
    if not c0:
        print('    MISS DA-COUNT: registry has no high_bytes claim')
        ok = False
    else:
        good = c0[0]['template'].format(value=c0[0]['expected'])
        bad = c0[0]['template'].format(value=c0[0]['expected'] + 1000)
        doc = c0[0]['doc']
        for tag, repl in (('value', bad), ('deleted', '')):
            D = dict(base['docs'])
            D[doc] = _mut(D[doc], good, repl, 1, 'DA-COUNT-%s' % tag)
            if D[doc] is None:
                ok = False
            else:
                scenario('NEG-count-doc-%s' % tag, dict(real), 'DA-COUNT', docs=D)

    # 12) DA-FRESH：派生文件的字节被动了一个
    scenario('NEG-fresh-stale-board', dict(real),
             'DA-FRESH', board_raw=base['bytes'][BO] + b' ')

    for code in ('DA-EXTRACT', 'DA-USE-UNDEF', 'DA-ANCHOR-UNDEF', 'DA-DUP-ID', 'DA-FRAME-STATUS',
                 'DA-TRACE-PRD', 'DA-WF-REGION', 'DA-SCREEN-SET', 'DA-UPSTREAM-F5',
                 'DA-UPSTREAM-RULES', 'DA-RULE-COUNT', 'DA-COUNT', 'DA-FRESH'):
        if code not in seen:
            print('  [%-30s] %s' % ('DETECTOR-NEVER-FIRED', code))
            ok = False

    print('SELFTEST %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main(argv):
    if '--selftest' in argv:
        return selftest()
    try:
        reg = load_registry(ROOT)
        art = collect(ROOT, reg)
    except InputMissing as exc:
        print('GATE FAIL: missing input %s' % exc)
        return 1
    except (OSError, ValueError) as exc:
        print('GATE FAIL: cannot read inputs: %s' % afold(str(exc)))
        return 1
    measure(art, reg)
    issues = judge(art, reg)

    for key in sorted(reg['artifacts']):
        rel = reg['artifacts'][key]
        print('CRLF-normalised: %s=%d bytes, %d line(s)'
              % (rel, len(art['bytes'][rel]), len(art['text'][rel].splitlines())))
    p = art['probes']
    print('extracted: frames=%d proposal=%d delivered=%s sections=%d pills=%d '
          'symbols=%d uses=%d(no-target=%d, unused=%d) ids=%d(dup=%d) anchors=%d(no-target=%d) '
          'td=%d(dash=%d, dash-only=%d) placeholder=%d/%d(n=%d) css-sections=%d media=%d '
          'disabled=%d(clickable=%d, no-title=%d)'
          % (p['high_frames'], p['high_proposal'], p['high_delivered_ids'], p['high_sections'],
             p['high_pills'], p['high_symbols'], p['high_uses'], p['high_use_missing'],
             len(p['high_unused_symbols']), p['high_ids'], len(p['high_dup_ids']),
             p['high_anchor_count'], len(p['high_anchor_missing']), p['high_td'],
             p['high_dash_contains'], p['high_dash_only'], p['high_bracket_places'],
             p['high_bracket_kinds'], p['high_bracket_n'], p['high_css_sections'], p['high_media'],
             p['high_disabled'], p['high_clickable'], p['high_disabled_no_title']))
    print('extracted: low-frames=%s regions=%d trace-rows=%d | proto-probes=%d screens=%d '
          '| board=%d bytes (regenerated=%d) | upstream: labels=%d rules=%d fixture-texts=%d '
          '| rules: surfaces=%d rows=%d(specs=%d decisions=%d blacklist=%s)'
          % (p['low_wf_frames'], p['low_regions'], len(p['low_trace']), p['proto_probes'],
             len(set(p['proto_screens'])), p['board_bytes'], p['board_expected_bytes'],
             len(p['metric_labels']), len(p['rule_ids']), len(p['fixture_texts']),
             len(p['rule_blocks']), p['rule_row_count'], p['rule_spec_count'],
             p['rule_decision_count'],
             p['blacklist_rule'][0] if p['blacklist_rule'] else '-'))
    for fid in reg['wf']['frames']:
        print('extracted: trace[%s] low=%s high=%s'
              % (fid, [r['prd'] for r in p['low_trace'] if r['fid'] == fid][:1],
                 p['high_trace'].get(fid)))
    for n in art['notes']:
        print(n)

    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: this gate reads chart/board sources only; it does NOT check layout or visual '
          'fidelity (still zero coverage), it does NOT make any proposal frame delivered, and it '
          'does NOT mark the platform layer as started.')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
