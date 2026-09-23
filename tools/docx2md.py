# -*- coding: utf-8 -*-
"""
docx -> markdown converter (structure preserving, no external deps).

Handles: headings, paragraphs, bullet lists (numbering), inline bold/italic/verbatim,
styled code paragraphs (style=SourceCode) grouped into fenced blocks, tables,
and the stray ``` fences present in the source documents.
"""
import sys, os, re, glob, zipfile, html

T_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>', re.S)
TOKEN_RE = re.compile(
    r'<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:br(?:\s[^>]*)?/>|<w:cr/>|<w:tab/>|<w:noBreakHyphen/>',
    re.S)
RUN_RE = re.compile(r'<w:r(?:\s[^>]*)?>.*?</w:r>|<w:r(?:\s[^>]*)?/>', re.S)
BLOCK_RE = re.compile(
    r'<w:tbl>.*?</w:tbl>|<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>', re.S)

HEADING_RE = re.compile(r'^Heading(\d)$')
PROSE_NUM_RE = re.compile(r'^\d+(?:\.\d+)*[\s、.．]')
# pseudo headings: authored as plain body text but structurally are headings
PSEUDO_APPENDIX = re.compile(r'^附[录表]\s*[A-Z0-9]?\s*$')
PSEUDO_LETTER = re.compile(r'^([A-Z])\.\s+(\S.*)$')
PSEUDO_NUMBERED = re.compile(r'^(\d+(?:\.\d+)+)\s+(\S.*)$')
# run-on error-code lines -> table
err_code = re.compile(
    r'^错误码[:：]\s*(\S+)\s+级别[:：]\s*(\S+)\s+描述[:：]\s*(.*?)\s+建议措施[:：]\s*(.*)$')
CURLY = {'\u201c': '"', '\u201d': '"', '\u2018': "'", '\u2019': "'"}


def unescape(s):
    return html.unescape(s)


def straighten(s):
    for k, v in CURLY.items():
        s = s.replace(k, v)
    return s


def para_style(px):
    m = re.search(r'<w:pStyle w:val="([^"]+)"', px)
    return m.group(1) if m else ''


def para_ilvl(px):
    if '<w:numPr>' not in px:
        return None
    m = re.search(r'<w:ilvl w:val="(\d+)"/>', px)
    return int(m.group(1)) if m else 0


def raw_text(px):
    """plain text of a paragraph, <w:br/> -> newline"""
    out = []
    for t in TOKEN_RE.finditer(px):
        if t.group(1) is not None:
            out.append(unescape(t.group(1)))
        elif t.group(0).startswith('<w:br') or t.group(0) == '<w:cr/>':
            out.append('\n')
        elif t.group(0) == '<w:tab/>':
            out.append('\t')
        else:
            out.append('-')
    return ''.join(out)


def rich_text(px):
    """paragraph text with inline markdown (bold / italic / `verbatim`)."""
    segs = []       # (text, bold, italic, verbatim)
    for rm in RUN_RE.finditer(px):
        run = rm.group(0)
        rpr = re.search(r'<w:rPr>(.*?)</w:rPr>', run, re.S)
        rpr = rpr.group(1) if rpr else ''
        bold = '<w:b/>' in rpr or re.search(r'<w:b\s+w:val="(?:1|true|on)"', rpr) is not None
        ital = '<w:i/>' in rpr or re.search(r'<w:i\s+w:val="(?:1|true|on)"', rpr) is not None
        verb = '<w:rStyle w:val="VerbatimChar"/>' in rpr
        buf = []
        for t in TOKEN_RE.finditer(run):
            if t.group(1) is not None:
                buf.append(unescape(t.group(1)))
            elif t.group(0).startswith('<w:br') or t.group(0) == '<w:cr/>':
                buf.append('\n')
            elif t.group(0) == '<w:tab/>':
                buf.append(' ')
            else:
                buf.append('-')
        txt = ''.join(buf)
        if not txt:
            continue
        if segs and segs[-1][1:] == (bold, ital, verb):
            segs[-1] = (segs[-1][0] + txt, bold, ital, verb)
        else:
            segs.append((txt, bold, ital, verb))

    out = []
    for txt, bold, ital, verb in segs:
        # verbatim wins over bold/italic (code should not be styled)
        if verb:
            txt = txt.strip()
            if txt:
                out.append('`' + txt + '`')
            continue
        txt = re.sub(r'\s+', ' ', txt)
        if not txt.strip():
            out.append(txt)
            continue
        lead = txt[:len(txt) - len(txt.lstrip())]
        trail = txt[len(txt.rstrip()):]
        core = txt.strip()
        if bold and ital:
            core = '***' + core + '***'
        elif bold:
            core = '**' + core + '**'
        elif ital:
            core = '*' + core + '*'
        out.append(lead + core + trail)
    return ''.join(out).strip()


