# -*- coding: utf-8 -*-
"""Verify that every text-bearing paragraph/cell of a .docx survives in the .md."""
import sys, os, re, glob, zipfile, html

T_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>', re.S)
TOKEN_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:br(?:\s[^>]*)?/>|<w:cr/>', re.S)
BLOCK_RE = re.compile(r'<w:tbl>.*?</w:tbl>|<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>', re.S)
STRIP = re.compile(r'[\s`*|#>\-]+')

# Declared, intentional transformation: the converter re-lays out run-on
# "错误码: X 级别: Y 描述: Z 建议措施: W" paragraphs as a markdown table, which
# moves the four labels into the table header.  Such a paragraph cannot be found
# verbatim, so its FIELDS are checked individually instead.
LABELED = re.compile(
    r'^错误码[:：](\S+)\s*级别[:：](\S+)\s*描述[:：](.*?)\s*建议措施[:：](.*)$')
RELOCATED = []

# Declared, intentional POST-CONVERSION EDIT (different from LABELED above: that one
# is a layout artefact of the converter, this one is a real content change made after
# the fact because the source document contradicted a decision taken later).
#
# A plain "ignore these old paragraphs" allowlist would be a hole -- it would stay
# green even if the replacement text never made it into the file, i.e. exactly the
# case where the content was dropped rather than replaced. So a supersession is only
# honoured when the old paragraph is ABSENT *and* the new one is PRESENT.
#   (old text as it appears in the .docx, new text as it appears in the .md, why)
SUPERSEDED = [
    ('SpringBoot+MySQL+Redis+Kafka/gRPC',
     'Spring Boot + PostgreSQL + Redis + Kafka/gRPC',
     '数据库由 MySQL 改为 PostgreSQL（2026-09-23 选型决策）'),
    ('关系型数据库（MySQL/PostgreSQL）：存储财务数据、股票基本信息',
     '关系型数据库（**PostgreSQL 14+**，已确定）：存储财务数据、股票基本信息',
     '数据库由 MySQL 改为 PostgreSQL（2026-09-23 选型决策）'),
]
SUPERSEDED_OK = []


def norm(s):
    s = s.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
    return STRIP.sub('', s)


def fields_kept(p, hay):
    """For a declared-relocated paragraph, every field value must survive."""
    m = LABELED.match(p)
    if not m:
        return False
    for g in m.groups():
        if not g or norm(g) not in hay:
            return False
    RELOCATED.append(norm(m.group(1)))
    return True


def superseded(p, hay):
    """A declared post-conversion edit. Honoured only when the replacement text is
    actually present in the md; otherwise the rewrite dropped the content instead of
    replacing it and the paragraph must still count as missing."""
    for old, new, why in SUPERSEDED:
        if norm(old) == p:
            if norm(new) not in hay:
                print('    SUPERSEDED-BUT-REPLACEMENT-MISSING: %r  (%s)' % (new, why))
                return False
            SUPERSEDED_OK.append(why)
            return True
    return False


def para_texts(xml):
    """every paragraph's normalized text, including those inside table cells"""
    out = []
    for m in BLOCK_RE.finditer(xml):
        block = m.group(0)
        if block.startswith('<w:tbl'):
            for c in re.findall(r'<w:tc>.*?</w:tc>', block, re.S):
                out.extend(para_texts(c))
        else:
            txt = ''.join(html.unescape(t.group(1)) for t in TOKEN_RE.finditer(block) if t.group(1) is not None)
            n = norm(txt)
            if n:
                out.append(n)
    return out


def verify(docx, md_path, label):
    del RELOCATED[:]                    # per-file counter, never accumulate
    del SUPERSEDED_OK[:]
    with zipfile.ZipFile(docx) as z:        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    body = re.search(r'<w:body>(.*)</w:body>', xml, re.S)
    if body:
        xml = body.group(1)
    paras = para_texts(xml)
    md = open(md_path, encoding='utf-8').read()
    hay = norm(md)

    missing = [p for p in paras if p not in hay
               and not fields_kept(p, hay)
               and not superseded(p, hay)]
    print('[%s] paragraphs=%d  missing=%d  relocated-fields-ok=%d  superseded-ok=%d'
          % (label, len(paras), len(missing), len(RELOCATED), len(SUPERSEDED_OK)))
    for p in missing[:10]:
        print('    MISSING: %s' % p[:110])
    return len(missing)


def gate(ok, msg):
    if not ok:
        print('GATE FAIL: ' + msg)
        sys.exit(1)


