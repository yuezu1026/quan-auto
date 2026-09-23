#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for docs/迭代计划.md -- the agile delivery plan.

Why this file exists
--------------------
A plan is a product too. If it can say '已交付' with nothing behind it, it becomes a
document that lies, and every iteration status in it is worthless. So the plan gets the
same treatment as the other artifacts: one checkable statement, enforced by a gate, with
a trigger test.

The single checkable statement:

    An iteration may be marked '已交付' only if its 证据 column cites a path that
    actually exists on disk.

Nothing here judges whether the iteration was worth doing. It only refuses to let the
word '已交付' float free of evidence.

Checks
------
  C1  GATE  the §三 overview table yielded >= 1 iteration row. A drifted row regex would
            otherwise make every check below iterate over an empty list and print a
            clean 'verdict: PASS (0 issue(s))' -- the most convincing possible lie.
  C2        every 状态 cell is exactly one of 已交付 / 进行中 / 未开始 / 待决. Free text
            ('差不多了', '基本完成') is not a status: nothing can be checked against it.
  C3        a 已交付 row must have a non-empty 证据 column (not '—').
  C4        every path inside the 证据 column (backticked) must exist on disk, and there
            must be at least one. This is the check the whole file is built around.
  C5        iteration ids are unique and consecutive from I0 (no gap, no repeat).
  C6        every iteration in §三 has a matching DoD section in §四 (and vice versa).
  C7  GATE  the §四 DoD extraction yielded >= 1 section. Same vacuity guard as C1.
  C8        each DoD section carries a four-row table whose 件 column is exactly
            契约 / 产物 / 门禁 / 触发测试.

C8 checks the *shape* (row count and column values), never 'does the word appear'.
Verified against the spec §3.6 case nine: a detector that only greps for a keyword
still reports full coverage after the paragraph it guards is deleted.

What this gate does NOT do
--------------------------
It never evaluates the plan's content, the iteration count, or whether the slicing is
sound. It also cannot tell a genuinely delivered iteration from a well-formatted claim
with a real-but-irrelevant evidence path -- a human still has to read the cited file.
Passing this gate means 'the plan is internally well-formed and none of its completion
claims dangle', not 'the plan is good'.

Measured on 2026-09-23: 0 iterations are marked 已交付, so C3/C4 ran without exercising
their failing branch on the real document. That is why they each have a dedicated
--selftest sample; see the samples list in selftest().
"""

import os
import re
import sys

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = os.path.join(ROOT, 'docs', '迭代计划.md')

SECTION_OVERVIEW = '## 三、'
SECTION_DOD = '## 四、'

STATUSES = ('已交付', '进行中', '未开始', '待决')
ITEM_NAMES = ('契约', '产物', '门禁', '触发测试')
NO_EVIDENCE = ('', '—', '-', '--', '无')

ITER_ID_RE = re.compile(r'^I(\d+)$')
DOD_HEAD_RE = re.compile(r'^####\s+(I\d+)\b')
BACKTICK_RE = re.compile(r'`([^`]+)`')


# ---------------------------------------------------------------------------------
# The '↔' incident, again. This console is cp936 and a status word read out of a
# malformed sample can contain any character at all. Printing it raised
# UnicodeEncodeError *while the issues list was being written*, which lost the whole
# verdict and made a hard failure look like a crash. Unencodable characters now
# degrade to '?' instead of aborting the run.
# ---------------------------------------------------------------------------------
def harden_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def read_text(path):
    """UTF-8 (BOM tolerated) then CRLF -> LF.

    Normalising first is mandatory: every anchor below is a bare '\\n' start-of-line
    pattern, so a CRLF file matches nothing and this gate would print a clean 0.
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def slice_section(text, prefix):
    """Returns the body of the '## ...' section whose heading starts with `prefix`,
    or None when the heading is absent. Slicing on the next '## ' keeps a table in a
    later section from being read as part of this one."""
    lines = text.split('\n')
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(prefix):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith('## '):
            end = j
            break
    return '\n'.join(lines[start:end])


def split_row(line):
    """'| a | b |' -> ['a', 'b']. Returns None for lines that are not table rows."""
    if not line.startswith('|'):
        return None
    cells = line.split('|')
    if len(cells) < 3:
        return None
    return [c.strip() for c in cells[1:-1]]


def is_separator(cells):
    return all(set(c) <= set('-: ') and c for c in cells)


