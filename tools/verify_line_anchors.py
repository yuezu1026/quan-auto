#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: every line-number reference in the docs/source must be BOUND to something.

Why this file exists
--------------------
This repository has a long history of writing line numbers as addresses:

    「见数据中心契约 … 那一行」「本模块 … 那一行」「附录 B21 第 … 行」

A line number is the one kind of address that rots silently. Insert a paragraph above
it and the number points somewhere else; nothing errors, nothing warns, and the reader
who follows it lands on an unrelated table row. It has already happened here at least
twice (a 47-line drift recorded in `docs/历史订正.md`, and the 2026-10-01 sweep that
found three more). The lesson was written down, and the METHOD was changed to
"locate by content, not by line number" — but nothing ever checked it, so the habit
came straight back. `docs/开发工作流规范.md` lists this as an open hole (D4), and until
now the mitigation was "a human re-measures after every doc change".

What this gate can and cannot do
--------------------------------
It cannot tell whether a line number is CORRECT in the semantic sense (does the number
still mean what the sentence claims?). That needs a reader. What it CAN do is refuse to
let an unbound number exist, where "bound" means one of:

  B1  a date (YYYY-MM-DD) on the same line       -> "this was the value at the time"
  B2  a commit hash on the same line             -> pinned to a revision
  B3  a version triple on the same line          -> the number belongs to version X of
                                                    something else (a third-party file)
  B4  a drift warning on the same line
      (会漂 / 按内容 / 不再钉 / 不再写死 / 自我描述 / 本节的地址 / 会老去)
                                                 -> the text itself says "do not trust
                                                    this number"
  B5  a registry entry in `tools/line-anchors.json`, one of:
        kind=numbered    text points into a repo file: we re-derive the number from the
                         text and assert the expected text is on that exact line. This
                         is the only kind that catches a drift, and it FAILS the gate.
        kind=row         text is a ROW number of a table in a repo file: we find the
                         table by its header line and assert the expected text is in
                         that row. Same teeth as `numbered`, one axis over.
        kind=quoted      the number sits inside a quotation of a past measurement, a
                         tool's output, or a bug's artifact. Bookkeeping + a reason.
        kind=structural  the number is a format/schema index, not a location: report
                         line numbers, data-row numbers, or a line-index convention.
                         Needs a reason.

The registry is NOT an automatic exemption: an entry is a human statement that the
number does not address a repository file at a specific line, or that we agree to keep
it verified. That statement is the reviewable artifact, and the run prints the counts by
kind so a swelling `quoted` bucket is visible.

Exit codes: 0 = all anchors bound, 1 = at least one finding, 2 = selftest setup failure.

