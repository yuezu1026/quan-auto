#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dangling cross-document appendix references.

Why this exists
---------------
The contract documents cite each other by appendix label -- a citation is the word for
"appendix" followed by a label such as ``B12`` / ``E1`` / ``C``.  An agent told that the
contracts are 强约束 reads such a citation as "go look that up".  A citation pointing at
nothing is therefore *worse* than no citation at all: the agent either invents the
ruling or silently drops the constraint, and neither failure shows up in a diff.

Renumbering an appendix (``B16`` -> ``B15``) or deleting one during a rewrite leaves
every citation to it dangling, and no other gate notices:

  * md-fidelity           only asks whether the paragraphs of the .docx still exist;
  * contract-signature    only compares parameter lists and field names;
  * core-contract-refs    only checks type names inside ```python blocks.

Labels live in prose, so they fall between every existing net.  This is that net.

Checks (each runs independently; no check returns early and shadows a later one)
  A1  every contract document listed below was found.  A missing one makes all of the
      labels it owns look dangling, so it is reported as its own defect.
  A2  the cited label exists: the letter is some document's appendix letter and, when
      the citation carries an item number, that item is defined somewhere.
  A3  when the citation names its owner (数据中心契约 / DC 契约 / 主契约 / 风控契约,
      directly adjacent to the citation), the label must exist in *that* document.  A
      label that only resolves elsewhere is a cross-document mistake, which is exactly
      what the union check in A2 would wave through.

Vacuity guards (each fails on its own; see the 「提取为空必须判 FAIL」 discipline)
  APX-NO-SCAN-SCOPE     the tracked-file scope could not be enumerated
  APX-NO-INDEX          the appendix index came out empty (letters and/or items)
  APX-NO-CITATIONS      not one citation was extracted
  APX-NO-NUMERIC-CITES  no citation carried an item number, so the item half of A2 and
                        all of A3 would run as no-ops over an empty set
  APX-THIN-SCAN         fewer than MIN_NUMERIC_CITES item-carrying citations: a floor
                        set far below the current count, so an extractor collapse cannot
                        be mistaken for "the documents got shorter"

Scope and boundary
  * Scope is ``git ls-files``: the files a reader of a clone actually sees.  Gitignored
    one-off probes under .rounds/ are deliberately out of scope -- they are not
    deliverables, and auditing scratch work would make this gate red for the wrong
    reason.  If the tracked set cannot be enumerated the gate refuses (APX-NO-SCAN-SCOPE)
    rather than auditing an unknown subset, which would be indistinguishable from clean.
  * Static text audit only.  It proves "the label you were sent to exists"; it proves
    nothing about whether the ruling behind that label is accurate or still current.
  * Files that do not decode as UTF-8 are skipped and counted (they are binaries).
  * This tool must never print the appendix word next to a label.  tools/gates-report.txt
    is tracked, and it embeds this output: a message containing a dangling citation
    would be re-scanned on the next run and turn into a phantom finding.  That is also
    why the samples below assemble the word from two pieces (``KW``) instead of writing
    it whole -- this file is inside its own scan scope.

Exit codes: 0 = no findings, 1 = findings, 2 = selftest setup failure.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Documents that own appendix labels.  'risk' owns none of its own (it cites the core
# document) and is listed anyway: a citation naming it must be reported as a mistake
# instead of resolving by accident through another document's labels.
CONTRACT_FILES = {
    'core': 'docs/智能量化交易平台-核心模块接口契约文档.md',
    'dc': 'docs/智能量化交易平台-数据中心接口契约文档.md',
    'risk': 'docs/智能量化交易平台-风控层接口契约文档.md',
    'prd': 'docs/智能量化交易平台.md',
}

# The appendix word, assembled from two pieces on purpose (see the module docstring).
KW = '附' + '录'

# Owner prefixes, honoured only when directly adjacent to the citation.  A prefix that
# merely appears earlier in the line is *not* taken as the owner: falling back to the
# union check is the conservative direction, because a mis-attributed owner would turn
# legitimate prose into a false finding (and a false finding gets the rule weakened).
OWNER_RE = re.compile(
    r'(数据中心接口契约|数据中心契约|DC[ \t]*契约|DC契约'
    r'|核心模块接口契约|核心模块契约|主契约|核心契约'
    r'|风控层接口契约|风控层契约|风控契约)'
    r'[\s*「『（(的之]{0,3}$')
OWNER_KEY = {
    '数据中心接口契约': 'dc', '数据中心契约': 'dc',
    'DC 契约': 'dc', 'DC契约': 'dc',
    '核心模块接口契约': 'core', '核心模块契约': 'core',
    '主契约': 'core', '核心契约': 'core',
    '风控层接口契约': 'risk', '风控层契约': 'risk', '风控契约': 'risk',
}
OWNER_WINDOW = 40

CITE_RE = re.compile(KW + r'[\s*]{0,3}([A-Z])(\d+(?:\.\d+)*)?(?![A-Za-z0-9])')
CONT_RE = re.compile(r'[\s*]{0,3}([/、~～])[\s*]{0,3}([A-Z])(\d+(?:\.\d+)*)?'
                     r'(?![A-Za-z0-9])')

# Heading shapes.  Both contracts label their appendices on the heading line, but the
# core one uses a bare `## 附录` followed by `### A. title`, so the bare form has to
# switch the parser into "these A..E letters are appendices" mode.
APX_HEAD_RE = re.compile(r'^#{1,6}[ \t]*' + KW + r'[ \t]*([A-Z])(?![A-Za-z])')
BARE_APX_RE = re.compile(r'^#{1,6}[ \t]*' + KW + r'[ \t]*$')
BARE_LETTER_RE = re.compile(r'^#{2,6}[ \t]*([A-Z])[.、:：][ \t]*')
ITEM_HEAD_RE = re.compile(r'^#{2,6}[ \t]*([A-Z])(\d+(?:\.\d+)*)\.?(?![0-9])')
ITEM_BOLD_RE = re.compile(r'^\*\*([A-Z])(\d+(?:\.\d+)*)[.、]')
TOP_HEAD_RE = re.compile(r'^##[ \t]')