def parse_iterations(text):
    """Returns (rows, malformed) from the §三 overview table.

    rows:     [{'id','status','evidence','line'}] in file order
    malformed: lines that start with an I<digits> cell but do not have 5 cells
    """
    body = slice_section(text, SECTION_OVERVIEW)
    if body is None:
        return None, None
    rows, malformed = [], []
    for line in body.split('\n'):
        cells = split_row(line)
        if not cells or is_separator(cells):
            continue
        if not ITER_ID_RE.match(cells[0]):
            continue
        if len(cells) != 5:
            malformed.append((line, len(cells)))
            continue
        rows.append({'id': cells[0], 'status': cells[3],
                     'evidence': cells[4], 'line': line})
    return rows, malformed


def parse_dod(text):
    """Returns {iteration_id: section_text} from §四."""
    body = slice_section(text, SECTION_DOD)
    if body is None:
        return None
    lines = body.split('\n')
    starts = [(i, DOD_HEAD_RE.match(ln).group(1))
              for i, ln in enumerate(lines) if DOD_HEAD_RE.match(ln)]
    out = {}
    for n, (i, iid) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out[iid] = '\n'.join(lines[i:end])
    return out


def dod_items(section):
    """The 件 column values of the DoD table inside one section."""
    items = []
    for line in section.split('\n'):
        cells = split_row(line)
        if not cells or is_separator(cells):
            continue
        if cells[0] in ITEM_NAMES:
            items.append(cells[0])
    return items


def evidence_paths(evidence):
    return [p.strip() for p in BACKTICK_RE.findall(evidence)]


def run_checks(plan_text):
    issues = []
    stats = {}

    def fail(code, msg):
        issues.append((code, msg))

    rows, malformed = parse_iterations(plan_text)
    dod = parse_dod(plan_text)

    # --- C1: vacuity guard on the overview extraction -----------------------------
    if rows is None:
        fail('GATE', 'the §三 overview section heading was not found -- the iteration '
                     'table cannot be read, so every check below would iterate over an '
                     'empty list and report a clean 0; refusing to pass')
        rows = []
    elif not rows:
        fail('GATE', 'the §三 overview table yielded 0 iteration rows -- the row shape '
                     '(5 cells, first cell = I<digits>) has drifted, or the table was '
                     'replaced by prose; C2-C6 would all pass vacuously')
    if malformed:
        for line, n in malformed:
            fail('C2', 'iteration row has %d cells, expected 5 -- a row that cannot be '
                       'split cannot have its status or evidence checked: %s'
                       % (n, line.strip()[:90]))

    # --- C2: status must be from the closed vocabulary ----------------------------
    for r in rows:
        if r['status'] not in STATUSES:
            fail('C2', '%s has status %r, which is not one of %s -- a status nothing '
                       'can be checked against is not a status'
                       % (r['id'], r['status'], '/'.join(STATUSES)))

    # --- C3 / C4: a 已交付 row must cite evidence that exists ---------------------
    delivered = 0
    for r in rows:
        if r['status'] != '已交付':
            continue
        delivered += 1
        ev = r['evidence']
        if ev in NO_EVIDENCE:
            fail('C3', '%s is marked 已交付 but its 证据 column is empty (%r)'
                       % (r['id'], ev))
            continue
        paths = evidence_paths(ev)
        if not paths:
            fail('C4', '%s is marked 已交付 but its 证据 column contains no backticked '
                       'path, so the claim cannot be verified: %r' % (r['id'], ev[:80]))
            continue
        for p in paths:
            if not os.path.exists(os.path.join(ROOT, p)):
                fail('C4', '%s is marked 已交付 and cites %s, but that path does not '
                           'exist -- a completion claim with a dangling evidence path '
                           'is a ghost ✅' % (r['id'], p))

    # --- C5: numbering unique and consecutive from I0 ----------------------------
    nums = [int(ITER_ID_RE.match(r['id']).group(1)) for r in rows
            if ITER_ID_RE.match(r['id'])]
    if nums:
        seen = {}
        for n in nums:
            seen[n] = seen.get(n, 0) + 1
        dupes = sorted(n for n, c in seen.items() if c > 1)
        if dupes:
            fail('C5', 'iteration id(s) appear more than once: %s'
                       % ['I%d' % n for n in dupes])
        if nums[0] != 0:
            fail('C5', 'the first iteration is I%d, expected I0 -- a plan that starts '
                       'mid-sequence makes the burn-down table unreadable' % nums[0])
        gaps = [n for n in range(min(nums), max(nums) + 1) if n not in seen]
        if gaps:
            fail('C5', 'iteration numbering has gap(s): %s'
                       % ['I%d' % n for n in gaps])
        if nums != sorted(nums):
            fail('C5', 'iteration rows are out of order: %s'
                       % ['I%d' % n for n in nums])

    # --- C6: every iteration has a DoD section, and vice versa --------------------
    if dod is None:
        fail('GATE', 'the §四 DoD section heading was not found -- C6/C8 cannot run')
        dod = {}
    elif not dod:
        fail('GATE', 'the §四 DoD section yielded 0 subsections -- the heading shape '
                     "'#### I<digits> · <title>' has drifted; C6/C8 would pass "
                     'vacuously')
    iter_ids = set(r['id'] for r in rows)
    dod_ids = set(dod)
    for missing in sorted(iter_ids - dod_ids):
        fail('C6', '%s is in the §三 overview but has no DoD section in §四 -- an '
                   'iteration with no completion definition cannot be scored' % missing)
    for extra in sorted(dod_ids - iter_ids):
        fail('C6', '%s has a DoD section in §四 but is not in the §三 overview -- '
                   'an orphan definition' % extra)

    # --- C8: the DoD table must have the exact four 件 rows -----------------------
    for iid in sorted(dod):
        items = dod_items(dod[iid])
        if sorted(items) != sorted(ITEM_NAMES):
            missing = [n for n in ITEM_NAMES if n not in items]
            extra = [n for n in items if n not in ITEM_NAMES]
            fail('C8', '%s DoD 四件套 is incomplete: found %s, missing %s, unexpected '
                       '%s -- a DoD that does not carry all four cannot make a green '
                       'claim' % (iid, items or '[]', missing or '[]', extra or '[]'))

    stats['iterations'] = len(rows)
    stats['delivered'] = delivered
    stats['dod_sections'] = len(dod)
    return issues, stats