def struct_check(md, label):
    """Structural gates that paragraph-by-paragraph diffing cannot catch."""
    issues = []
    if '\r' in md:
        issues.append('CRLF present (count=%d)' % md.count('\r'))
    leaks = [i + 1 for i, ln in enumerate(md.split('\n')) if re.search(r'</?w:[a-zA-Z]', ln)]
    if leaks:
        issues.append('raw XML leaked on lines %s' % leaks[:5])
    lines = md.split('\n')
    depth = 0
    opens = 0
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s.startswith('```'):
            continue
        if depth == 0:
            depth = 1
            opens += 1
            if s.lstrip('`').strip() and not re.fullmatch(r'[A-Za-z0-9_+-]+', s.lstrip('`').strip()):
                issues.append('line %d: bad fence info string %r' % (i, s))
        else:
            depth = 0
    if depth != 0:
        issues.append('unclosed code fence')
    if opens == 0:
        issues.append('no code fences found (%s) -> structural check is vacuous' % label)
    for i, ln in enumerate(lines, 1):
        if re.match(r'^#{1,6}[^#\s]', ln):
            issues.append('line %d: heading without space after #: %r' % (i, ln[:40]))
    for m in re.finditer(r'^\|(.+)\|$', md, re.M):
        n = m.group(1).count('|')
        if n < 1:
            issues.append('line with malformed table row: %r' % m.group(0)[:50])
    return issues


if sys.argv[1] == '--selftest':
    # negative control: a deliberately gutted md must be reported as FAIL
    d = sys.argv[2]
    docx = sorted(glob.glob(os.path.join(d, '*.docx')))[0]
    md = os.path.splitext(docx)[0] + '.md'
    broken = os.path.join(os.environ.get('TEMP', '.'), 'broken-selftest.md')
    txt = open(md, encoding='utf-8').read()
    with open(broken, 'w', encoding='utf-8', newline='\n') as f:
        f.write(txt[:len(txt) // 2])       # keep only half the document
    n = verify(docx, broken, 'NEGATIVE-CONTROL')
    gate(n > 0, 'broken md reported 0 missing -> verifier is a no-op')
    # second control: the relocation rule must not swallow intact paragraphs
    n2 = verify(docx, md, 'POSITIVE-CONTROL')
    gate(n2 == 0, 'intact md reported %d missing -> false alarm' % n2)

    # third control: a declared supersession must not become a content hole. Delete
    # the REPLACEMENT text from a copy of the md. The old paragraph is still absent,
    # so a naive allowlist would stay green while the content vanished; the gate must
    # report it.
    prd_docx = os.path.join(d, '智能量化交易平台.docx')
    prd_md = os.path.join(d, '智能量化交易平台.md')
    gate(os.path.exists(prd_docx), 'superseded control needs %s' % prd_docx)
    t = open(prd_md, encoding='utf-8').read()
    before = t
    for _, new, _ in SUPERSEDED:
        t = t.replace(new, '')
    gate(t != before, 'no declared replacement text found in the md -> the SUPERSEDED '
                      'table has drifted away from the file it claims to describe')
    lost = os.path.join(os.environ.get('TEMP', '.'), 'superseded-selftest.md')
    with open(lost, 'w', encoding='utf-8', newline='\n') as f:
        f.write(t)
    n3 = verify(prd_docx, lost, 'SUPERSEDED-CONTROL')
    gate(n3 > 0, 'a superseded paragraph whose replacement vanished was NOT reported -> '
                 'the supersession rule is a content hole')

    # struct_check must actually fire: one INDEPENDENT sample per detector, ordered
    # so that no detector shadows another (an earlier unbalanced fence would swallow
    # the bad info string and make that detector silently never run).
    bad = ''.join([
        '#NoSpace\r\n',                       # heading w/o space + CRLF
        '<w:p>leak</w:p>\n',                  # raw XML leak
        '```bad lang info string\nx\n```\n',  # bad info string, balanced on purpose
        '```python\nno close\n',              # unclosed fence must be LAST
    ])
    got = struct_check(bad, 'STRUCT-CONTROL')
    for want in ('CRLF', 'raw XML leaked', 'bad fence info string',
                 'unclosed code fence', 'heading without space'):
        gate(any(want in g for g in got), 'struct_check missed detector: ' + want)
    print('SELFTEST OK: detects removed content (missing=%d), no false alarm on intact md, '
          'lost replacement caught (missing=%d), struct_check fires on all %d detectors'
          % (n, n3, len(got)))
    sys.exit(0)

d = sys.argv[1]
total = 0
for f in sorted(glob.glob(os.path.join(d, '*.docx'))):
    md = os.path.splitext(f)[0] + '.md'
    gate(os.path.exists(md), 'md not found for ' + os.path.basename(f))
    base = os.path.basename(f)[:34]
    total += verify(f, md, base)
    txt = open(md, encoding='utf-8').read()
    for iss in struct_check(txt, base):
        print('[%s] STRUCT: %s' % (base, iss))
        total += 1

print('verdict: %s (total missing=%d)' % ('PASS' if total == 0 else 'FAIL', total))
sys.exit(0 if total == 0 else 1)