def table_to_md(tbl):
    rows = re.findall(r'<w:tr(?:\s[^>]*)?>.*?</w:tr>', tbl, re.S)
    grid = []
    for r in rows:
        cells = re.findall(r'<w:tc>.*?</w:tc>', r, re.S)
        vals = []
        for c in cells:
            ps = re.findall(r'<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>', c, re.S)
            parts = [rich_text(p) for p in ps]
            parts = [p for p in parts if p.strip()]
            v = '<br>'.join(parts).replace('|', '\\|')
            vals.append(v.strip())
        grid.append(vals)
    if not grid:
        return []
    ncol = max(len(r) for r in grid)
    grid = [r + [''] * (ncol - len(r)) for r in grid]
    out = []
    header = grid[0]
    if not any(h.strip() for h in header):
        header = [''] * ncol
    out.append('| ' + ' | '.join(header) + ' |')
    out.append('|' + '|'.join([' --- '] * ncol) + '|')
    for r in grid[1:]:
        out.append('| ' + ' | '.join(r) + ' |')
    return out


def detect_lang(code):
    if re.search(r'^\s*syntax\s*=', code, re.M) or re.search(r'^\s*message\s+\w+\s*\{', code, re.M) \
            or re.search(r'^\s*rpc\s+\w+', code, re.M) or re.search(r'^\s*package\s+[\w.]+;', code, re.M):
        return 'protobuf'
    if re.search(r'^\s*(def |class |import |from |@dataclass|async def )', code, re.M) \
            or re.search(r'^\s*(if|for|while|return|with|try|except)\b.*:\s*$', code, re.M) \
            or re.search(r'\bdef \w+\(self', code):
        return 'python'
    if re.search(r'^\s*(输入|输出)\s*[：:]', code, re.M):
        return ''
    return ''


def pseudo_heading(text):
    """Map paragraph text to a heading level if it is structurally a heading.

    Returns (level, title) or None. Conservative on purpose: only short,
    punctuation-free titles are promoted so real prose is never re-titled.
    """
    t = text.strip()
    if not t or len(t) > 40:
        return None
    if PSEUDO_APPENDIX.match(t):
        return (2, t)
    m = PSEUDO_LETTER.match(t)
    if m and '。' not in t and '：' not in t:
        return (3, t)
    m = PSEUDO_NUMBERED.match(t)
    if m and '。' not in t and '：' not in t and '，' not in t and not t.endswith(':'):
        depth = m.group(1).count('.')
        return (2 + depth, t)
    return None


def is_prose(text, style):
    """decide whether a paragraph ends a stray fence"""
    if style.startswith('Heading'):
        return True
    t = text.strip()
    if not t:
        return False
    if '。' in t:
        return True
    if PROSE_NUM_RE.match(t) and ' ' in t:
        return True
    if len(t) > 120 and ' ' not in t[:40]:
        return True
    return False


def error_code_table(lines, stats):
    """Turn a run of consecutive '错误码: X 级别: Y 描述: Z 建议措施: W' lines into a table."""
    out = []

    def blank():
        if out and out[-1] != '':
            out.append('')

    i = 0
    while i < len(lines):
        m = err_code.match(lines[i].strip())
        if not m:
            out.append(lines[i])
            i += 1
            continue
        rows = []
        while i < len(lines):
            if not lines[i].strip():
                i += 1
                continue
            mm = err_code.match(lines[i].strip())
            if not mm:
                break
            rows.append(mm.groups())
            i += 1
        blank()
        out.append('| 错误码 | 级别 | 描述 | 建议措施 |')
        out.append('| --- | --- | --- | --- |')
        for code, lvl, desc, advice in rows:
            cells = [c.replace('|', '\\|') for c in (code, lvl, desc, advice)]
            out.append('| ' + ' | '.join(cells) + ' |')
        out.append('')
        stats['table'] = stats.get('table', 0) + 1
        stats['errcodes'] = stats.get('errcodes', 0) + len(rows)
    return out


