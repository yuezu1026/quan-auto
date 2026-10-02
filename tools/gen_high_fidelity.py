#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate `docs/high fidelity/dashboard.html` -- a mechanical copy of the platform page.

Why this file exists
--------------------
The low-fidelity boards (`docs/low fidelity/`) are *design proposals*: they may invent.
This artifact is the opposite kind of thing -- it is the **already implemented** screen
(`platform/web`, the F5 绩效看板) copied out as one self-contained static file, so that
the page can be looked at without a JVM.

"Copied" has to be mechanical, and that is the whole reason this script exists. The
first hand-written draft of that copy had **three spaces in the wrong place**: JSX folds
a multi-line text child into one line (Babel's `cleanJSXElementLiteralChild`), and hand
folding moved a space from before `覆盖` to after `输入`. Nothing failed, because at that
moment nothing compared the text against the real page -- the artifact *looked* copied.
A human doing this reliably is the thing that does not scale, so the copy is generated:

  * styles      -- `platform/web/src/styles.css` inlined BYTE FOR BYTE;
  * structure   -- tags copied from `App.jsx`, text folded by the JSX rule above;
  * numbers     -- read one by one from `platform/web/test/report-view.fixture.json`;
  * the curve   -- recomputed with the same formula as `curvePoints()` in `App.jsx`;
  * copy claims -- `--dump=` compares the result against a real React render.

It is NOT a gate. Nothing in `tools/run_all_gates.py` calls it; it can be red or green
and no verdict moves. It only makes the artifact reproducible and its claims checkable.

Usage
-----
    python tools/gen_high_fidelity.py                    # generate + structural checks
    python tools/gen_high_fidelity.py --dump=DOM.json    # + byte diff vs a real render
    python tools/gen_high_fidelity.py --dump=DOM.json --selftest   # prove the diff can fail

`--selftest` mutates one derived character and asserts the same `--dump=` comparison
turns red, then restores it. A comparison that cannot fail is not a comparison. How to
produce `DOM.json` is written down in `docs/high fidelity/README.md` (it needs Node).

Exit codes: 0 = all checks passed, 1 = a finding, 2 = harness could not run (fail-closed).
"""

import argparse
import html
import json
import os
import pathlib
import re
import subprocess
import sys

sys.dont_write_bytecode = True

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = ROOT / 'platform' / 'web'
OUT = ROOT / 'docs' / 'high fidelity' / 'dashboard.html'

# The four files whose last commit the artifact pins. Deliberately not HEAD and not the
# whole tree: if it pinned HEAD, generating the artifact would immediately record the
# artifact's own commit, and the number would be stale the moment it was committed.
SOURCES = (
    'platform/web/src/App.jsx',
    'platform/web/src/styles.css',
    'platform/web/src/api.js',
    'platform/web/test/report-view.fixture.json',
)


def harden_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


# ---------------------------------------------------------------------------------
# JSX -> HTML
#
# Babel trims each line of a text child, strips only the leading spaces of non-first
# lines and only the trailing spaces of non-last lines, and joins the surviving lines
# with a single space. Getting this wrong is silent -- it moves a space, it does not
# raise -- so it is implemented once, here, and every text node goes through it.
# ---------------------------------------------------------------------------------
def fold_jsx_text(value):
    lines = re.split(r'\r\n|\n|\r', value)
    last_non_empty = 0
    for i, line in enumerate(lines):
        if re.search(r'[^ \t]', line):
            last_non_empty = i
    out = ''
    count = len(lines)
    for i, line in enumerate(lines):
        is_first = i == 0
        is_last = i == count - 1
        text = line.replace('\t', ' ')
        if not is_first:
            text = re.sub(r'^ +', '', text)
        if not is_last:
            text = re.sub(r' +$', '', text)
        if text:
            if i != last_non_empty:
                text += ' '
            out += text
    return out


def jsx_to_html(src, subs):
    """Static JSX fragment -> the HTML React would serialize.

    `subs` maps a whitespace-normalized expression body to the text to print for it.
    Anything the converter does not recognize RAISES: guessing here would put invented
    copy into an artifact whose entire claim is that it invents nothing.
    """
    subs = dict(subs)
    out = []
    pending = []
    i = 0
    while i < len(src):
        char = src[i]
        if char == '<':
            end = src.index('>', i)
            tag = src[i:end + 1]
            if '{' in tag:
                raise SystemExit('HARNESS-FAIL: attribute expression in tag %r -- '
                                 'this converter does not guess attribute values' % tag)
            if pending:
                out.append(html.escape(fold_jsx_text(''.join(pending)), quote=False))
                pending = []
            out.append(tag.replace('className=', 'class=').replace('htmlFor=', 'for='))
            i = end + 1
        elif char == '{':
            end = src.index('}', i)
            body = re.sub(r'\s+', ' ', src[i + 1:end]).strip()
            if pending:
                out.append(html.escape(fold_jsx_text(''.join(pending)), quote=False))
                pending = []
            literal = re.fullmatch(r'"([^"]*)"', body)
            if literal:
                out.append(html.escape(literal.group(1), quote=False))
            elif body in subs:
                out.append(subs[body])
            else:
                raise SystemExit('HARNESS-FAIL: unregistered expression %r' % body)
            i = end + 1
        else:
            pending.append(char)
            i += 1
    if pending:
        out.append(html.escape(fold_jsx_text(''.join(pending)), quote=False))
    return ''.join(out)


def cut(src, open_tag, close_tag):
    start = src.index(open_tag)
    end = src.index(close_tag, start) + len(close_tag)
    return src[start:end]


# ---------------------------------------------------------------------------------
# The one comparison that can fail: our copy vs a real React render
# ---------------------------------------------------------------------------------
def extract_main(text):
    start = text.index('<main>')
    return text[start:text.index('</main>', start) + len('</main>')]


def canonical(text):
    """Whitespace-only canonicalization, applied to BOTH sides identically.

    Inter-tag whitespace is a formatting choice (this file is pretty-printed, React's
    `innerHTML` is not). Whitespace INSIDE text is content, and survives -- that is the
    whole point: it is what caught the three misplaced spaces.
    """
    text = text.replace('\r\n', '\n')
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'> ', '>', text)
    text = re.sub(r' <', '<', text)
    return text.strip()


def first_divergence(ours, theirs):
    limit = min(len(ours), len(theirs))
    for i in range(limit):
        if ours[i] != theirs[i]:
            return i
    return limit if len(ours) != len(theirs) else -1


def diff_against_dump(artifact, dump_path, mutate=None):
    """-> (ok, lines). `mutate` is (index, new_char) for the selftest sample."""
    real = json.loads(pathlib.Path(dump_path).read_text(encoding='utf-8'))['html']
    ours = canonical(extract_main(artifact))
    if mutate is not None:
        index, char = mutate
        ours = ours[:index] + char + ours[index + 1:]
    theirs = canonical(extract_main(real))
    lines = ['对拍（真渲染 DOM）: 归一化后 ours=%d theirs=%d' % (len(ours), len(theirs))]
    at = first_divergence(ours, theirs)
    if at < 0:
        lines.append('  差异：无（照抄这层逐字符一致）')
        return True, lines
    lines.append('  首个分歧 @%d' % at)
    lines.append('    ours  : %r' % ours[max(0, at - 60):at + 60])
    lines.append('    theirs: %r' % theirs[max(0, at - 60):at + 60])
    return False, lines


# ---------------------------------------------------------------------------------
def build():
    css_bytes = (WEB / 'src' / 'styles.css').read_bytes()
    css = css_bytes.decode('utf-8').replace('\r\n', '\n')
    app = (WEB / 'src' / 'App.jsx').read_text(encoding='utf-8')
    view = json.loads((WEB / 'test' / 'report-view.fixture.json').read_text(encoding='utf-8'))
    checks = []

    # 「逐字节内联」要成立，样式源必须是**纯 CRLF**：本仓库的 .gitattributes 是 `* -text`
    # （提交什么字节就检出什么字节），产物也按 CRLF 写，所以源的 CRLF 被原样带过去。
    # 源若是混排（有的行 LF），内存里的字符串会与磁盘字节不一致 —— 那正是这条要拦的事。
    checks.append(('styles.css 是纯 CRLF（内联可无损往返）',
                   css_bytes.count(b'\n') == css_bytes.count(b'\r\n')
                   and css_bytes.count(b'\r') == css_bytes.count(b'\r\n')))

    # ---- the curve: App.jsx's own arithmetic, not a re-description of it
    width, height, pad = 720, 180, 8
    curve = [point['equity'] for point in view['curve']]
    low, high = min(curve), max(curve)
    inner_w, inner_h = width - pad * 2, height - pad * 2
    points = ' '.join(
        '%.2f,%.2f' % (
            pad + index * inner_w / (len(curve) - 1),
            pad + (1 - (0.5 if high == low else (value - low) / (high - low))) * inner_h,
        )
        for index, value in enumerate(curve)
    )
    checks.append(('曲线点数 == 夹具点数', len(points.split()) == len(curve)))

    # ---- the <option> list: the reports that actually exist on disk
    report_dir = ROOT / '.rounds' / 'i1'
    report_ids = sorted(p.stem for p in report_dir.glob('*.json'))
    unreadable = set()
    for report_id in report_ids:
        try:
            json.loads((report_dir / (report_id + '.json')).read_text(encoding='utf-8'))
        except Exception:
            unreadable.add(report_id)
    options = ''.join(
        '<option value="%s">%s%s</option>'
        % (rid, rid, '（读不出来）' if rid in unreadable else '')
        for rid in report_ids
    )
    checks.append(('报告目录里至少一份可读报告', bool(report_ids) and len(unreadable) < len(report_ids)))

    # ---- header / curve caption / footer: folded out of App.jsx, never re-typed
    window = view['window']
    header = jsx_to_html(cut(app, '<header>', '</header>'), {})
    curve_foot = jsx_to_html(
        cut(app, '<p className="curve-foot">', '</p>'),
        {
            'window ? window[0] + " → " + window[1] : ""': html.escape('%s → %s' % (window[0], window[1])),
            'view.curve.length': str(len(curve)),
        },
    )
    footer = jsx_to_html(cut(app, '<footer>', '</footer>'), {})

    # ---- the data-driven middle: read field by field from the fixture
    counts = view['counts']
    meta_rows = [
        ('报告', view['id']),
        ('标的', view.get('symbol') or '—'),
        ('窗口', window[0] + ' → ' + window[1]),
        ('状态', view.get('status') or '—'),
        ('策略', (view.get('strategyId') or '—') + ' / ' + (view.get('strategyVersion') or '—')),
        ('数据版本', view.get('dataVersion') or '—'),
        ('结构', '曲线 %s 点 · 成交 %s 笔 · 委托 %s 条'
                 % (counts['equityCurve'], counts['trades'], counts['orders'])),
    ]
    meta_html = ''.join('<div><dt>%s</dt><dd>%s</dd></div>'
                        % (html.escape(str(key)), html.escape(str(value)))
                        for key, value in meta_rows)
    order = []
    for metric in view['metrics']:
        if metric['group'] not in order:
            order.append(metric['group'])
    groups_html = ''.join(
        '<section class="group"><h3>%s</h3><table><tbody>%s</tbody></table></section>'
        % (html.escape(group), ''.join(
            '<tr><th scope="row" title="%s">%s</th><td class="value">%s</td></tr>'
            % (metric['key'], html.escape(str(metric['label'])), html.escape(str(metric['text'])))
            for metric in view['metrics'] if metric['group'] == group))
        for group in order
    )
    checks.append(('14 条指标行（夹具）', len(view['metrics']) == 14))

    # ---- every class App.jsx uses must have a rule in the inlined stylesheet
    used = sorted(set(re.findall(r'className="([^"]+)"', app)))
    missing = [c for c in used if not any(('.' + part) in css for part in c.split())]
    checks.append(('App.jsx 用到的 class 全有规则（缺=%s）' % missing, not missing))

    # ---- what commit the copy describes (see SOURCES above)
    try:
        sha = subprocess.run(['git', '-C', str(ROOT), 'log', '-1', '--format=%h', '--', *SOURCES],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--', *SOURCES],
                                    capture_output=True, text=True).stdout.strip())
    except Exception:
        sha, dirty = 'unknown', True
    checks.append(('源提交可取得', sha != 'unknown'))

    screen = """  <div id="root">
    <main>
__HEADER__

      <div class="toolbar">
        <label for="report">报告</label>
        <select id="report">
__OPTIONS__
        </select>
      </div>

      <dl class="meta">
__META__
      </dl>

      <section class="curve">
        <h3>净值曲线</h3>
        <svg viewBox="0 0 720 180" preserveAspectRatio="none" role="img" aria-label="净值曲线">
          <polyline points="__POINTS__"></polyline>
        </svg>
__FOOT__
      </section>

      <div class="groups">
__GROUPS__
      </div>

__FOOTER__
    </main>
  </div>
""".replace('__HEADER__', header).replace('__FOOT__', curve_foot) \
   .replace('__FOOTER__', footer).replace('__OPTIONS__', options) \
   .replace('__META__', meta_html).replace('__POINTS__', points) \
   .replace('__GROUPS__', groups_html)

    note_head = """  <div class="hifi-note">
    <p class="hifi-title">高保真基准稿 · 回测绩效看板（低保真的 F5）</p>
    <p>这是一份<b>静态照抄</b>：结构 / class 名 / 文案由 <code>tools/gen_high_fidelity.py</code>
      从 <code>platform/web/src/App.jsx</code> 现折叠（含 JSX 的换行折叠规则）；
      样式<b>逐字节内联</b>自 <code>platform/web/src/styles.css</code>；
      数字逐字段取自 <code>platform/web/test/report-view.fixture.json</code>
      （由真报告 <code>.rounds/i1/report-seed7-a.json</code> 投影，已与
      <code>quanauto/dashboard.py</code> 的 <code>METRIC_SPECS</code> + <code>format_metric</code> 逐条对拍，14/14 一致）。
      <b>手抄这一步已被取消</b>：初稿手抄时把三个空格抄错了位置，而当时没有任何东西比对文本。</p>
    <p>真源提交 <code>__SHA__</code>（__DIRTY__）。怎么重新生成 / 怎么复核：见
      <code>docs/high fidelity/README.md</code>。</p>
    <p class="hifi-warn"><b>它没有牙</b>：不被任何门禁读取，不是规格、不解除任何缺口、<b>不等于</b>平台层开工。
      它只是那两份<b>低保真</b>旁边的一份「照着实现画」的基准 —— 低保真正在做的事是<b>替代真页面</b>（不起 JVM 就能看）。</p>
    <p class="hifi-warn">它能证的是「照抄这层是机械的」：本稿与一次<b>真 React 渲染</b>的 DOM 做过逐字符对拍
      （归一化空白后差异为 0）。但那是一次<b>快照式核对</b>，不是常驻判据 —— 没有活着的 JVM 时，
      两侧的<b>渲染结果</b>不会自动被复核。</p>
  </div>
""".replace('__SHA__', sha).replace(
        '__DIRTY__',
        '这四个真源文件有<b>未提交改动</b> ⇒ 本稿可能已落后' if dirty
        else '这四个真源文件自那以后没有改动')

    mark_top = ('  <div class="hifi-mark">以下整块是 <code>platform/web</code> 真页面的结构照抄'
                '（<code>body &gt; #root &gt; main</code>）。它上面与下面才是本稿自己的注释块。</div>\n')
    mark_bottom = ('  <div class="hifi-mark">照抄结束。以下为本稿的说明块，'
                   '<b>不属于</b>被照抄的页面。</div>\n')

    note_tail = """  <div class="hifi-note">
    <p class="hifi-title">照抄出来的两处「不该修」的现象</p>
    <ul>
      <li>抬头写「成交 <b>3</b> 笔」、指标表里「成交笔数」是 <b>1</b> —— 两个数都来自同一份报告
        （<code>counts.trades</code> = 3 = 流水条数；<code>performance.total_trades</code> = 1 = 指标口径）。
        <b>页面照抄、不解释</b>。设计稿若把它改成一致，那不是修 bug，是发明一个上游没有的事实。</li>
      <li>文案里的<b>单个空格</b>（如 <code>； 本页</code>、<code>是 同一份</code>、
        <code>、 platform-web-parity</code>）：来自 JSX 里显式的 <code>{" "}</code> 与换行折叠。
        真页面就是这样；改掉它，这一稿就不再是照抄。这些空格现在由生成器现折叠，
        <b>不再手抄</b>（手抄版曾在 <code>输入是一份</code> / <code>夹具）覆盖</code> 两处错了位置）。</li>
    </ul>
    <p class="hifi-title">同一页还有两个真实态（本稿<b>没有</b>画，见 README 的说明）</p>
    <ul>
      <li><b>读取中</b>：<code>&lt;span class="busy"&gt;读取中…&lt;/span&gt;</code>，选择框在报告列表为空时
        <code>disabled</code>；此时抬头 / 曲线 / 指标<b>都不渲染</b>（不会被旧数字垫着）。</li>
      <li><b>出错</b>：<code>&lt;p class="error"&gt;…&lt;/p&gt;</code>，且渲染被
        <code>view &amp;&amp; !error</code> 挡着 ⇒ <b>出错时不显示旧数据</b>。</li>
    </ul>
    <p class="hifi-warn">还有一件事要说清楚：本稿 <b>0 个 <code>&lt;script&gt;</code></b> ⇒
      上面那个「报告」选择框<b>是死的</b>（切了不会换报告，也不会去请求 REST）。
      它被照抄下来是为了核对样式与文案，<b>不是摆一个假开关</b>；
      要看真能切的那种，用 <code>docs/low fidelity/prototype.html</code>。</p>
  </div>
"""

    style_extra = """    /* ---- 以下为本稿自己的注释块样式（不属于被照抄的页面；颜色一律走 :root 的 token） ---- */
    .hifi-note {
      max-width: 1000px;
      margin: 0 auto 20px;
      padding: 12px 14px;
      border: 2px dashed var(--ink);
      border-radius: 8px;
      background: #fff;
      font-size: 13px;
    }
    .hifi-note .hifi-title {
      margin: 12px 0 6px;
      font-size: 14px;
      font-weight: 700;
    }
    .hifi-note .hifi-title:first-child {
      margin-top: 0;
    }
    .hifi-note p {
      margin: 6px 0;
    }
    .hifi-note ul {
      margin: 6px 0;
      padding-left: 20px;
    }
    .hifi-note li {
      margin-bottom: 4px;
    }
    .hifi-note .hifi-warn {
      color: var(--bad);
    }
    .hifi-mark {
      max-width: 1000px;
      margin: 0 auto 10px;
      padding: 4px 10px;
      border-left: 4px solid var(--ink);
      background: #f2f4f7;
      font-size: 12px;
      color: var(--muted);
    }
"""

    doc = """<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>回测绩效看板 · 高保真基准稿（按 platform/web 照抄）</title>
    <style>
/* ===== platform/web/src/styles.css -- 逐字节内联（不要在此处手改） ===== */
__CSS__
/* ===== 上面是照抄的样式；下面是本稿自己的注释块样式 ===== */
__EXTRA__
    </style>
  </head>
  <body>
__NOTE_HEAD__
__MARK_TOP____SCREEN____MARK_BOTTOM__
__NOTE_TAIL__
  </body>
</html>
""".replace('__CSS__', css).replace('__EXTRA__', style_extra) \
   .replace('__NOTE_HEAD__', note_head).replace('__MARK_TOP__', mark_top) \
   .replace('__SCREEN__', screen).replace('__MARK_BOTTOM__', mark_bottom) \
   .replace('__NOTE_TAIL__', note_tail)
    return doc, checks


def css_lf():
    """被内联的样式，按 LF 读进来做**内存**比对（磁盘那份 CRLF 由 main() 单独核）。"""
    return (WEB / 'src' / 'styles.css').read_bytes().decode('utf-8').replace('\r\n', '\n')


def structural_checks(doc, checks):
    checks.append(('styles.css 逐字节内联（生成结果里）', css_lf() in doc))
    checks.append(('恰好 1 个 <main>', doc.count('<main>') == 1 and doc.count('</main>') == 1))
    checks.append(('恰好 1 个 id="root"', doc.count('id="root"') == 1))
    checks.append(('0 个 <script>（无脚本）', doc.count('<script') == 0))
    checks.append(('14 条 td.value', doc.count('<td class="value">') == 14))
    checks.append(('7 条 dt（抬头 7 项）', doc.count('<dt>') == 7))
    checks.append(('4 个指标分组 section', doc.count('<section class="group">') == 4))
    checks.append(('没有 selected 属性（真渲染里也没有：它是 DOM 属性）', ' selected' not in doc))

    # 注释块的色值必须**复用**被照抄样式里的字面量，不许自造一个新颜色。
    # （朴素写法 `doc.count('#fff')` 在这里是错的：风格是子串计数，而 css 里有 `#fff5f4`。）
    marker = '/* ===== 上面是照抄的样式；下面是本稿自己的注释块样式 ===== */'
    extra = doc[doc.index(marker):doc.index('</style>')]
    hex_re = re.compile(r'#[0-9a-fA-F]{3,8}\b')
    invented = sorted(set(hex_re.findall(extra)) - set(hex_re.findall(css_lf())))
    checks.append(('注释块没有自造色值（被照抄样式里没有的=%s）' % (invented or '无'), not invented))
    return checks


def main():
    harden_stdout()
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--dump', default=None,
                        help='a JSON dump {"html": ...} of a real React render of the same screen')
    parser.add_argument('--selftest', action='store_true',
                        help='mutate one character and prove the --dump comparison goes red')
    args = parser.parse_args()

    doc, checks = build()
    structural_checks(doc, checks)

    if args.selftest and not args.dump:
        print('HARNESS-FAIL: --selftest needs --dump (there is nothing else that can fail)')
        return 2

    ok = True
    for label, passed in checks:
        print('%-70s %s' % (label, 'OK' if passed else 'FAIL'))
        ok = ok and passed

    # 先落盘，再拿**磁盘上的字节**做后面的对拍与内联断言。
    # 理由：这里所有的绿都要能对着提交进仓库的那份字节复核；
    # 「内存里的字符串是好的」和「文件里的字节是好的」是两件事（本仓库 .gitattributes 是 `* -text`）。
    if not args.selftest:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, 'w', encoding='utf-8', newline='\r\n') as fh:
            fh.write(doc)
        raw = OUT.read_bytes()
        css_bytes = (WEB / 'src' / 'styles.css').read_bytes()
        disk = [
            ('落盘后等于生成结果（只差 \\n -> \\r\\n）', raw == doc.replace('\n', '\r\n').encode('utf-8')),
            ('styles.css 在磁盘上逐字节内联（不是「内存里像」）', css_bytes in raw),
            ('产物是纯 CRLF（与 .gitattributes 的 `* -text` 相配）',
             raw.count(b'\n') == raw.count(b'\r\n') and raw.count(b'\r') == raw.count(b'\r\n')),
        ]
        for label, passed in disk:
            print('%-70s %s' % (label, 'OK' if passed else 'FAIL'))
            ok = ok and passed
        print('bytes=%d  ->  %s' % (len(raw), OUT))
        subject = raw.decode('utf-8')
    else:
        subject = doc

    if args.dump:
        good, lines = diff_against_dump(subject, args.dump)
        for line in lines:
            print(line)
        if args.selftest:
            mutant = diff_against_dump(subject, args.dump, mutate=(200, 'X'))
            mutated_ok = mutant[0]
            print('触发测试（把第 200 个字符改成 X）：%s' % ('FAIL 如预期' if not mutated_ok else '仍报一致 -> 对拍没有牙'))
            print('  ' + mutant[1][-1])
            good = good and not mutated_ok
        ok = ok and good

    print('VERDICT: %s' % ('OK' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