# ---------------------------------------------------------------------------------
# Selftest. One sample class per detector, plus the two vacuity guards, plus a clean
# control. The samples are built by assert-anchored substitution: if an anchor is
# absent the sample COUNTS AS FAILED rather than silently testing the pristine text,
# which is how a mutation-based trigger test turns into a no-op.
# ---------------------------------------------------------------------------------
SYNTH = (
    '## 三、迭代总览\n'
    '\n'
    '| 迭代 | 目标 | 产物 | 状态 | 证据 |\n'
    '| --- | --- | --- | --- | --- |\n'
    '| I0 | 做骨架 | 产物甲 | 未开始 | — |\n'
    '| I1 | 做竖切 | 产物乙 | 已交付 | `CONTEXT.md` |\n'
    '\n'
    '## 四、每个迭代的完成定义（DoD）\n'
    '\n'
    '#### I0 · 骨架\n'
    '\n'
    '| 件 | 具体是什么 | 判定方式 | 证据 |\n'
    '| --- | --- | --- | --- |\n'
    '| 契约 | a | b | — |\n'
    '| 产物 | a | b | — |\n'
    '| 门禁 | a | b | — |\n'
    '| 触发测试 | a | b | — |\n'
    '\n'
    '#### I1 · 竖切\n'
    '\n'
    '| 件 | 具体是什么 | 判定方式 | 证据 |\n'
    '| --- | --- | --- | --- |\n'
    '| 契约 | a | b | — |\n'
    '| 产物 | a | b | — |\n'
    '| 门禁 | a | b | — |\n'
    '| 触发测试 | a | b | — |\n'
)


def mutate(text, old, new, tag):
    if old not in text:
        print('    sample %s: ANCHOR NOT FOUND (%r)' % (tag, old[:60]))
        return None
    out = text.replace(old, new, 1)
    if out == text:
        print('    sample %s: replacement was a no-op' % tag)
        return None
    return out


