# -*- coding: utf-8 -*-
"""Dump paragraphs around a keyword, straight from the docx, so truncation can be
attributed to the source document rather than to the converter."""
import re, sys, zipfile, html, glob, os

BLOCK_RE = re.compile(r'<w:tbl>.*?</w:tbl>|<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>', re.S)
TOKEN_RE = re.compile(r'<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:br(?:\s[^>]*)?/>|<w:cr/>', re.S)


def raw_text(px):
    out = []
    for t in TOKEN_RE.finditer(px):
        if t.group(1) is not None:
            out.append(html.unescape(t.group(1)))
        else:
            out.append('\n')
    return ''.join(out)


docx, needle = sys.argv[1], sys.argv[2]
before = int(sys.argv[3]) if len(sys.argv) > 3 else 2
after = int(sys.argv[4]) if len(sys.argv) > 4 else 12
width = int(sys.argv[5]) if len(sys.argv) > 5 else 150
with zipfile.ZipFile(docx) as z:
    xml = z.read('word/document.xml').decode('utf-8', 'ignore')
xml = re.search(r'<w:body>(.*)</w:body>', xml, re.S).group(1)

blocks = [m.group(0) for m in BLOCK_RE.finditer(xml)]
hit = None
for i, b in enumerate(blocks):
    if needle in raw_text(b):
        hit = i
        break
if hit is None:
    print('needle not found in any paragraph')
    sys.exit(2)

print('docx: %s   (%d blocks, needle at #%d)' % (os.path.basename(docx), len(blocks), hit))
for i in range(max(0, hit - before), min(len(blocks), hit + after + 1)):
    st = re.search(r'<w:pStyle w:val="([^"]+)"', blocks[i])
    st = st.group(1) if st else '-'
    txt = raw_text(blocks[i])
    mark = '>>' if i == hit else '  '
    print('%s #%-4d [%-14s] len=%-5d %s' % (mark, i, st, len(txt), txt.replace('\n', '\\n')[:width]))
