# -*- coding: utf-8 -*-
"""Static gate for the data-center contract vs its executable DDL.

What it proves
--------------
The data-center contract's §3.6.1 lists the invariants that are supposed to be pushed
down to the database. The DDL is where they actually live. The dangerous failure is
one-sided: a constraint documented but never written (a 纸面防线), or one written but
never documented (an undocumented invariant nobody tests).

What it explicitly does NOT do
------------------------------
It never executes the DDL and never inspects a CHECK expression. A constraint whose
expression is wrong, inverted, or omits an enum member passes every check here. Only
running the DDL on a real PostgreSQL and feeding it values that must be rejected can
prove the constraints work. This gate only keeps the files in step, and (C7) keeps the
file that CAN prove it from quietly losing coverage.

Checks (each independent; no check returns early and shadows a later one)
  C1  GATE  both extractions non-empty (an unmatched regex would report a clean 0)
  C2  every ck_ documented in contract §3.6.1 exists in the DDL
  C3  every ck_ in the DDL is documented in contract §3.6.1
  C4  every ck_ is declared inside a CREATE TABLE body, not in a comment or a stray line
  C5  no MySQL-isms in the DDL
  C6  the DDL still carries its verification-status stamp, and that stamp names the
      image the DDL was actually run on (2026-09-23: postgres:17). Was "must carry a
      NOT YET EXECUTED banner" -- once the DDL really did run, keeping that literal
      would have made the gate defend a falsehood. The stamp is now the other way
      round: a DDL that HAS run must say WHERE, because "passed on 17.11" is not
      "PostgreSQL 14+ works". NOT YET DONE: cross-check the cited image against the
      image recorded in tools/sql-smoke-report.txt (today the stamp is a hand-written
      claim and only the second half of that pair is machine-checked).
  C7  GATE  db/data_center.smoke.sql was supplied and is non-empty, and every ck_ the
      DDL declares is asserted BY NAME against a rejected sample there

Exit codes: 0 = PASS, 1 = FAIL.
"""
import os
import re
import sys

# ----------------------------------------------------------------------------- 
# Extractors. Every one of them is followed by a hard vacuity guard in run_checks().
# -----------------------------------------------------------------------------
CONTRACT_TABLE_RE = re.compile(r'^#{2,5}\s*3\.6\.1\b.*?$(.*?)(?=^#{2,5}\s|\Z)',
                               re.S | re.M)
CONTRACT_ROW_RE = re.compile(r'^\|\s*`(ck_dc_[a-z0-9_]+)`\s*\|', re.M)
DDL_CONSTRAINT_RE = re.compile(r'^\s*CONSTRAINT\s+(ck_dc_[a-z0-9_]+)', re.M)
CREATE_TABLE_BODY_RE = re.compile(
    r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)^\s*\)\s*;', re.S | re.M)
TABLE_NAME_RE = re.compile(r'^\s*(\w+)\s+\w+', re.M)

MYSQLISM_RE = re.compile(
    r'\bAUTO_INCREMENT\b|\bENGINE\s*=|\bTINYINT\b|\bMEDIUMINT\b|\bDATETIME\s*\(|\bUNSIGNED\b'
    r'|\bIFNULL\s*\(|\bint\(\d+\)|`[a-z_]+`|\bON\s+UPDATE\s+CURRENT_TIMESTAMP\b'
    r'|\bLONGTEXT\b|\bDOUBLE\s*\(\s*\d', re.I)

# C6: the DDL's verification-status stamp. It must name the image the DDL was run on --
# an executed DDL presented as "verified" without saying WHERE was run recreates exactly
# the falsehood the old NOT-YET-EXECUTED banner existed to prevent (one green run on
# postgres:17 does not make the DDL's own "PostgreSQL 14+" claim true).
EXEC_BANNER_RE = re.compile(r'VERIFIED\s+ONCE\s+on\s+(postgres:\d[\w.\-]*)')