NOTE for whoever edits this file: do not write a line-number literal into it. The gate
scans `tools/*.py`, so a literal here is a hit that it would report against itself. The
selftest fixtures build their numbers with `'L' + str(n)` for exactly that reason.
"""

import json
import os
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, 'tools', 'line-anchors.json')

# ---------------------------------------------------------------------------------
# Scope. Deliberately explicit rather than '**/*.py': the scan must not wander into
# build output (platform/*/node_modules, target, dist) where thousands of third-party
# files live, and the exclusions are checked against directory components so a folder
# named 'target' anywhere is skipped.
# ---------------------------------------------------------------------------------
EXCLUDE_DIRS = frozenset(('.venv', 'node_modules', 'target', 'dist', 'build',
                          '__pycache__', '.git', '.mypy_cache', '.pytest_cache'))
SCOPE_DIRS = ('quanauto', 'tools', 'tests', 'docs')


def in_scope(rel):
    parts = rel.replace('\\', '/').split('/')
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return False
    if rel == 'CONTEXT.md':
        return True
    if len(parts) == 2 and parts[0] == 'docs' and parts[1].endswith('.md'):
        return True
    if len(parts) == 2 and parts[0] in ('quanauto', 'tools', 'tests') \
            and parts[1].endswith('.py'):
        return True
    if parts[0] == 'platform' and parts[-1].endswith(('.md', '.jsx', '.java', '.tsx')):
        return True
    if parts[0] == '.github' and parts[-1].endswith('.md'):
        return True
    return False


def collect(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace('\\', '/')
            if in_scope(rel):
                out.append(rel)
    return out


# ---------------------------------------------------------------------------------
# What counts as a line-number reference.
#
# The 'L' form requires a boundary on both sides, so a number that is merely the prefix
# of a longer number, or that is glued to an identifier, is not a hit; the '第 … 行' form
# tolerates the half-width spaces this repository actually uses. Both forms are matched
# against the CRLF-normalised text -- a bare '\n' regex over a CRLF file would match
# nothing and report a clean zero.
# ---------------------------------------------------------------------------------
PATTERNS = (
    re.compile(r'(?<![0-9A-Za-z_])L(\d{2,4})(?![0-9])'),
    re.compile(r'第\s?(\d{1,4})\s?行'),
)

DATE_RE = re.compile(r'\b20\d\d-\d\d-\d\d\b')
# A commit hash must contain a letter, otherwise a 7-digit number would pass as one.
HASH_RE = re.compile(r'(?=[0-9a-f]{7,40}\b)[0-9a-f]*[a-f][0-9a-f]*\b')
VERSION_RE = re.compile(r'\b\d+\.\d+\.\d+\b')
DRIFT_WORDS = ('漂', '按内容', '不再钉', '不再写死', '自我描述', '本节的地址', '会老去')

KINDS = ('numbered', 'row', 'quoted', 'structural')

# ---------------------------------------------------------------------------------
# The '↔' incident, again: a line read out of a malformed sample can contain any
# character, and printing it raised UnicodeEncodeError WHILE the findings were being
# written -- which lost the whole verdict and made a hard failure look like a crash.
# Unencodable characters degrade to '?' instead of aborting the run.
# ---------------------------------------------------------------------------------
def harden_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def read_text(path):
    """UTF-8 (BOM tolerated), CRLF -> LF. Normalising before matching is mandatory."""
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def count_occurrences(text, needle):
    """How many times `needle` occurs as a reference (not as a prefix of a longer one).

    A registry entry stores the exact matched substring, so the count here must use the
    same boundary rule as the extractor: a shorter number must not be counted inside a
    longer one, and an identifier suffix must not count as a reference.
    """
    if needle.startswith('L') and needle[1:].isdigit():
        rx = re.compile(r'(?<![0-9A-Za-z_])%s(?![0-9])' % re.escape(needle))
        return len(rx.findall(text))
    return text.count(needle)


def scan(root):
    """-> (files, hits, stats). hits: {(rel, text): [(line_no, line_text), ...]}"""
    files = collect(root)
    hits = {}
    for rel in files:
        text = read_text(os.path.join(root, rel))
        for i, line in enumerate(text.split('\n'), 1):
            for rx in PATTERNS:
                for m in rx.finditer(line):
                    hits.setdefault((rel, m.group(0)), []).append((i, line))
    return files, hits


def binding_of(line):
    """The on-the-same-line binding (B1-B4), or None."""
    if DATE_RE.search(line):
        return 'B1-date'
    if HASH_RE.search(line):
        return 'B2-hash'
    if VERSION_RE.search(line):
        return 'B3-version'
    for w in DRIFT_WORDS:
        if w in line:
            return 'B4-drift-word:%s' % w
    return None


def check(root, entries):
    issues = []
    stats = {'files': 0, 'hits': 0, 'distinct': 0, 'bound': 0,
             'registered': 0, 'numbered_verified': 0, 'row_verified': 0,
             'entry_kinds': {}}

    files, hits = scan(root)
    stats['files'] = len(files)
    stats['hits'] = sum(len(v) for v in hits.values())
    stats['distinct'] = len(hits)

    # GATE: extraction empty. Every detector below iterates over `hits`; a regex that
    # stopped matching (a renamed convention, a re-encoded file) would leave them all
    # spinning in the void while still printing a clean verdict -- the most convincing
    # possible lie. Refuse to pass.
    if not hits:
        issues.append(('GATE', '在 %d 个文件里一个行号引用都没提取到 —— 提取器失配时'
                               '下面的检查全在空转，拒绝通过' % len(files)))
    if not entries:
        issues.append(('GATE', '登记表 %s 是空的 —— 本门禁只剩提取，没有任何一条'
                               '被真正核对' % os.path.basename(REGISTRY)))

    index = {}
    for ent in entries:
        key = (ent.get('file'), ent.get('text'))
        if key in index:
            issues.append(('LA-DUP', '登记表里 %s :: %r 有重复条目'
                           % (ent.get('file'), ent.get('text'))))
        index[key] = ent

    for key, occ in sorted(hits.items()):
        rel, text = key
        ent = index.get(key)
        if ent is None:
            unbindable = [(ln, line) for ln, line in occ if binding_of(line) is None]
            if not unbindable:
                stats['bound'] += len(occ)
                continue
            for ln, line in unbindable:
                issues.append(('LA-UNBOUND', '%s:%d 裸行号 %r（同一行没有日期 / 提交号 / '
                                             '版本号 / 「会漂」类措辞，登记表里也没有）'
                                             '  行内容: %s'
                               % (rel, ln, text, line.strip()[:90])))
            stats['bound'] += len(occ) - len(unbindable)
            continue

        n = count_occurrences(read_text(os.path.join(root, rel)), text)
        if n != ent.get('count'):
            issues.append(('LA-COUNT', '%s :: %r 登记 count=%s，实际出现 %d 次 '
                                       '（产物改过而登记表没跟上）'
                           % (rel, text, ent.get('count'), n)))
        kind = ent.get('kind')
        stats['entry_kinds'][kind] = stats['entry_kinds'].get(kind, 0) + 1
        if kind not in KINDS:
            issues.append(('LA-KIND', '%s :: %r 的 kind=%r 不在 %s 里'
                           % (rel, text, kind, '/'.join(KINDS))))
            continue
        if kind in ('quoted', 'structural'):
            if not (ent.get('why') or '').strip():
                issues.append(('LA-NO-WHY', '%s :: %r 是 %s，必须写 why（这句话就是'
                                            '被审阅的那个声明）' % (rel, text, kind)))
            else:
                stats['registered'] += len(occ)
            continue

        # kind == 'row': the same teeth as `numbered`, along the table-row axis.
        if kind == 'row':
            target, table, expect = (ent.get('target'), ent.get('table'),
                                     ent.get('expect'))
            if not target or not table or not expect:
                issues.append(('LA-NOEP', '%s :: %r 是 row，必须给 target / table / '
                                          'expect' % (rel, text)))
                continue
            tpath = os.path.join(root, target)
            if not os.path.exists(tpath):
                issues.append(('LA-NOFILE', '%s :: %r 的 target=%s 不存在'
                               % (rel, text, target)))
                continue
            tlines = read_text(tpath).split('\n')
            heads = [i for i, ln in enumerate(tlines) if ln.strip() == table.strip()]
            if len(heads) != 1:
                issues.append(('LA-TABLE', '%s :: %r 要用表头 %r 定位表格，但它在 %s 里'
                                           '命中 %d 次（要么表被改了，要么表头不唯一）'
                               % (rel, text, table[:40], target, len(heads))))
                continue
            rows = []
            for ln in tlines[heads[0] + 2:]:
                if not ln.startswith('|'):
                    break
                rows.append(ln)
            num = int(re.search(r'\d+', text).group(0))
            if not 1 <= num <= len(rows):
                issues.append(('LA-RANGE', '%s :: %r 指向 %s 那张表的第 %d 行数据，'
                                           '但表里只有 %d 行'
                               % (rel, text, target, num, len(rows))))
                continue
            if expect not in rows[num - 1]:
                issues.append(('LA-DRIFT', '%s :: %r 已漂：%s 那张表的第 %d 行现在是 %r，'
                                           '登记表期望它含 %r'
                               % (rel, text, target, num, rows[num - 1].strip()[:60],
                                  expect[:40])))
                continue
            stats['row_verified'] += 1
            continue

        # kind == 'numbered': a line number pointing into a repo file.
        target = ent.get('target')
        expect = ent.get('expect')
        if not target or not expect:
            issues.append(('LA-NOEP', '%s :: %r 是 numbered，必须给 target 与 expect'
                           % (rel, text)))
            continue
        tpath = os.path.join(root, target)
        if not os.path.exists(tpath):
            issues.append(('LA-NOFILE', '%s :: %r 的 target=%s 不存在'
                           % (rel, text, target)))
            continue
        tlines = read_text(tpath).split('\n')
        num = int(re.search(r'\d+', text).group(0))
        if not 1 <= num <= len(tlines):
            issues.append(('LA-RANGE', '%s :: %r 指向 %s 第 %d 行，但该文件只有 %d 行'
                           % (rel, text, target, num, len(tlines))))
            continue
        if expect not in tlines[num - 1]:
            issues.append(('LA-DRIFT', '%s :: %r 已漂：%s 第 %d 行现在是 %r，登记表'
                                       '期望它含 %r'
                           % (rel, text, target, num, tlines[num - 1].strip()[:70],
                              expect[:60])))
            continue
        if read_text(tpath).count(expect) > 1:
            issues.append(('LA-WEAK', '%s :: %r 的 expect=%r 在 %s 里出现不止一次，'
                                      '这条核对分不清是哪一处'
                           % (rel, text, expect[:40], target)))
        stats['numbered_verified'] += 1

    for ent in entries:
        if (ent.get('file'), ent.get('text')) not in hits:
            issues.append(('LA-STALE', '登记表里的 %s :: %r 在产物里已经找不到'
                                       '（多半是你把它改掉了）—— 删掉这条'
                           % (ent.get('file'), ent.get('text'))))

    # GATE: no teeth left. A registry of only quoted/structural entries reduces this
    # gate to bookkeeping, and the whole point is the verified kinds.
    checked = stats['numbered_verified'] + stats['row_verified']
    if entries and not checked and not (stats['entry_kinds'].get('numbered')
                                        or stats['entry_kinds'].get('row')):
        issues.append(('GATE', '登记表里没有一条 numbered / row 被核对 ⇒ '
                               '这门禁只剩记账，漂了也不会红'))
    return issues, stats


def load_registry(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    return data.get('entries', [])


def main():
    harden_stdout()
    if '--selftest' in sys.argv:
        return selftest()

    entries = load_registry(REGISTRY)
    if entries is None:
        print('GATE FAIL: 登记表 %s 不存在' % REGISTRY)
        return 1

    issues, stats = check(ROOT, entries)
    print('scope: %d file(s) scanned (docs/**/*.md, quanauto|tools|tests/*.py, '
          'CONTEXT.md, platform, .github/**/*.md)' % stats['files'])
    print('anchors: hits=%d distinct=%d auto-bound=%d registered=%d '
          'numbered-verified=%d row-verified=%d'
          % (stats['hits'], stats['distinct'], stats['bound'], stats['registered'],
             stats['numbered_verified'], stats['row_verified']))
    print('registry: entries=%d %s'
          % (len(entries), ' '.join('%s=%d' % kv for kv in sorted(stats['entry_kinds'].items()))))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: 本门禁判的是「这个行号被绑住了」，不是「这个行号是对的」。B1-B4 的绑定'
          '只是要求同一行自己声明了它是快照 / 外部版本 / 会漂；kind=numbered / row 才'
          '真的按内容核对那一条。')
    print('NOTE: 要修一个 LA-UNBOUND，优先把行号改成按内容定位（引那一格的文字），其次'
          '才是在登记表里把它登记成 numbered 并给出 target/expect。')
    return 0 if not issues else 1


# ---------------------------------------------------------------------------------
# Selftest. One sample per detector, both vacuity guards, and a clean control. The
# fixtures are built in a temp tree so the samples never touch the real registry, and
# every number is assembled from a string so this file stays clean of line-number
# literals (it scans itself).
# ---------------------------------------------------------------------------------
FIXTURE_DATED = '| 表 | （2026-09-25 实测）见 L%d 那一行 |\n'
FIXTURE_BARE = '| 表 | 见 L%d 那一行 |\n'
FIXTURE_HOME = '| 表 | 见 第 %d 行 那一格 |\n'


def build_tree(base, files):
    for rel, content in files.items():
        full = os.path.join(base, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8', newline='') as f:
            f.write(content)


def numbered_entry(rel, num, target, expect):
    return {'file': rel, 'text': 'L%d' % num, 'count': 1, 'kind': 'numbered',
            'target': target, 'expect': expect,
            'why': '自测夹具'}


def selftest():
    ok = True
    base = tempfile.mkdtemp(prefix='line-anchors-selftest-')
    try:
        # Two real-ish targets: `docs/plan.md` holds the anchors, `quanauto/mod.py`
        # holds the lines they point at, and `docs/spec.md` holds a table whose rows
        # are addressed by the `row` kind.
        target = 'quanauto/mod.py'
        tlines = ['# 模块\n', '\n'] + ['# 填充\n'] * 9 + ['def beta():\n', '    return 2\n']
        table = '| 模块 | 场景 |'
        table_target = 'docs/spec.md'
        ttlines = ['## 测试要求\n', '\n', table + '\n', '| --- | --- |\n',
                   '| 规则加载 | a |\n', '| 分层覆盖 | b |\n', '| 校验路径 | c |\n']
        row_anchor = '第 %d 行' % 2
        clean_files = {
            'CONTEXT.md': FIXTURE_DATED % 31 + FIXTURE_HOME % 2,
            'docs/plan.md': (FIXTURE_BARE % 12) + row_anchor,
            target: ''.join(tlines),
            table_target: ''.join(ttlines),
        }
        clean_entries = [
            numbered_entry('docs/plan.md', 12, target, 'def beta():'),
            {'file': 'docs/plan.md', 'text': row_anchor, 'count': 1, 'kind': 'row',
             'target': table_target, 'table': table, 'expect': '分层覆盖',
             'why': '自测夹具'},
            # The third kind that must not be a blank cheque: a home-grown index with a
            # written-down reason. Order matters -- `NEG-row-out-of-range` rebuilds the
            # list and expects the row entry at index 1.
            {'file': 'CONTEXT.md', 'text': row_anchor, 'count': 1,
             'kind': 'structural', 'why': '自测夹具：自造的行号索引'},
        ]

        def scenario(tag, files, entries, code=None, want_clean=False):
            nonlocal ok
            root = os.path.join(base, tag)
            build_tree(root, files)
            issues, stats = check(root, entries)
            codes = sorted(set(k for k, _ in issues))
            hit = (not issues) if want_clean else (code in codes)
            print('  [%s] issues=%d codes=%s hits=%d numbered=%d row=%d %s'
                  % (tag, len(issues), codes, stats['hits'],
                     stats['numbered_verified'], stats['row_verified'],
                     'OK' if hit else 'MISSED'))
            if not hit:
                for k, m in issues:
                    print('        (was) [%s] %s' % (k, m[:110]))
            ok = ok and hit

        # CONTROL: the real repository. Reported, never asserted clean -- the gate must
        # be able to fail on the real artifacts, that is the point of running it.
        real = load_registry(REGISTRY)
        if real is None:
            print('  [control-real-repo] SKIPPED (registry missing)')
        else:
            issues, stats = check(ROOT, real)
            print('  [control-real-repo] issues=%d codes=%s hits=%d distinct=%d '
                  'bound=%d registered=%d numbered-verified=%d row-verified=%d'
                  % (len(issues), sorted(set(k for k, _ in issues)), stats['hits'],
                     stats['distinct'], stats['bound'], stats['registered'],
                     stats['numbered_verified'], stats['row_verified']))

        # POSITIVE: everything bound (a date + a home-grown row index) plus one real
        # numbered check that passes. Without this, every detector below could be one
        # that can only ever fire.
        scenario('POSITIVE-clean', clean_files, clean_entries, want_clean=True)

        # LA-UNBOUND: a bare number nobody bound and nobody registered.
        f = dict(clean_files)
        f['docs/other.md'] = FIXTURE_BARE % 44
        scenario('NEG-unbound-bare-number', f, clean_entries, 'LA-UNBOUND')

        # LA-DRIFT: the target line changed under the registered anchor. This is the
        # detector the whole gate exists for.
        f = dict(clean_files)
        f[target] = ''.join(tlines).replace('def beta():', 'def gamma():')
        scenario('NEG-drift-target-line-moved', f, clean_entries, 'LA-DRIFT')

        # LA-COUNT: the anchored text now occurs twice; the entry still says once.
        f = dict(clean_files)
        f['docs/plan.md'] = (FIXTURE_BARE % 12) + (FIXTURE_BARE % 12) + row_anchor
        scenario('NEG-count-changed', f, clean_entries, 'LA-COUNT')

        # LA-DRIFT on the row axis: the table gained a row above the addressed one,
        # which is the exact silent shift this kind exists to catch.
        f = dict(clean_files)
        f[table_target] = ''.join(ttlines).replace('| 规则加载 | a |\n',
                                                   '| 新增行 | z |\n| 规则加载 | a |\n')
        scenario('NEG-row-drift', f, clean_entries, 'LA-DRIFT')

        # LA-TABLE: the header used to locate the table is gone (renamed / restyled).
        f = dict(clean_files)
        f[table_target] = ''.join(ttlines).replace('| 模块 |', '| 成员 |')
        scenario('NEG-row-table-header-lost', f, clean_entries, 'LA-TABLE')

        # LA-RANGE: the addressed row does not exist yet.
        f = dict(clean_files)
        f['docs/plan.md'] = (FIXTURE_BARE % 12) + ('第 %d 行' % 9)
        e = [clean_entries[0], dict(clean_entries[1], text='第 %d 行' % 9)]
        scenario('NEG-row-out-of-range', f, e, 'LA-RANGE')

        # LA-STALE: the entry survived, the anchor did not (the usual aftermath of
        # rewriting an anchor into content-based wording).
        f = dict(clean_files)
        f['docs/plan.md'] = FIXTURE_DATED % 12
        scenario('NEG-registry-entry-stale', f, clean_entries, 'LA-STALE')
        # LA-KIND: a kind nobody defined would otherwise be silently skipped.
        e = [dict(clean_entries[0], kind='expected')]
        scenario('NEG-unknown-kind', clean_files, e, 'LA-KIND')

        # LA-NO-WHY: quoted / structural entries are the two that can rubber-stamp, so
        # the reason string is mandatory.
        f = dict(clean_files)
        f['docs/other.md'] = FIXTURE_BARE % 44
        e = clean_entries + [{'file': 'docs/other.md', 'text': 'L%d' % 44, 'count': 1,
                              'kind': 'structural'}]
        scenario('NEG-structural-without-why', f, e, 'LA-NO-WHY')

        # LA-NOEP / LA-RANGE / LA-NOFILE: the three ways a numbered entry can be
        # unusable as written.
        e = [dict(clean_entries[0], expect=None)]
        scenario('NEG-numbered-without-expect', clean_files, e, 'LA-NOEP')
        e = [dict(clean_entries[0], text='L%d' % 900, count=1)]
        f = dict(clean_files)
        f['docs/plan.md'] = FIXTURE_BARE % 900
        scenario('NEG-numbered-out-of-range', f, e, 'LA-RANGE')
        e = [dict(clean_entries[0], target='quanauto/nope.py')]
        scenario('NEG-numbered-target-missing', clean_files, e, 'LA-NOFILE')

        # LA-DUP: two entries for the same anchor means one of them is dead weight and
        # the counts can disagree without anyone noticing which one is authoritative.
        e = clean_entries + [numbered_entry('docs/plan.md', 12, target, 'def beta():')]
        scenario('NEG-duplicate-entry', clean_files, e, 'LA-DUP')

        # GATE: nothing to scan at all (a file with no reference). Both the empty-tree
        # and the empty-registry guard must be live. Note the numbers in these fixtures
        # are assembled from a string: `L6` would be a one-digit number and the
        # extractor requires two, so a sample built with `%d` % 6 would silently
        # exercise nothing -- the exact failure mode this gate reports on others.
        scenario('GATE-empty-tree', {'CONTEXT.md': '# 没有任何行号\n'},
                 clean_entries, 'GATE')
        scenario('GATE-empty-registry', clean_files, [], 'GATE')

        # GATE: a registry with no `numbered` / `row` entry is all bookkeeping.
        # Mutating the kinds of EVERY entry (not just one) is what reaches the branch --
        # a single mutated entry would leave the guard correctly silent and the sample
        # would report some other code, which looks exactly like a missing detector.
        e = []
        for ent in clean_entries:
            e.append(dict(ent, kind='quoted', why='自测夹具'))
        for k in ('table', 'expect', 'target'):
            e[1].pop(k, None)
        f = dict(clean_files)
        f['docs/plan.md'] = FIXTURE_DATED % 12 + row_anchor
        scenario('GATE-registry-without-verified-kinds', f, e, 'GATE')

        print('SELFTEST %s'
              % ('OK: 每个探测器都开火了、干净样本没误报、两条空转守卫都咬人'
                 if ok else 'FAIL'))
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