# A floor, not a target: the repository currently carries ~90 item-carrying citations.
# Raising it is a deliberate commit; tripping it means the extractor went half-blind.
MIN_NUMERIC_CITES = 20
RANGE_MAX = 20


class ScopeError(Exception):
    """The tracked-file scope could not be enumerated."""


def read_text(path):
    """Read as UTF-8 dropping the BOM, then normalise CRLF.

    Normalising first is mandatory: a bare '\\n' regex over a CRLF file matches nothing,
    every extractor below would silently return empty, and the run would report a
    deceptively clean verdict.
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def read_maybe_text(path):
    """Return the file as normalised text, or None when it is not UTF-8 text.

    Returning None (rather than raising) keeps one binary fixture from aborting the
    audit, and the caller counts the skips so "0 citations" cannot hide behind them.
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    if b'\x00' in raw[:4096]:
        return None
    try:
        return raw.decode('utf-8-sig').replace('\r\n', '\n')
    except UnicodeDecodeError:
        return None


def tracked_files(root):
    """`git ls-files`, with core.quotePath disabled so CJK paths stay literal.

    Without `-c core.quotePath=false` git escapes non-ASCII paths (\\346\\231\\...) and
    every docs/ path would fail to open -- which the skip counter would then absorb.
    """
    cmd = ['git', '-c', 'core.quotePath=false', 'ls-files']
    try:
        proc = subprocess.run(cmd, cwd=root, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScopeError('%s: %s' % (type(exc).__name__, exc))
    if proc.returncode != 0:
        raise ScopeError('git ls-files exit=%s: %s'
                         % (proc.returncode,
                            proc.stderr.decode('utf-8', 'replace').strip()[:160]))
    out = proc.stdout.decode('utf-8', 'replace').replace('\r\n', '\n')
    return [ln for ln in out.split('\n') if ln.strip()]


def owner_of(before):
    """The document a citation names, or 'any' when it names none."""
    m = OWNER_RE.search(before[-OWNER_WINDOW:])
    if m is None:
        return 'any'
    return OWNER_KEY[m.group(1)]


def expand_range(prev, letter, num):
    """E5~E8 -> E6, E7, E8.  Anything that is not a same-letter numeric range is left as
    the two endpoints, which are still checked."""
    p_letter, p_num = prev
    if num and p_num and letter == p_letter and '.' not in num and '.' not in p_num:
        start, stop = int(p_num), int(num)
        if 0 < stop - start <= RANGE_MAX:
            return [(letter, str(n)) for n in range(start + 1, stop + 1)]
    return [(letter, num)]


def parse_citations(text):
    """Extract {line, letter, num, owner} for every citation in `text`.

    Shapes that actually occur in this repository (all of them are covered by the
    extractor samples in selftest()):

        <KW>** B12**     bold markers inside the gap            (quanauto/datasources.py)
        <KW> **B13**     bold wrapped around the label          (docs/迭代计划.md)
        <KW> A6 / B10    slash-joined continuation              (DC contract, appendix D)
        <KW> B16.1/B17.1 slash-joined without spaces            (quanauto/pgstore.py)
        <KW> E5~E8       inclusive range                        (docs/迭代计划.md)
        <KW> A/B/C       bare letters joined by slashes         (docs/开工前缺口清单.md)
        DC 契约<KW> C    owner named immediately before the label

    Deliberately *not* extracted: bare labels with no appendix word in front of them
    (`ck_a`, the smoke file's own B1..B19 namespace, the gate detector ids A0..A11).
    Extracting those would report a collision the documents do not have.
    """
    out = []
    for lineno, line in enumerate(text.split('\n'), 1):
        pos = 0
        while True:
            m = CITE_RE.search(line, pos)
            if m is None:
                break
            owner = owner_of(line[:m.start()])
            tokens = [(m.group(1), m.group(2))]
            prev = tokens[0]
            end = m.end()
            while True:
                c = CONT_RE.match(line, end)
                if c is None:
                    break
                letter, num = c.group(2), c.group(3)
                if c.group(1) in ('~', '～'):
                    tokens.extend(expand_range(prev, letter, num))
                else:
                    tokens.append((letter, num))
                    prev = (letter, num)
                end = c.end()
            for letter, num in tokens:
                out.append({'line': lineno, 'letter': letter, 'num': num,
                            'owner': owner})
            pos = end
    return out


def build_index(text):
    """Return (letters, items) -- the appendix labels one document defines.

    Section tracking is not optional: the DC contract carries a `### D1 — ...` design
    decision list *outside* any appendix, and counting those as appendix items would
    make the item check pass for labels nobody ever registered as an appendix.
    """
    letters, items = set(), set()
    in_apx = bare = False
    for line in text.split('\n'):
        m = APX_HEAD_RE.match(line)
        if m is not None:
            letters.add(m.group(1))
            in_apx, bare = True, False
            continue
        if BARE_APX_RE.match(line) is not None:
            in_apx, bare = True, True
            continue
        if TOP_HEAD_RE.match(line) is not None:
            in_apx, bare = False, False
            continue
        if not in_apx:
            continue
        if bare:
            m = BARE_LETTER_RE.match(line)
            if m is not None:
                letters.add(m.group(1))
        for pat in (ITEM_HEAD_RE, ITEM_BOLD_RE):
            m = pat.match(line)
            if m is not None:
                items.add(m.group(1) + m.group(2))
    return letters, items


def resolves(label, items):
    """True when `label` is defined, or when only its sub-items are.

    A citation to `B16` must resolve on a document whose headings are `### B16` and
    `#### B16.1`: a citation to a section is not dangling just because the sub-items
    carry the headings.  Without this the DC contract would report ~15 false findings.
    """
    return label in items or any(i.startswith(label + '.') for i in items)


def audit(index, cites, tracked, missing=(), scope_error=None,
          min_numeric=MIN_NUMERIC_CITES):
    """Returns [(code, message)].  Every check appends independently."""
    findings = []
    letters, items = set(), set()
    for key in index:
        letters |= index[key][0]
        items |= index[key][1]

    # --- vacuity guards -----------------------------------------------------------
    for rel in missing:
        findings.append(('APX-MISSING-CONTRACT',
                         'contract document not found: %s -- every label it owns will '
                         'be reported as dangling' % rel))
    if scope_error or tracked == 0:
        findings.append(('APX-NO-SCAN-SCOPE',
                         'could not enumerate the tracked-file scope (%s) -- auditing an '
                         'empty or unknown file set would pass vacuously'
                         % (scope_error or 'git ls-files returned 0 files')))
    if not letters or not items:
        findings.append(('APX-NO-INDEX',
                         'appendix index is empty (letters=%d items=%d) -- A2/A3 cannot '
                         'run; any citation reported below is measured against a broken '
                         'index' % (len(letters), len(items))))
    if not cites:
        findings.append(('APX-NO-CITATIONS',
                         'no citation was extracted at all -- the extractor is not wired '
                         'up (or the documents lost every cross reference) and a "clean" '
                         'verdict here would mean nothing'))
    numeric = [c for c in cites if c['num']]
    if cites and not numeric:
        findings.append(('APX-NO-NUMERIC-CITES',
                         '%d citation(s) but none carries an item number -- the item half '
                         'of A2 and all of A3 would run over an empty set'
                         % len(cites)))
    elif len(numeric) < min_numeric:
        findings.append(('APX-THIN-SCAN',
                         'only %d item-carrying citation(s), floor is %d -- an extractor '
                         'collapse is indistinguishable from "the documents got shorter"'
                         % (len(numeric), min_numeric)))

    # --- A2 / A3 ------------------------------------------------------------------
    for c in cites:
        letter, num = c['letter'], c['num']
        label = letter + (num or '')
        # 'path' is filled in by scan_all(); selftest samples are built in memory.
        where = '%s:%d' % (c.get('path', '<sample>'), c['line'])
        if c['owner'] == 'any':
            if letter not in letters:
                findings.append(('APX-UNKNOWN-APPENDIX',
                                 "%s: label '%s' -- no contract document owns appendix "
                                 "%s, so the reader is sent nowhere"
                                 % (where, label, letter)))
            elif num and not resolves(label, items):
                findings.append(('APX-DANGLING-ITEM',
                                 "%s: label '%s' -- appendix %s exists but item %s is "
                                 "defined nowhere" % (where, label, letter, label)))
        else:
            o_letters, o_items = index.get(c['owner'], (set(), set()))
            if letter not in o_letters:
                findings.append(('APX-WRONG-CONTRACT',
                                 "%s: label '%s' is attributed to the %s document, but "
                                 "that document owns no appendix %s (it owns: %s)"
                                 % (where, label, c['owner'], letter,
                                    ''.join(sorted(o_letters)) or 'none')))
            elif num and not resolves(label, o_items):
                findings.append(('APX-DANGLING-ITEM',
                                 "%s: label '%s' -- attributed to the %s document, which "
                                 "owns appendix %s but no item %s"
                                 % (where, label, c['owner'], letter, label)))
    return findings


def scan_all(root):
    """Audit one checkout.  Returns (findings, stats)."""
    index, cites, skipped, missing = {}, [], [], []
    for key in sorted(CONTRACT_FILES):
        rel = CONTRACT_FILES[key]
        path = os.path.join(root, rel)
        if os.path.exists(path):
            index[key] = build_index(read_text(path))
        else:
            missing.append(rel)
    try:
        files = tracked_files(root)
        scope_error = None
    except ScopeError as exc:
        files, scope_error = [], str(exc)
    for rel in files:
        text = read_maybe_text(os.path.join(root, rel))
        if text is None:
            skipped.append(rel)
            continue
        for c in parse_citations(text):
            c['path'] = rel
            cites.append(c)
    findings = audit(index, cites, len(files), missing, scope_error)
    stats = {'index': index, 'cites': cites, 'skipped': skipped, 'missing': missing,
             'tracked': len(files), 'read': len(files) - len(skipped),
             'scope_error': scope_error}
    return findings, stats


def report(findings, stats):
    print('scan scope: git ls-files=%d file(s), read as text=%d, skipped=%d'
          % (stats['tracked'], stats['read'], len(stats['skipped'])))
    if stats['skipped']:
        print('  skipped (binary/undecodable): %s'
              % ', '.join(sorted(stats['skipped'])[:6]))
    print('appendix index: %s'
          % ' '.join('%s=%dL/%dI' % (k, len(v[0]), len(v[1]))
                     for k, v in sorted(stats['index'].items())))
    cites = stats['cites']
    numeric = [c for c in cites if c['num']]
    owned = [c for c in cites if c['owner'] != 'any']
    print('citations: %d total, %d with an item label, %d naming an owner, in %d file(s)'
          % (len(cites), len(numeric), len(owned),
             len(set(c['path'] for c in cites))))
    for code, msg in findings:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not findings else 'FAIL', len(findings)))