def convert(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
        media = [n for n in z.namelist() if n.startswith('word/media/')]
    body = re.search(r'<w:body>(.*)</w:body>', xml, re.S)
    if body:
        xml = body.group(1)

    lines = []
    stats = dict(heading=0, bullet=0, para=0, codeblock=0, table=0)
    fence_open = False          # a real (styled) code block
    fence_buf = []
    stray_lang = None           # stray ``` fence from the source doc

    def blank():
        if lines and lines[-1] != '':
            lines.append('')

    def close_code():
        nonlocal fence_open, fence_buf
        if fence_open:
            code = '\n'.join(fence_buf).rstrip('\n')
            fence_open = False
            fence_buf = []
            if code.strip():
                lang = detect_lang(code)
                lines.append('```' + lang)
                lines.extend(straighten(code).split('\n'))
                lines.append('```')
                blank()
                stats['codeblock'] += 1

    def open_stray(lang):
        nonlocal stray_lang
        stray_lang = lang
        lines.append('```' + lang)

    def close_stray():
        nonlocal stray_lang
        while lines and lines[-1] == '':
            lines.pop()
        lines.append('```')
        blank()
        stray_lang = None
        stats['codeblock'] += 1

    for m in BLOCK_RE.finditer(xml):
        block = m.group(0)

        # ---------- table ----------
        if block.startswith('<w:tbl'):
            close_code()
            if stray_lang is not None:
                close_stray()
            md = table_to_md(block)
            if md:
                lines.extend(md)
                blank()
                stats['table'] += 1
            continue

        style = para_style(block)
        ilvl = para_ilvl(block)
        text = raw_text(block)

        # ---------- styled code ----------
        if style == 'SourceCode':
            if stray_lang is not None:
                lines.extend(straighten(text).split('\n'))
                continue
            if not fence_open:
                fence_open = True
                fence_buf = []
            fence_buf.append(text)
            continue

        # ---------- stray ``` fence ----------
        if stray_lang is not None:
            if text.strip().startswith('```') or is_prose(text, style):
                close_stray()
                if text.strip().startswith('```'):
                    continue
            else:
                lines.extend(straighten(raw_text(block)).split('\n'))
                continue

        if text.strip().startswith('```'):
            # opening fence with optional language
            head = text.strip().lstrip('`')
            lang = ''
            rest = head
            mm = re.match(r'^([A-Za-z0-9_+-]*)\s*(.*)$', head, re.S)
            if mm and mm.group(1) in ('python', 'protobuf', 'json', 'bash', 'sql', 'yaml', 'text'):
                lang = mm.group(1)
                rest = mm.group(2)
            open_stray(lang)
            if rest.strip():
                lines.extend(straighten(rest).split('\n'))
            stats['para'] += 1
            continue

        # ---------- heading ----------
        hm = HEADING_RE.match(style)
        if hm:
            close_code()
            blank()
            lines.append('#' * int(hm.group(1)) + ' ' + rich_text(block))
            blank()
            stats['heading'] += 1
            continue

        close_code()

        # ---------- list item ----------
        if ilvl is not None or style == 'Compact':
            md = rich_text(block)
            if md:
                lvl = ilvl if ilvl is not None else 0
                lines.append('  ' * lvl + '- ' + md)
                stats['bullet'] += 1
            continue

        # ---------- plain paragraph ----------
        md = rich_text(block)
        if md:
            ph = None if md.startswith(('`', '|')) else pseudo_heading(raw_text(block))
            if ph:
                blank()
                lines.append('#' * ph[0] + ' ' + md)
                blank()
                stats['heading'] += 1
                stats.setdefault('promoted', []).append('H%d %s' % ph)
            else:
                lines.append(md)
                blank()
                stats['para'] += 1

    close_code()
    if stray_lang is not None:
        close_stray()
    lines = error_code_table(lines, stats)

    # collapse blank runs, strip leading/trailing blanks
    out = []
    for ln in lines:
        if ln == '' and (not out or out[-1] == ''):
            continue
        out.append(ln.rstrip())
    while out and out[-1] == '':
        out.pop()

    md_text = '\n'.join(out) + '\n'
    stats['media'] = len(media)
    return md_text, stats


def main():
    src, dst = sys.argv[1], sys.argv[2]
    os.makedirs(dst, exist_ok=True)
    for f in sorted(glob.glob(os.path.join(src, '*.docx'))):
        name = os.path.splitext(os.path.basename(f))[0]
        md, st = convert(f)
        outp = os.path.join(dst, name + '.md')
        with open(outp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(md)
        with zipfile.ZipFile(f) as z:
            raw = z.read('word/document.xml').decode('utf-8', 'ignore')
        raw_chars = sum(len(unescape(x)) for x in T_RE.findall(raw))
        md_chars = len(re.sub(r'[`*|\-]', '', md))
        for p in st.get('promoted', []):
            print('    promoted heading: ' + p)
        print('%-52s chars=%6d rawText=%6d  h=%d bullets=%d paras=%d code=%d tables=%d media=%d'
              % (name + '.md', len(md), raw_chars, st['heading'], st['bullet'],
                 st['para'], st['codeblock'], st['table'], st['media']))


if __name__ == '__main__':
    main()