# C7: the shape a constraint name must take in the smoke test to count as a trigger
# test -- an EQUALITY COMPARISON against that literal, e.g.
#     IF v_cname = 'ck_dc_factor_positive' THEN
# Deliberately NOT "the name appears somewhere". Two weaker forms would otherwise slip
# through, and both of them are real: a name mentioned in a comment, and a name listed
# as a bare literal in the smoke test's own pg_constraint inventory (which must name all
# of them anyway). Under either weak form the entire rejection half of the smoke test
# could be deleted and C7 would still report full coverage.
SMOKE_ASSERT_TMPL = r"=\s*'%s'"
# C7's second guard: the smoke test must actually read WHICH constraint fired, else its
# name comparisons are decorative and one catch-all clause could satisfy them all.
DIAGNOSTICS_RE = re.compile(r'GET\s+STACKED\s+DIAGNOSTICS\s+\w+\s*=\s*CONSTRAINT_NAME', re.I)


def smoke_assert_re(name):
    return re.compile(SMOKE_ASSERT_TMPL % re.escape(name))


def strip_sql_line_comments(text):
    """Removes `-- ...` line comments, leaving single-quoted literals intact.

    A plain regex is wrong here, and the smoke file proves it: it contains the literal
    '---- smoke summary: ... ----'. Tracking single-quote state (with '' as the escape)
    is what makes C7's central claim true. If this stripping were wrong, a constraint
    assertion could be commented out and C7 would still count it as covered -- the check
    would be reporting on text it had already misread, which is the failure mode this
    whole gate exists to refuse.
    """
    out = []
    in_str = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '-' and i + 1 < n and text[i + 1] == '-':
            j = text.find('\n', i)
            if j == -1:
                break
            i = j
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def read_text(path):
    """UTF-8 (BOM tolerated) then CRLF -> LF.

    Normalising first is mandatory: the regexes below use bare '\\n' anchors, so a CRLF
    file would match nothing and the whole gate would pass with '0 issues'.
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def contract_constraints(text):
    m = CONTRACT_TABLE_RE.search(text)
    if not m:
        return None
    return set(CONTRACT_ROW_RE.findall(m.group(1)))


def ddl_constraints(text):
    return set(DDL_CONSTRAINT_RE.findall(text))


def ddl_bodies(text):
    """Returns {table_name: body_text}. Slicing to the first ')^;' terminator keeps a
    constraint from a later table out of an earlier table's body, so a substring test
    cannot substitute for an existence test."""
    return {t: b for t, b in CREATE_TABLE_BODY_RE.findall(text)}


def run_checks(ddl_text, contract_text, smoke_text):
    """smoke_text is REQUIRED, never defaulted.

    A defaulted argument would silently turn C7 into a check that runs only in main()
    and never in --selftest -- i.e. exactly the kind of detector that can never be seen
    to be broken, which is the failure this whole file is written to avoid.
    """
    issues = []
    stats = {}

    def fail(code, msg):
        issues.append((code, msg))

    documented = contract_constraints(contract_text)
    declared = ddl_constraints(ddl_text)
    bodies = ddl_bodies(ddl_text)
    in_body = set()
    for tname, body in bodies.items():
        for name in DDL_CONSTRAINT_RE.findall(body):
            in_body.add((tname, name))
    body_names = set(n for _, n in in_body)

    stats['documented'] = len(documented or ())
    stats['declared'] = len(declared)
    stats['tables'] = len(bodies)
    stats['in_body'] = len(body_names)

    # --- C1: vacuity guards ------------------------------------------------------
    if documented is None:
        fail('GATE', 'contract §3.6.1 table not found -- every comparison below would '
                     'pass vacuously, refusing to report a clean result')
    elif not documented:
        fail('GATE', 'contract §3.6.1 exists but yielded 0 constraint names -- the row '
                     'regex has drifted (check the `| `ck_...` |` shape)')

    if not declared:
        fail('GATE', 'no CONSTRAINT declaration extracted from the DDL -- the C2/C3/C4 '
                     'comparisons would all pass vacuously')
    if not bodies:
        fail('GATE', 'no CREATE TABLE body extracted -- C4 cannot run')
    if not body_names:
        fail('GATE', 'no constraint found inside any table body -- C4 is not wired up')

    # --- C2 / C3: two-way inventory ---------------------------------------------
    if documented:
        missing_in_ddl = sorted(documented - declared)
        if missing_in_ddl:
            fail('C2', 'documented in contract §3.6.1 but absent from db/data_center.sql: '
                       '%s -- a documented invariant with no implementation is a 纸面防线'
                       % missing_in_ddl)
        undocumented = sorted(declared - documented)
        if undocumented:
            fail('C3', 'declared in db/data_center.sql but not documented in §3.6.1: %s '
                       '-- an undocumented invariant is one nobody writes a trigger test '
                       'for' % undocumented)

    # --- C4: constraints must live inside a table body ---------------------------
    if documented:
        if not documented <= body_names:
            fail('C4', 'constraint(s) present in the file but not inside any CREATE TABLE '
                       'body (commented out or misplaced): %s'
                       % sorted(documented - body_names))

    # --- C5: MySQL leftovers -----------------------------------------------------
    mysql = sorted(set(m.group(0) for m in MYSQLISM_RE.finditer(ddl_text)))
    if mysql:
        fail('C5', 'MySQL-ism(s) in the PostgreSQL DDL: %s' % mysql)
    stats['mysqlisms'] = len(mysql)

    # --- C6: the verification-status stamp must survive and must name the image -------
    stamp = EXEC_BANNER_RE.search(ddl_text)
    if not stamp:
        fail('C6', 'the DDL no longer carries a "VERIFIED ONCE on <image>" stamp -- a '
                   'verification claim that does not say which image it was run on is '
                   'not checkable, and one green run on one version does not make the '
                   "DDL's own supported-version claim true")
    else:
        stats['verified_on'] = stamp.group(1)

    # --- C7: every declared constraint has a trigger test that names it -----------
    # C2-C6 prove the constraint INVENTORY is consistent. None of them can show that a
    # constraint rejects anything: a wrong expression passes every regex here. The
    # smoke test is the only artifact that can, so the one thing this gate CAN do is
    # refuse to let that smoke test silently lose a constraint.
    #
    # The name search runs over comment-stripped text on purpose -- see
    # strip_sql_line_comments(). `declared` being empty is already a GATE failure above,
    # so this check cannot pass vacuously on an empty inventory.
    if smoke_text is None:
        fail('GATE', 'the DDL smoke test was not supplied to run_checks -- C7 would '
                     'pass vacuously')
    elif not smoke_text.strip():
        fail('GATE', 'db/data_center.smoke.sql is empty -- there is no trigger test for '
                     'the database-level guards')
    else:
        code_only = strip_sql_line_comments(smoke_text)
        untested = [name for name in sorted(declared)
                    if not smoke_assert_re(name).search(code_only)]
        if untested:
            fail('C7', 'db/data_center.smoke.sql never asserts which constraint refused '
                       'the sample for: %s -- every database-enforced invariant needs a '
                       'sample that must fail, plus a check of the name that refused it'
                       % untested)
        if not DIAGNOSTICS_RE.search(code_only):
            fail('C7', 'the smoke test never reads GET STACKED DIAGNOSTICS ... '
                       'CONSTRAINT_NAME, so its constraint-name assertions cannot tell '
                       'WHICH guard fired -- a single catch-all clause could satisfy '
                       'them all while the real guard rots')
        stats['smoke_asserted'] = len(declared) - len(untested)

    return issues, stats


def _mutate(text, old, new, tag):
    """Assert-anchored replacement. Returns None when the anchor is absent, so the
    caller must treat 'sample could not be built' as a failure instead of silently
    testing the unmodified file."""
    if old not in text:
        print('    sample %s: ANCHOR NOT FOUND (%r)' % (tag, old[:60]))
        return None
    return text.replace(old, new, 1)


def selftest():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cpath = os.path.join(root, 'docs', '智能量化交易平台-数据中心接口契约文档.md')
    dpath = os.path.join(root, 'db', 'data_center.sql')
    spath = os.path.join(root, 'db', 'data_center.smoke.sql')
    contract, ddl, smoke = read_text(cpath), read_text(dpath), read_text(spath)

    ok = True

    def scenario(tag, d, c, code, want_clean=False, s=None):
        nonlocal ok
        issues, _ = run_checks(d, c, smoke if s is None else s)
        codes = set(k for k, _ in issues)
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s %s'
              % (tag, len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # CONTROL: the real artifacts. Reported but not asserted clean -- the gate is
    # expected to be able to fail on them, which is the whole point of running it.
    issues, stats = run_checks(ddl, contract, smoke)
    print('  [control-real-artifacts] issues=%d codes=%s tables=%d documented=%d '
          'declared=%d smoke_asserted=%d'
          % (len(issues), sorted(set(k for k, _ in issues)), stats['tables'],
             stats['documented'], stats['declared'], stats.get('smoke_asserted', -1)))

    # C2: a constraint in the contract that the DDL does not declare.
    bad = _mutate(contract, '| `ck_dc_version_format` |', '| `ck_dc_ghost_format` |', 'NEG1')
    if bad is None:
        ok = False
    else:
        scenario('NEG1-contract-only', ddl, bad, 'C2')

    # C3: a constraint in the DDL that the contract does not document.
    bad = _mutate(contract, '| `ck_dc_factor_positive` |', '| `ck_dc_not_it` |', 'NEG2')
    if bad is None:
        ok = False
    else:
        scenario('NEG2-ddl-only', ddl, bad, 'C3')

    # C4: keep the constraint in the file, but move it out of the CREATE TABLE body.
    anchor = ('    CONSTRAINT ck_dc_factor_positive CHECK (adjust_factor > 0)\n')
    bad = _mutate(ddl, anchor, '', 'NEG3')
    if bad is None:
        ok = False
    else:
        bad = bad + '\n-- CONSTRAINT ck_dc_factor_positive CHECK (adjust_factor > 0)\n'
        scenario('NEG3-outside-body', bad, contract, 'C4')

    # C5: MySQL leftovers.
    bad = _mutate(ddl, 'run_id          bigint          GENERATED ALWAYS AS IDENTITY',
                  'run_id          bigint          AUTO_INCREMENT', 'NEG4')
    if bad is None:
        ok = False
    else:
        scenario('NEG4-mysqlism', bad, contract, 'C5')

    # C6a: the verification stamp silently dropped. Both halves matter now, so each half
    # gets its own sample: this one loses the "VERIFIED ONCE on <image>" shape entirely.
    bad = _mutate(ddl, 'VERIFIED ONCE on postgres:17', 'STATUS: executed', 'NEG5a')
    if bad is None:
        ok = False
    else:
        scenario('NEG5a-stamp-dropped', bad, contract, 'C6')

    # C6b: the stamp survives but no longer names the image. This is the sample that
    # distinguishes the new rule from the old one -- a banner-only check would call this
    # input clean, and the claim would be unverifiable again.
    bad = _mutate(ddl, 'VERIFIED ONCE on postgres:17', 'VERIFIED ONCE by hand', 'NEG5b')
    if bad is None:
        ok = False
    else:
        scenario('NEG5b-stamp-without-image', bad, contract, 'C6')

    # C7a: a constraint that the DDL declares but the smoke test never asserts. The DDL
    # is untouched and the contract still lists it, so C2-C6 all stay clean -- without
    # C7 this input looks perfectly healthy.
    bad = _mutate(smoke, "IF v_cname = 'ck_dc_factor_positive' THEN",
                  "IF v_cname = 'ck_dc_some_other_name' THEN", 'NEG6')
    if bad is None:
        ok = False
    else:
        scenario('NEG6-smoke-name-dropped', ddl, contract, 'C7', s=bad)

    # C7b: the name is still on the line, but COMMENTED OUT. This is the sample that
    # proves strip_sql_line_comments() actually strips: if it did not, C7 would count
    # the commented assertion as coverage -- the exact way a gate reports on text it
    # has misread. Note that the anchor itself is a code line, so it must be mutated
    # into a comment rather than removed.
    bad = _mutate(smoke, "IF v_cname = 'ck_dc_factor_positive' THEN",
                  "-- IF v_cname = 'ck_dc_factor_positive' THEN", 'NEG7')
    if bad is None:
        ok = False
    else:
        scenario('NEG7-smoke-name-commented', ddl, contract, 'C7', s=bad)

    # C7c: names asserted, but nothing ever reads which constraint refused the row.
    # Worth a separate sample because a name-equality check can be satisfied by a
    # decorative comparison; removing the diagnostic is what makes the comparison
    # meaningless, and only this sample can show C7 notices.
    bad = smoke.replace('GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME', 'v_cname := NULL')
    if bad == smoke:
        print('    sample NEG8: ANCHOR NOT FOUND (the diagnostics read)')
        ok = False
    else:
        scenario('NEG8-smoke-no-diagnostics', ddl, contract, 'C7', s=bad)

    # C7d: the strongest sample -- DELETE the whole rejection half of the smoke file
    # (section B) while leaving section A in place. A still names all 16 constraints as
    # bare quoted literals, so a weaker C7 that merely looked for "the name appears on a
    # code line" would keep reporting full coverage here. This is the sample that pins
    # down C7's actual requirement, which is an equality assertion after a rejection.
    b_start = '-- B. one sample per CHECK constraint'
    b_end = '-- C. boundary samples'
    if b_start not in smoke or b_end not in smoke:
        print('    sample NEG9: ANCHOR NOT FOUND (section B banner)')
        ok = False
    else:
        i, j = smoke.index(b_start), smoke.index(b_end)
        scenario('NEG9-smoke-rejections-deleted', ddl, contract, 'C7',
                 s=smoke[:i] + smoke[j:])

    # GATE: an input with no constraints at all must FAIL, not report a clean 0.
    scenario('GATE-empty-ddl', '# no constraints here\n', contract, 'GATE')
    scenario('GATE-empty-contract', ddl, '# no table here\n', 'GATE')
    # GATE: an empty smoke test must FAIL rather than let C7 report full coverage over
    # nothing. This is the vacuity guard for the newest check, so it gets its own sample.
    scenario('GATE-empty-smoke', ddl, contract, 'GATE', s='')

    # POSITIVE: a self-consistent synthetic triple must be clean, otherwise the gate is
    # flagging everything and its "DIRTY" verdict carries no information. The synthetic
    # smoke must carry the real assertion shapes (equality + diagnostics), otherwise this
    # control would be asserting that a clean verdict is reachable while C7 is broken.
    synth_ddl = ('CREATE TABLE IF NOT EXISTS t (\n'
                 '    a int NOT NULL,\n'
                 '    CONSTRAINT ck_dc_demo CHECK (a > 0)\n'
                 ');\n'
                 '-- VERIFIED ONCE on postgres:17\n')
    synth_contract = ('#### 3.6.1 数据库级不变量（最后防线）\n\n'
                      '| 约束名 | 表 | 强制内容 | 依据 |\n'
                      '| --- | --- | --- | --- |\n'
                      '| `ck_dc_demo` | t | a > 0 | D1 |\n')
    synth_smoke = ('BEGIN;\n'
                   'DO $s$ DECLARE v_cname text; BEGIN\n'
                   '  BEGIN\n'
                   '    INSERT INTO t (a) VALUES (-1);\n'
                   "    RAISE NOTICE 'FAIL: accepted';\n"
                   '  EXCEPTION WHEN check_violation THEN\n'
                   '    GET STACKED DIAGNOSTICS v_cname = CONSTRAINT_NAME;\n'
                   "    IF v_cname = 'ck_dc_demo' THEN RAISE NOTICE 'ok'; END IF;\n"
                   '  END;\n'
                   'END $s$;\n'
                   'ROLLBACK;\n')
    scenario('POSITIVE-consistent', synth_ddl, synth_contract, None, want_clean=True,
             s=synth_smoke)

    print('SELFTEST %s' % ('OK: all detectors fire, consistent sample stays clean'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if '--selftest' in sys.argv:
        return selftest()

    cpath = os.path.join(root, 'docs', '智能量化交易平台-数据中心接口契约文档.md')
    dpath = os.path.join(root, 'db', 'data_center.sql')
    spath = os.path.join(root, 'db', 'data_center.smoke.sql')
    for p in (cpath, dpath, spath):
        if not os.path.exists(p):
            print('GATE FAIL: missing input %s' % p)
            return 1

    contract, ddl, smoke = read_text(cpath), read_text(dpath), read_text(spath)
    print('CRLF-normalised: contract=%d bytes, ddl=%d bytes, smoke=%d bytes'
          % (len(contract.encode('utf-8')), len(ddl.encode('utf-8')),
             len(smoke.encode('utf-8'))))

    issues, stats = run_checks(ddl, contract, smoke)
    print('extracted: tables=%d documented=%d declared=%d in_body=%d mysqlisms=%d '
          'smoke_asserted=%d'
          % (stats['tables'], stats['documented'], stats['declared'],
             stats['in_body'], stats['mysqlisms'], stats.get('smoke_asserted', -1)))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: this gate never executes the DDL and never inspects a CHECK '
          'expression. Passing it does NOT mean the constraints reject bad values -- '
          'that is what db/data_center.smoke.sql has to print SMOKE PASS for. That run '
          'has happened (see tools/sql-smoke-report.txt, and tools/falsify-report.txt '
          'for the per-constraint falsification); both are SNAPSHOTS and are void the '
          'moment any db/*.sql or *.smoke.sql file changes.')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