def selftest():
    """One sample per detector, plus a clean control.

    Samples are separate from each other so no detector can shadow another: the index
    sample never feeds the citation samples, and the A3 sample has a companion control
    with the *same* label but no owner, which is what proves the finding is about the
    attribution and not about the label.

    The appendix word is assembled from two pieces (KW) rather than written whole: this
    file is inside the gate's scan scope, so a literal citation-shaped sample would
    become a finding on the very next real run.
    """
    ok = True

    def expect(tag, got, want):
        nonlocal ok
        hit = got == want
        print('  [%s] %s got=%s want=%s' % (tag, 'OK' if hit else 'MISSED', got, want))
        ok = ok and hit

    def codes(findings):
        return sorted(set(c for c, _ in findings))

    # --- extractor -----------------------------------------------------------------
    forms = (KW + '** B12** 与 ' + KW + ' **B13**\n'
             '见 ' + KW + ' A6 / B10，以及 ' + KW + ' E5~E8\n'
             'DC ' + '契约' + KW + ' C\n')
    expect('extract-forms',
           [(c['letter'], c['num'], c['owner']) for c in parse_citations(forms)],
           [('B', '12', 'any'), ('B', '13', 'any'), ('A', '6', 'any'),
            ('B', '10', 'any'), ('E', '5', 'any'), ('E', '6', 'any'),
            ('E', '7', 'any'), ('E', '8', 'any'), ('C', None, 'dc')])

    # Negative control for the extractor: bare labels with no appendix word in front
    # must not be read as citations (the smoke file owns B1..B19, the gate owns A0..A11).
    bare = ("SELECT unnest(ARRAY['ck_a','B1','B19','D1']);\n"
            "the detector ids `A0`~`A11` are not appendix items\n")
    expect('extract-ignores-bare-labels', parse_citations(bare), [])

    # The owner is honoured only when adjacent; a mention earlier in the line must fall
    # back to the union check, otherwise ordinary prose becomes a false finding.
    expect('extract-owner-must-be-adjacent',
           [c['owner'] for c in parse_citations('数据中心契约的源列名映射见' + KW + ' B1\n')],
           ['any'])

    # --- index --------------------------------------------------------------------
    dc_doc = ('## ' + KW + ' A：x\n'
              '### A1. one\n'
              '## ' + KW + ' B：y\n'
              '### B1. one\n'
              '#### B16.1 deep\n'
              '### B16. tail-dot\n'
              '## ' + KW + '\n'
              '### C. bare-letter\n'
              '**E1. bold item**\n'
              '## 三、not an appendix\n'
              '### D1 — outside\n')
    dc_letters, dc_items = build_index(dc_doc)
    # Letters and items are collected by two different rules on purpose: the fixture
    # owns the item E1 (a bold list entry) while owning no letter E of its own, which is
    # exactly the core contract's shape and what makes the A3 sample below decisive.
    expect('index-letters', sorted(dc_letters), ['A', 'B', 'C'])
    expect('index-items', sorted(dc_items), ['A1', 'B1', 'B16', 'B16.1', 'E1'])
    expect('index-ignores-labels-outside-an-appendix', 'D1' in dc_items, False)

    core_doc = ('## ' + KW + '\n### E. 登记\n**E1. one**\n')
    core_letters, core_items = build_index(core_doc)
    expect('index-bare-appendix-mode',
           (sorted(core_letters), sorted(core_items)), (['E'], ['E1']))

    index = {'dc': (dc_letters, dc_items), 'core': (core_letters, core_items)}

    # --- A2 ------------------------------------------------------------------------
    expect('A2-unknown-appendix',
           codes(audit(index, parse_citations(KW + ' Z9\n'), 5, min_numeric=0)),
           ['APX-UNKNOWN-APPENDIX'])
    expect('A2-dangling-item',
           codes(audit(index, parse_citations(KW + ' B99\n'), 5, min_numeric=0)),
           ['APX-DANGLING-ITEM'])
    # A citation to a section whose sub-items carry the headings must still resolve.
    sub_only = build_index('## ' + KW + ' B：y\n#### B16.1 deep\n')
    expect('A2-resolves-through-sub-item',
           codes(audit({'dc': sub_only}, parse_citations(KW + ' B16\n'), 5,
                       min_numeric=0)), [])

    # --- A3 ------------------------------------------------------------------------
    expect('A3-wrong-contract',
           codes(audit(index, parse_citations('DC ' + '契约' + KW + ' E1\n'), 5,
                       min_numeric=0)), ['APX-WRONG-CONTRACT'])
    # same label, no owner named -> the union resolves it, so A3 is about attribution.
    expect('A3-union-fallback-control',
           codes(audit(index, parse_citations(KW + ' E1\n'), 5, min_numeric=0)), [])

    # --- clean control -------------------------------------------------------------
    expect('clean-sample',
           codes(audit(index, parse_citations(KW + ' A1 / B16\n'), 5, min_numeric=0)),
           [])

    # --- vacuity guards ------------------------------------------------------------
    expect('guard-no-citations',
           codes(audit(index, [], 5, min_numeric=0)), ['APX-NO-CITATIONS'])
    expect('guard-no-index-has-its-own-code',
           'APX-NO-INDEX' in codes(audit({'dc': (set(), set())},
                                         parse_citations(KW + ' B1\n'), 5,
                                         min_numeric=0)), True)
    expect('guard-missing-contract',
           'APX-MISSING-CONTRACT' in codes(audit(index, parse_citations(KW + ' A1\n'), 5,
                                                 missing=['docs/gone.md'], min_numeric=0)),
           True)
    expect('guard-no-scan-scope',
           'APX-NO-SCAN-SCOPE' in codes(audit(index, parse_citations(KW + ' A1\n'), 0,
                                              min_numeric=0)), True)
    three = '\n'.join(KW + ' ' + lab for lab in ('A1', 'B1', 'E1'))
    expect('guard-thin-scan',
           codes(audit(index, parse_citations(three), 5)), ['APX-THIN-SCAN'])
    expect('guard-thin-scan-silent-at-zero-floor',
           codes(audit(index, parse_citations(three), 5, min_numeric=0)), [])

    # --- the real artifacts, as a non-vacuity assertion -----------------------------
    # Deliberately does NOT assert "0 findings": that is the gate's own job.  What is
    # asserted here is that the scan reached real files and extracted real citations --
    # the failure mode this catches is an extractor that returns nothing and a scope
    # that resolved to nowhere.
    real_findings, stats = scan_all(ROOT)
    n_num = len([c for c in stats['cites'] if c['num']])
    expect('real-artifacts-nonvacuous',
           (stats['tracked'] > 0 and stats['read'] > 0 and len(stats['cites']) > 0
            and n_num > 0), True)
    print('  [real-artifacts-counts] tracked=%d read=%d citations=%d numeric=%d '
          'findings=%d' % (stats['tracked'], stats['read'], len(stats['cites']), n_num,
                           len(real_findings)))

    print('SELFTEST %s' % ('OK: every detector fires, the clean control stays clean, '
                           'the real scan is not vacuous'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        return selftest()
    findings, stats = scan_all(ROOT)
    report(findings, stats)
    return 0 if not findings else 1


if __name__ == '__main__':
    sys.exit(main())