def selftest():
    ok = True

    def scenario(tag, plan, code=None, want_clean=False):
        nonlocal ok
        issues, stats = run_checks(plan)
        codes = set(k for k, _ in issues)
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s iters=%d dod=%d %s'
              % (tag, len(issues), sorted(codes), stats['iterations'],
                 stats['dod_sections'], 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # CONTROL: the real plan. Reported, never asserted clean -- the gate must be able
    # to fail on the real artifact, that is the point of running it.
    if os.path.exists(PLAN):
        issues, stats = run_checks(read_text(PLAN))
        print('  [control-real-plan] issues=%d codes=%s iterations=%d delivered=%d '
              'dod_sections=%d'
              % (len(issues), sorted(set(k for k, _ in issues)), stats['iterations'],
                 stats['delivered'], stats['dod_sections']))
    else:
        print('  [control-real-plan] SKIPPED (file missing) -- the vacuity guards below '
              'still cover the empty-input case')

    # POSITIVE FIRST: a sample with a real 已交付 + existing path must be clean,
    # otherwise C3/C4 would be detectors that can only ever fire.
    scenario('POSITIVE-clean', SYNTH, want_clean=True)

    # C2: free-text status.
    s = mutate(SYNTH, '| I0 | 做骨架 | 产物甲 | 未开始 |', '| I0 | 做骨架 | 产物甲 | 差不多了 |',
               'C2')
    if s:
        scenario('C2-free-text-status', s, 'C2')

    # C3: 已交付 with no evidence.
    s = mutate(SYNTH, '| I1 | 做竖切 | 产物乙 | 已交付 | `CONTEXT.md` |',
               '| I1 | 做竖切 | 产物乙 | 已交付 | — |', 'C3')
    if s:
        scenario('C3-delivered-no-evidence', s, 'C3')

    # C4: 已交付 citing a path that does not exist -- THE detector this gate exists for.
    s = mutate(SYNTH, '`CONTEXT.md`', '`docs/这份证据根本不存在.md`', 'C4')
    if s:
        scenario('C4-ghost-path', s, 'C4')

    # C4 again, the other branch: evidence present but not backticked, so the claim is
    # unverifiable. Separate sample because it exercises a different code path.
    s = mutate(SYNTH, '| I1 | 做竖切 | 产物乙 | 已交付 | `CONTEXT.md` |',
               '| I1 | 做竖切 | 产物乙 | 已交付 | 已经做完了 |', 'C4b')
    if s:
        scenario('C4-unverifiable-evidence', s, 'C4')

    # C5: a gap in the numbering.
    s = mutate(SYNTH, '| I1 | 做竖切', '| I2 | 做竖切', 'C5')
    if s:
        scenario('C5-numbering-gap', s, 'C5')

    # C6: an iteration in the overview with no DoD section.
    s = mutate(SYNTH, '#### I1 · 竖切', '#### I1x · 竖切', 'C6')
    if s:
        scenario('C6-missing-dod', s, 'C6')

    # C8: a DoD table missing one of the four 件.
    s = mutate(SYNTH, '| 门禁 | a | b | — |\n| 触发测试 | a | b | — |\n\n#### I1',
               '| 触发测试 | a | b | — |\n\n#### I1', 'C8')
    if s:
        scenario('C8-incomplete-dod', s, 'C8')

    # GATE: the §三 heading present but the table emptied. C2-C6 must not report a
    # clean 0 over an empty list.
    s = mutate(SYNTH, '| I0 | 做骨架 | 产物甲 | 未开始 | — |\n'
                      '| I1 | 做竖切 | 产物乙 | 已交付 | `CONTEXT.md` |\n', '', 'GATE-C1')
    if s:
        scenario('GATE-empty-overview', s, 'GATE')

    # GATE: the §四 heading present but no DoD subsections at all.
    #
    # BOTH headings have to be broken. The first version of this sample only renamed
    # I0's, which left one DoD section standing: the extraction returned a non-empty
    # dict, the C7 guard correctly stayed silent, and the sample reported C6 instead.
    # A trigger test whose mutation does not actually reach the branch under test is
    # indistinguishable from a missing detector -- so the mutation is applied to every
    # heading and the sample asserts the guard that is supposed to fire.
    s = SYNTH
    for old, new, tag in (('#### I0 · 骨架', '#### 骨架', 'GATE-C7a'),
                          ('#### I1 · 竖切', '#### 竖切', 'GATE-C7b')):
        if s is not None:
            s = mutate(s, old, new, tag)
    if s:
        scenario('GATE-no-dod-sections', s, 'GATE')

    # GATE: a file with no plan structure whatsoever (the .gitignore case). Both
    # sections are missing, so both guards must fire at once.
    scenario('GATE-no-such-document', '# just a comment\n\nno tables here\n', 'GATE')

    print('SELFTEST %s' % ('OK: every detector fires, the clean sample stays clean, '
                           'and both vacuity guards bite'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    harden_stdout()
    if '--selftest' in sys.argv:
        return selftest()

    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    path = args[0] if args else PLAN
    if not os.path.exists(path):
        print('GATE FAIL: missing input %s' % path)
        return 1

    plan = read_text(path)
    print('CRLF-normalised: plan=%d bytes (%s)'
          % (len(plan.encode('utf-8')), os.path.basename(path)))

    issues, stats = run_checks(plan)
    print('extracted: iterations=%d delivered=%d dod_sections=%d'
          % (stats['iterations'], stats['delivered'], stats['dod_sections']))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not issues else 'FAIL', len(issues)))
    if stats['delivered'] == 0 and stats['iterations']:
        print('NOTE: no iteration is marked 已交付 yet, so C3/C4 found nothing to '
              'check on this run -- that is an empty result, NOT a verification. '
              'Their failing branch is exercised by --selftest samples '
              'C3-delivered-no-evidence / C4-ghost-path / C4-unverifiable-evidence.')
    print('NOTE: this gate checks the plan is internally well-formed and that no '
          'completion claim dangles. It cannot judge whether an iteration was actually '
          'delivered -- someone still has to open the cited evidence file.')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
