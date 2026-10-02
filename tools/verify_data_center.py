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
  C6  the DDL's "-- PG-VERIFIED-ON: <images>" stamp and the images recorded in
      tools/sql-smoke-report*.txt agree IN BOTH DIRECTIONS: a cited image with no
      report behind it is a claim with no evidence, and a report the stamp omits is
      evidence the stamp hides. This check has a history worth keeping: it began as
      "must carry a NOT YET EXECUTED banner" (keeping that literal after the DDL really
      ran would have made the gate defend a falsehood), then became "must name the one
      image it ran on" while exactly one image had been run. As of 2026-09-24 the runs
      are a ladder -- postgres:14/15/16/17, each with its own snapshot file -- so the
      union of those reports IS the claim, both halves are machine-checked, and the
      "NOT YET DONE: cross-check the cited image" note that used to live here is done.
  C7  GATE  db/data_center.smoke.sql was supplied and is non-empty, and every ck_ the
      DDL declares is asserted BY NAME against a rejected sample there
  C8  GATE  the quantisation constants in quanauto/pgstore.py and the `numeric(p,s)`
      scales of the columns they write agree in BOTH directions; every constant the
      writer defines is registered, every numeric column in the DDL is either
      quantised or named as having no writer yet, and neither list has gone stale

Exit codes: 0 = PASS, 1 = FAIL.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

# C6: the verification-status stamp, tied to the evidence files rather than trusted.
#
# The marker is a single dedicated line, so "what does this DDL claim" has exactly one
# answer a reviewer can look up and a stray `postgres:NN` in prose cannot inflate it.
#
# The comparison runs in BOTH directions on purpose. A one-directional judge ("every
# cited image has a report") cannot see the drift where the artifact regenerates: once a
# fifth major is run, the DDL would keep claiming four, every check would stay green, and
# the next reader would re-derive a weaker claim than the evidence supports. That family
# -- one-directional judge, report cleaner than reality -- is the one this project keeps
# getting bitten by, so the evidence side is a hard equality.
STAMP_RE = re.compile(r'^[ \t]*--[ \t]*PG-VERIFIED-ON:[ \t]*(.*)$', re.M)
PG_IMAGE_RE = re.compile(r'postgres:\d[\w.\-]*')
REPORT_GLOB = os.path.join(ROOT, 'tools', 'sql-smoke-report*.txt')
REPORT_IMAGE_RE = re.compile(r'^[ \t]*image[ \t]*:[ \t]*(postgres:\d[\w.\-]*)[ \t]*$', re.M)

# C6's second half: the FALSIFICATION ladder, tied to the same stamp.
#
# The stamp's claim is not "this DDL was loaded on four images" -- the claim a reader
# actually leans on is "these constraints were each shown to bite, on four images". The
# smoke reports can only show the constraints are *accepted* by the server; only the
# falsify reports show that relaxing one makes exactly one sample go red. Appendix B19
# recorded that ladder as covering postgres:17 alone, and said so as an explicit
# boundary. Add a fifth image to the smoke ladder while the falsify ladder stays at one
# and a smoke-only judge would stay green, with the stamp still claiming four -- the
# reader would derive a stronger claim than the evidence supports. That is the
# one-directional-judge family again, so the falsify set is a hard equality too.
#
# The ladder is a SHARED artifact: tools/falsify_smoke.py relaxes one constraint at a
# time in BOTH db/*.sql files and reruns both smoke files, so one set of reports covers
# the risk DDL exactly as much as the data-centre one. It is therefore cross-checked
# here, once, rather than also inside tools/verify_risk_config.py -- a second copy would
# be a copy that can drift, and this repository has already paid for that lesson.
FALSIFY_GLOB = os.path.join(ROOT, 'tools', 'falsify-report*.txt')
FALSIFY_TOOL = os.path.join(ROOT, 'tools', 'falsify_smoke.py')
FALSIFY_VERDICT_RE = re.compile(r'^verdict:[ \t]*(\S+)[ \t]*--', re.M)
FALSIFY_COVER_RE = re.compile(
    r'覆盖:[ \t]*(\d+)[ \t]*个案例[ \t]*/[ \t]*(\d+)[ \t]*条命名\s*CHECK')
# The tool's own case list, counted rather than written down here: a literal would go
# wrong the first time a case is added, and comparing against it is exactly what makes a
# stale snapshot visible.
FALSIFY_CASE_RE = re.compile(r"^[ \t]*\(\s*'[^']+',\s*(?:RISK_DDL|DC_DDL)\s*,", re.M)
# --selftest's injection point. None means "read the glob yourself". A module global
# rather than a run_checks() parameter on purpose: the samples then run through the very
# same call the real path uses, instead of through a branch only the samples take.
FALSIFY_DOCS = None

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

# C8: the writer's quantisation constants against the DDL's column scales.
#
# Every other check in this file compares two documents that BOTH claim to describe the
# database. This one reads the writer, because `numeric(p,s)` in the DDL and the quantum
# the code rounds to are one decision written twice, in two files that never meet -- and
# when they disagree nothing throws. A value carrying more decimals than the column keeps
# loses its tail on the way in, so the value read back is never equal to the value sent,
# and a re-ingest of unchanged data reports a divergence on row 1 forever. The contract
# recorded exactly this state as appendant item J-14: the constants existed, were covered
# by unit tests, and **no gate read them**. A unit test proves it for the columns someone
# thought of; a gate has to cover the columns nobody thought of, which is why the two
# "did every column get considered" lists below are compared against the DDL in both
# directions.
#
# The registry is deliberately explicit rather than "one constant per table": what binds
# a constant to a column is the column's own scale, and a table may hold columns the
# writer never touches (see UNQUANTISED).
SRC_PATH = os.path.join(ROOT, 'quanauto', 'pgstore.py')
SCALE_CONST_RE = re.compile(r'^(_[A-Z0-9_]+_SCALE)[ \t]*=[ \t]*(\d+)[ \t]*$', re.M)
# The pair is captured as two prefixes on purpose: a greedy `(_[A-Z0-9_]+)_QUANTUM` would
# backtrack to the shorter split and hand back '_VALUE' for '_VALUE_QUANTUM', so the two
# halves could not be compared to each other at all.
QUANTUM_CONST_RE = re.compile(
    r'^(_[A-Z0-9_]+)_QUANTUM[ \t]*=[ \t]*Decimal\(1\)\.scaleb\(-(_[A-Z0-9_]+)_SCALE\)'
    r'[ \t]*$', re.M)
# A scale only counts when it is the scale OF a column, so the column name is part of the
# match. NUMERIC_ANYWHERE_RE is the same match without the line anchor: comparing the two
# is what keeps a column written mid-line ("open numeric(18,4), high numeric(18,4)") from
# silently leaving the inventory, which would shrink the coverage claim without any check
# going red -- the family this repository keeps getting bitten by.
NUMERIC_COLUMN_RE = re.compile(r'^[ \t]*(\w+)[ \t]+numeric\((\d+)[ \t]*,[ \t]*(\d+)\)', re.M | re.I)
NUMERIC_ANYWHERE_RE = re.compile(r'(\w+)[ \t]+numeric\((\d+)[ \t]*,[ \t]*(\d+)\)', re.I)

#: (constant in quanauto/pgstore.py, table, columns it quantises). Kept as a list so the
#: per-row loop below can fail on the row that drifted instead of on the whole registry.
QUANTA = (
    ('_VALUE_SCALE', 'dc_daily_bar',
     ('open', 'high', 'low', 'close', 'volume', 'amount')),
    ('_FACTOR_SCALE', 'dc_adjust_factor', ('adjust_factor',)),
    ('_DIVIDEND_SCALE', 'dc_dividend', ('cash_per_share',)),
)

#: numeric columns with no writer yet, so no constant exists for them. This is a boundary
#: rather than a hole: `dc_financial_report` and `dc_index_member` have no ingestor, and
#: the contract says the four-decimal quantisation cannot simply be reused when one is
#: written. Naming them here makes the day that changes a one-line edit to a LIST THAT IS
#: CHECKED, instead of an invisible drop in coverage.
UNQUANTISED = {
    'dc_financial_report': ('revenue', 'net_profit', 'total_assets', 'total_equity', 'roe'),
    'dc_index_member': ('weight',),
}


def smoke_assert_re(name):
    return re.compile(SMOKE_ASSERT_TMPL % re.escape(name))


def evidence_images():
    """Returns (set of image names, list of problems) read from the smoke reports.

    Every report must yield at least one name: a report whose `image  :` line is gone
    (format drift) would otherwise contribute nothing and silently shrink the evidence
    side -- which is the half that makes the comparison mean anything.
    """
    problems = []
    files = sorted(glob.glob(REPORT_GLOB))
    if not files:
        return set(), ['no file matched %s' % REPORT_GLOB]
    found = set()
    for path in files:
        try:
            with open(path, encoding='utf-8-sig', errors='replace') as handle:
                text = handle.read().replace('\r\n', '\n')
        except OSError as exc:
            problems.append('cannot read %s (%s)' % (path, exc))
            continue
        hits = set(REPORT_IMAGE_RE.findall(text))
        if not hits:
            problems.append('%s records no "image  : postgres:N" line'
                            % os.path.basename(path))
        found |= hits
    return found, problems


def falsify_evidence(docs=None):
    """Returns (set of image names, list of problems) read from the falsify reports.

    `docs=None` reads tools/falsify-report*.txt from disk; a list of (basename, text)
    pairs is how --selftest drives every guard below. That parameter exists so the
    self-test needs no temporary files: a guard proven against a directory layout the
    real run never uses is only half proven, and a gate that writes files is a gate
    whose "gates wrote nothing" claim has to be argued rather than observed.

    Nothing here is allowed to be lenient -- each of these does the same damage, which is
    to make the ladder look complete while covering less:
      * a report file deleted or renamed away  -> that image silently loses its evidence
      * the `image  :` line gone (format drift) -> the report contributes 0 images, and a
        lighter-looking evidence set is exactly the shape this project keeps getting bitten by
      * verdict not OK -> a ladder run that failed is not evidence that anything bites
      * case count != the tool's current CASES -> the snapshot describes an older ladder
      * reports disagreeing with each other -> at least one is from another case list
    """
    problems = []
    if docs is None:
        docs = []
        for path in sorted(glob.glob(FALSIFY_GLOB)):
            try:
                with open(path, encoding='utf-8-sig', errors='replace') as handle:
                    docs.append((os.path.basename(path),
                                 handle.read().replace('\r\n', '\n')))
            except OSError as exc:
                problems.append('cannot read %s (%s)' % (path, exc))
    if not docs:
        return set(), problems + [
            'no falsify report to read (glob %s, or the supplied list was empty) -- with '
            'no ladder the comparison below would run over an empty set and agree with '
            'any stamp whatsoever' % FALSIFY_GLOB]
    try:
        with open(FALSIFY_TOOL, encoding='utf-8', errors='replace') as handle:
            n_cases = len(FALSIFY_CASE_RE.findall(handle.read().replace('\r\n', '\n')))
    except OSError as exc:
        return set(), problems + ['cannot read the ladder tool %s (%s)'
                                  % (FALSIFY_TOOL, exc)]
    if not n_cases:
        return set(), problems + [
            'the CASES list in %s yielded 0 entries -- every staleness comparison below '
            'would compare against nothing' % os.path.basename(FALSIFY_TOOL)]
    found = set()
    counts = {}
    for name, text in docs:
        hits = set(REPORT_IMAGE_RE.findall(text))
        if not hits:
            problems.append('%s records no "image  : postgres:N" line' % name)
        else:
            found |= hits
        verdict = FALSIFY_VERDICT_RE.search(text)
        if not verdict:
            problems.append('%s records no "verdict: ... -- ..." line' % name)
        elif verdict.group(1) != 'OK':
            problems.append('%s says verdict=%s -- a ladder run that did not pass is not '
                            'evidence that the constraints bite' % (name, verdict.group(1)))
        cover = FALSIFY_COVER_RE.search(text)
        if not cover:
            problems.append('%s records no "覆盖: N 个案例 / M 条命名 CHECK" line -- the '
                            'report cannot be tied to the case list it came from' % name)
        else:
            counts[name] = (int(cover.group(1)), int(cover.group(2)))
    stale = sorted('%s=%d' % (n, c[0]) for n, c in counts.items() if c[0] != n_cases)
    if stale:
        problems.append('falsify report(s) generated over a different case list than '
                        'tools/falsify_smoke.py now defines (%d cases): %s -- a snapshot '
                        'of an older ladder cannot be used to say the CURRENT constraints '
                        'were falsified; re-run the ladder' % (n_cases, ', '.join(stale)))
    if len(set(counts.values())) > 1:
        problems.append('the falsify reports do not agree on their coverage counts (%s) -- '
                        'at least one of them is from a different case list'
                        % ', '.join('%s=%d/%d' % (n, c[0], c[1])
                                    for n, c in sorted(counts.items())))
    return found, problems


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


def source_scales(text):
    """{'_VALUE_SCALE': 4, ...} -- the writer's module-level scale constants."""
    return {name: int(digits) for name, digits in SCALE_CONST_RE.findall(text)}


def source_quanta(text):
    """{'prefix': 'prefix'} for every `_X_QUANTUM = Decimal(1).scaleb(-_X_SCALE)`.

    The value is the prefix the quantum actually reads, so a renamed or crossed pair
    ('_FACTOR_QUANTUM' built from -_VALUE_SCALE) is a mismatch rather than a hit.
    """
    return {q: s for q, s in QUANTUM_CONST_RE.findall(text)}


def run_checks(ddl_text, contract_text, smoke_text, source_text):
    """smoke_text and source_text are REQUIRED, never defaulted.

    A defaulted argument would silently turn the check that reads it into one that runs
    only in main() and never in --selftest -- i.e. exactly the kind of detector that can
    never be seen to be broken, which is the failure this whole file is written to avoid.
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

    # --- C6: the verification stamp and the smoke evidence must say the same thing ----
    # Both halves are computed independently: the claim comes from the DDL, the evidence
    # from the report files. Neither is allowed to be empty, because an empty side would
    # make the equality below pass over nothing at all.
    evidence, ev_problems = evidence_images()
    if ev_problems:
        fail('C6', 'the evidence side of the verification stamp could not be read (%s) -- '
                   'with no evidence to compare against, the stamp is back to being a '
                   'hand-written claim, which is what this check exists to prevent'
                   % '; '.join(ev_problems))
    if not evidence:
        fail('C6', 'no smoke report yielded an image name -- the comparison below would '
                   'run over an empty set and agree with any stamp whatsoever')
    stamp = STAMP_RE.search(ddl_text)
    cited = set()
    if not stamp:
        fail('C6', 'the DDL no longer carries its "-- PG-VERIFIED-ON: <image>..." stamp -- '
                   'a verification claim that does not say which images it was run on is '
                   'not checkable, and one green run on one version does not make the '
                   "DDL's own supported-version claim true")
    else:
        cited = set(PG_IMAGE_RE.findall(stamp.group(1)))
        if not cited:
            fail('C6', 'the "-- PG-VERIFIED-ON:" stamp names no image (%r) -- the line is '
                       'there but carries no claim, so the check above would be satisfied '
                       'by decoration' % stamp.group(1).strip())
        else:
            stats['verified_on'] = ' '.join(sorted(cited))
            unevidenced = sorted(cited - evidence)
            if unevidenced:
                fail('C6', 'the DDL claims verification on %s but no tools/'
                           'sql-smoke-report*.txt records that image -- a cited version '
                           'with no evidence behind it is the same unfalsifiable claim as '
                           'before, just spelled with more versions' % unevidenced)
            hidden = sorted(evidence - cited)
            if hidden:
                fail('C6', 'a smoke report records %s but the DDL stamp does not cite it -- '
                           'the stamp understates what was actually run, and the next '
                           'reader re-derives a weaker claim than the evidence supports'
                           % hidden)
            stats['evidence_images'] = ' '.join(sorted(evidence))

    # --- C6 (second half): the falsification ladder, as wide as the same stamp -----
    # Guarded on `cited` being non-empty: when the stamp is missing or names no image the
    # failure above already says so, and reporting it again here would print one root
    # cause as two findings that mask each other.
    if cited:
        fevidence, f_problems = falsify_evidence(FALSIFY_DOCS)
        if f_problems:
            fail('C6', 'the falsification ladder could not be read (%s) -- the stamp claims '
                       'these images verified the constraints, and only the ladder shows '
                       'the constraints actually bite' % '; '.join(f_problems))
        if not fevidence:
            fail('C6', 'no falsify report yielded an image name -- the ladder comparison '
                       'below would run over an empty set and agree with any stamp '
                       'whatsoever')
        else:
            stats['falsified_on'] = ' '.join(sorted(fevidence))
            ladder_missing = sorted(cited - fevidence)
            if ladder_missing:
                fail('C6', 'the DDL claims verification on %s but the falsification ladder '
                           'covers only %s -- on the missing image(s) the constraints were '
                           'loaded, not shown to bite, so "verified" means two different '
                           'things depending on who reads it'
                           % (sorted(cited), sorted(fevidence)))
            ladder_extra = sorted(fevidence - cited)
            if ladder_extra:
                fail('C6', 'the falsification ladder covers %s but the DDL stamp does not '
                           'cite it -- the stamp understates what was actually falsified, '
                           'and the next reader re-derives a weaker claim than the '
                           'evidence supports' % ladder_extra)

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

    # --- C8: the writer's quantisation constants vs the DDL's column scales ---------
    # The registry, the exemption list and both extractors are all checked for going
    # stale. A one-directional C8 ("every registered constant matches the DDL") would
    # call a DDL with a brand-new numeric column clean, and a DDL whose column lost its
    # scale clean, so the "did anyone consider this column" question runs against the
    # DDL as well.
    scales = source_scales(source_text)
    quanta = source_quanta(source_text)
    numeric_cols = []
    for tname, body in bodies.items():
        near = NUMERIC_COLUMN_RE.findall(body)
        wide = NUMERIC_ANYWHERE_RE.findall(body)
        for col, prec, sc in near:
            numeric_cols.append((tname, col, int(sc)))
        if set(near) != set(wide):
            fail('GATE', 'the `numeric(p,s)` extractor sees a different column set in the '
                         'body of %s than a mid-line-tolerant search does (%s vs %s) -- '
                         'columns written underneath each other are the only shape this '
                         'check can inventory, so a reformat would shrink C8 silently'
                         % (tname, sorted(c[0] for c in near), sorted(c[0] for c in wide)))
    numeric_map = {(t, c): s for t, c, s in numeric_cols}
    stats['writer_scales'] = len(scales)
    stats['ddl_numeric_columns'] = len(numeric_cols)

    if not scales:
        fail('GATE', 'no `_*_SCALE = <int>` constant extracted from quanauto/pgstore.py -- '
                     'every comparison below would run over an empty registry and report '
                     'a clean result while checking nothing')
    if not quanta:
        fail('GATE', 'no `_*_QUANTUM = Decimal(1).scaleb(-_*_SCALE)` line extracted from '
                     'quanauto/pgstore.py -- the quantum/scale pairing cannot be checked, '
                     'so a call site rounding with the wrong quantum would pass')

    registered = set()
    for const, table, columns in QUANTA:
        registered.add(const)
        if const not in scales:
            fail('C8', 'quanauto/pgstore.py no longer defines %s, but it is what quantises '
                       '%s.%s -- with the constant gone the writer rounds to whatever it '
                       'happens to have' % (const, table, ', '.join(columns)))
            continue
        scale = scales[const]
        prefix = const[:-len('_SCALE')]
        quantum = prefix + '_QUANTUM'
        if prefix not in quanta:
            fail('C8', '%s is defined but its companion %s is missing (or no longer reads '
                       '`Decimal(1).scaleb(-%s)`) -- the two are one decision split into '
                       'two names, so a quantum that stopped tracking its scale is a '
                       'silent change to what gets rounded off'
                       % (const, quantum, const))
        elif quanta[prefix] != prefix:
            fail('C8', '%s is built from -%s_SCALE instead of -%s_SCALE -- the same value '
                       'now rounds differently at this call site than at the others'
                       % (quantum, quanta[prefix].lstrip('_'), prefix.lstrip('_')))
        # The pair has to reach the quantising function TOGETHER. A call site that
        # passes the scale but not the quantum still "uses" both names elsewhere in the
        # file, and it is the quantum that decides how many decimals survive.
        paired = re.compile(r'_to_decimal\([^)]*\b%s\b[^)]*\b%s\b'
                            % (re.escape(quantum), re.escape(const)))
        if not paired.search(source_text):
            fail('C8', 'no `_to_decimal(...)` call passes %s and %s together, so nothing '
                       'proves the quantum travelling with this scale is the one the '
                       'writer rounds with' % (quantum, const))
        if table not in bodies:
            fail('C8', 'C8 registers a writer for table %s, but the DDL has no such '
                       'CREATE TABLE -- the registry is describing another database'
                       % table)
            continue
        for col in columns:
            got = numeric_map.get((table, col))
            if got is None:
                fail('C8', '%s.%s is not a `numeric(p,s)` column in db/data_center.sql -- '
                           'the writer quantises it and the DDL no longer declares it'
                           % (table, col))
            elif got != scale:
                fail('C8', '%s.%s is `numeric(_,%d)` in the DDL but quanauto/pgstore.py '
                           'quantises it to %d (%s) -- the value the writer rounds to is '
                           'not the value the column can store, and the only symptom is '
                           'a re-ingest that keeps reporting a difference for data that '
                           'did not change' % (table, col, got, scale, const))

    stale = sorted(set(scales) - registered)
    if stale:
        fail('C8', 'quanauto/pgstore.py defines scale constant(s) %s that C8 does not know '
                   'about -- a new quantum the registry never learned is a column whose '
                   'scale nothing cross-checks' % stale)

    covered = set((t, c) for _k, t, cols in QUANTA for c in cols)
    exempt = set((t, c) for t, cols in UNQUANTISED.items() for c in cols)
    stray = sorted(k for k in numeric_map if k not in covered and k not in exempt)
    if stray:
        fail('C8', 'numeric column(s) in db/data_center.sql that are neither quantised by '
                   'the writer nor listed as having no writer yet: %s -- put it in QUANTA '
                   'with its constant, or in UNQUANTISED with the reason it has none'
                   % ['%s.%s' % k for k in stray])
    ghosts = sorted(k for k in exempt if k not in numeric_map)
    if ghosts:
        fail('C8', 'UNQUANTISED names column(s) that the DDL no longer has as numeric '
                   'columns: %s -- an exemption that outlived its column is exactly where '
                   'the next unquantised column hides' % ['%s.%s' % k for k in ghosts])
    stats['quanta_registered'] = len(covered)
    stats['quanta_exempt'] = len(exempt)
    stats['quanta_stray'] = len(stray)

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
    pgstore = read_text(SRC_PATH)

    ok = True
    # Counted, never hard-coded: the docs point at this number instead of copying it,
    # so a sample added tomorrow cannot make a sentence in the contract stale.
    n_samples = [0]

    def scenario(tag, d, c, code, want_clean=False, s=None, src=None):
        nonlocal ok
        n_samples[0] += 1
        issues, _ = run_checks(d, c, smoke if s is None else s,
                               pgstore if src is None else src)
        codes = set(k for k, _ in issues)
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s %s'
              % (tag, len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # CONTROL: the real artifacts. Reported but not asserted clean -- the gate is
    # expected to be able to fail on them, which is the whole point of running it.
    issues, stats = run_checks(ddl, contract, smoke, pgstore)
    print('  [control-real-artifacts] issues=%d codes=%s tables=%d documented=%d '
          'declared=%d smoke_asserted=%d verified_on=%s evidence=%s falsified_on=%s '
          'writer_scales=%d quanta=%d exempt=%d'
          % (len(issues), sorted(set(k for k, _ in issues)), stats['tables'],
             stats['documented'], stats['declared'], stats.get('smoke_asserted', -1),
             stats.get('verified_on', '(none)'), stats.get('evidence_images', '(none)'),
             stats.get('falsified_on', '(none)'), stats.get('writer_scales', -1),
             stats.get('quanta_registered', -1), stats.get('quanta_exempt', -1)))

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

    # C6a: the verification stamp silently dropped. The marker line is gone, so there is
    # no claim to compare with the evidence at all.
    bad = _mutate(ddl, '-- PG-VERIFIED-ON:', '-- STATUS:', 'NEG5a')
    if bad is None:
        ok = False
    else:
        scenario('NEG5a-stamp-dropped', bad, contract, 'C6')

    # C6b: the marker survives but names no image. This is the sample that distinguishes
    # the new rule from the old one -- a "carries a banner" check would call this input
    # clean while the claim is unverifiable again.
    bad = _mutate(ddl, '-- PG-VERIFIED-ON: postgres:14 postgres:15 postgres:16 postgres:17',
                  '-- PG-VERIFIED-ON: verified by hand', 'NEG5b')
    if bad is None:
        ok = False
    else:
        scenario('NEG5b-stamp-without-image', bad, contract, 'C6')

    # C6c: the stamp cites an image that no report covers. This is the direction the old
    # one-way check also had -- kept because it is the failure mode that puts a version in
    # the claim with nothing run behind it.
    bad = _mutate(ddl, '-- PG-VERIFIED-ON: postgres:14',
                  '-- PG-VERIFIED-ON: postgres:9 postgres:14', 'NEG5c')
    if bad is None:
        ok = False
    else:
        scenario('NEG5c-cited-image-unevidenced', bad, contract, 'C6')

    # C6d: the REVERSE direction -- a report exists that the stamp does not cite. The old
    # one-way rule called this clean, so this is the sample that proves the check is not
    # one-directional. Without it, "the gate is green" would still be compatible with a
    # DDL that quietly claims less than was actually proven.
    bad = _mutate(ddl, '-- PG-VERIFIED-ON: postgres:14 postgres:15 postgres:16 postgres:17',
                  '-- PG-VERIFIED-ON: postgres:15 postgres:16 postgres:17', 'NEG5d')
    if bad is None:
        ok = False
    else:
        scenario('NEG5d-evidence-not-cited', bad, contract, 'C6')

    # C6e: an empty evidence side. Pointing the glob at a name that cannot match proves
    # the guard without touching (much less deleting) any real report. Patched on the
    # module global, which is what evidence_images() reads at call time.
    saved_glob = REPORT_GLOB
    globals()['REPORT_GLOB'] = os.path.join(root, 'tools', '__selftest_no_such_report__*.txt')
    try:
        scenario('GATE-no-evidence-files', ddl, contract, 'C6')
    finally:
        globals()['REPORT_GLOB'] = saved_glob
    if REPORT_GLOB != saved_glob:
        print('    REPORT_GLOB not restored -- the samples after this point would run '
              'against patched state')
        ok = False

    # C6f: the FALSIFICATION ladder. One sample per guard, all built in memory (see
    # falsify_evidence()'s `docs` parameter). Without the clean sample at the end, the
    # eight negatives would be indistinguishable from a detector that fires on everything.
    # The image list is read off the real stamp, not written down here, so these samples
    # do not pin down "these four strings are the right ones" -- what they pin is how each
    # way of losing a piece of the ladder gets reported.
    def fake_report(image, n_cases=33, n_checks=32, verdict='OK', image_line=True,
                    verdict_line=True, cover_line=True):
        lines = ['--- 实测环境 ---']
        if image_line:
            lines.append('  image  : %s' % image)
        lines.append('  digest : postgres@sha256:%s' % ('0' * 8))
        lines.append('  server : PostgreSQL 99.9')
        lines.append('')
        lines.append('=== 汇总 ===')
        lines.append('  some-case                  CAUGHT')
        lines.append('')
        if verdict_line:
            lines.append('verdict: %s -- 每个被放宽的约束都被触发测试抓到了' % verdict)
        if cover_line:
            lines.append('         覆盖: %d 个案例 / %d 条命名 CHECK 约束'
                         % (n_cases, n_checks))
        return '\n'.join(lines) + '\n'

    def ladder(overrides=None, drop=()):
        docs = []
        for image in sorted(cited_now):
            if image in drop:
                continue
            docs.append(('falsify-report-%s.txt' % image.split(':')[1],
                         fake_report(image, **(overrides or {}).get(image, {}))))
        return docs

    saved_docs = FALSIFY_DOCS

    def fscenario(tag, docs, want_clean=False):
        globals()['FALSIFY_DOCS'] = docs
        try:
            scenario(tag, ddl, contract, 'C6', want_clean=want_clean)
        finally:
            globals()['FALSIFY_DOCS'] = saved_docs

    stamp_now = STAMP_RE.search(ddl)
    cited_now = set(PG_IMAGE_RE.findall(stamp_now.group(1))) if stamp_now else set()
    if not cited_now:
        print('    C6f samples: the DDL stamp names no image -- the samples below would run '
              'against an empty list, so each of them would either fire for the wrong '
              'reason or not fire at all')
        ok = False
    else:
        # the ladder is one image SHORTER than the stamp: loaded there, never shown to bite
        fscenario('NEG10a-ladder-missing-image', ladder(drop=('postgres:16',)))
        # the ladder is one image WIDER than the stamp: the stamp understates the evidence
        fscenario('NEG10b-ladder-extra-image',
                  ladder() + [('falsify-report-18.txt', fake_report('postgres:18'))])
        # a ladder run that did not pass
        fscenario('NEG10c-ladder-verdict-not-ok', ladder({'postgres:15': {'verdict': 'FAIL'}}))
        # the `image  :` line lost -> the report contributes 0 images (format drift)
        fscenario('NEG10d-ladder-no-image-line', ladder({'postgres:17': {'image_line': False}}))
        # a snapshot from an older case list, counted by the tool's own CASES
        fscenario('NEG10e-ladder-stale-cases', ladder({'postgres:14': {'n_cases': 31}}))
        # reports that disagree with each other (same case count, different CHECK count)
        fscenario('NEG10f-ladder-counts-disagree', ladder({'postgres:16': {'n_checks': 31}}))
        # the verdict line lost entirely -> nothing says the run succeeded
        fscenario('NEG10g-ladder-no-verdict-line',
                  ladder({'postgres:14': {'verdict_line': False}}))
        # the coverage line lost -> the report cannot be tied to a case list at all
        fscenario('NEG10h-ladder-no-cover-line', ladder({'postgres:14': {'cover_line': False}}))
        fscenario('GATE-no-falsify-reports', [])
        # CLEAN control: four reports, all OK, counts agreeing -> must be silent.
        fscenario('POSITIVE-ladder-complete', ladder(), want_clean=True)
        if FALSIFY_DOCS is not saved_docs:
            print('    FALSIFY_DOCS not restored -- the samples after this point would run '
                  'against patched state')
            ok = False

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

    # --- C8: the writer's scales vs the DDL's column scales -------------------------
    # C8 is the only check here that reads quanauto/pgstore.py, so these samples are the
    # only place a drift in that file can be seen. One sample per guard, and the ones
    # that mutate the DDL are scale/inventory changes: the constraint inventory is left
    # intact, so C2-C7 stay green and the input looks healthy without C8.

    # C8a: the DDL's scale for a quantised column drifts (narrowed, so the check on the
    # constant side is untouched).
    bad = _mutate(ddl, 'numeric(18,8)', 'numeric(18,6)', 'NEG11')
    if bad is None:
        ok = False
    else:
        scenario('NEG11-ddl-scale-drift', bad, contract, 'C8')

    # C8b: a brand-new numeric column nobody registered, added to a table that already
    # has numeric columns so the only thing that changed is the column set. This is the
    # half a one-directional C8 ("every registered constant matches") cannot see.
    #
    # Anchored on a whole COLUMN LINE inside a table body rather than on the bare
    # `numeric(10,6)` text: the bare text also occurs in the DDL's comments, and inserting
    # there produces a column the extractor cannot see -- the sample then looks like a
    # missing detector instead of a missed mutation. (It did, the first time.)
    bad = re.sub(r'\n([ \t]+)roe([ \t]+)numeric\(10,6\)',
                 r'\n\1roe\2numeric(10,6),\n\1eps\2numeric(10,6)', ddl, count=1)
    if bad == ddl:
        print('    sample NEG12: ANCHOR NOT FOUND (the dc_financial_report.roe column)')
        ok = False
    else:
        scenario('NEG12-numeric-column-unregistered', bad, contract, 'C8')

    # C8c: a fourth quantum appears in the writer with no registry entry. The DDL is not
    # touched at all, so nothing but C8's completeness half can notice.
    bad = _mutate(pgstore, '_DIVIDEND_SCALE = 4',
                  '_DIVIDEND_SCALE = 4\n_LATE_SCALE = 2', 'NEG13')
    if bad is None:
        ok = False
    else:
        scenario('NEG13-writer-const-unregistered', ddl, contract, 'C8', src=bad)

    # C8d: the quantum that travels with a scale is swapped at the call site. Both names
    # are still defined and still used, so "the constant exists and is referenced" stays
    # green -- only the pairing check can see this one.
    bad = _mutate(pgstore, '_FACTOR_QUANTUM, _FACTOR_SCALE',
                  '_VALUE_QUANTUM, _FACTOR_SCALE', 'NEG14')
    if bad is None:
        ok = False
    else:
        scenario('NEG14-quantum-not-paired', ddl, contract, 'C8', src=bad)

    # C8e: an exemption that outlived its column. Asserted on the message, not only on
    # the code: this mutation turns one column into a ghost AND into a stray, so a
    # code-only assertion would stay green with the staleness half deleted.
    bad = re.sub(r'weight([ \t]+)numeric\(10,6\)', r'weight_\1numeric(10,6)', ddl, count=1)
    if bad == ddl:
        print('    sample NEG15: ANCHOR NOT FOUND (the dc_index_member.weight column)')
        ok = False
    else:
        n_samples[0] += 1
        issues, _ = run_checks(bad, contract, smoke, pgstore)
        hit = any(k == 'C8' and 'no longer has as numeric columns' in m for k, m in issues)
        print('  [NEG15-exemption-stale] issues=%d codes=%s %s'
              % (len(issues), sorted(set(k for k, _ in issues)), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # GATE: a column written on the same line as another one leaves the line-anchored
    # inventory while an any-position search still finds it. Without the guard the
    # coverage claim shrinks and every remaining check stays green.
    bad = re.sub(r'\n([ \t]+)high([ \t]+)numeric\(18,4\)', r' high\2numeric(18,4)', ddl,
                 count=1)
    if bad == ddl:
        print('    sample GATE-midline-column: ANCHOR NOT FOUND (dc_daily_bar.high)')
        ok = False
    else:
        scenario('GATE-midline-column', bad, contract, 'GATE')

    # GATE: every scale constant stripped out of the writer. The registry then matches
    # nothing, and without the vacuity guard C8 would compare the DDL against no constants
    # at all and report a clean result. Stripped by the extractor's own pattern rather
    # than by a literal, so the sample cannot drift away from what is being tested.
    stripped = SCALE_CONST_RE.sub('', pgstore)
    if stripped == pgstore:
        print('    sample GATE-no-writer-scales: ANCHOR NOT FOUND (no scale constant)')
        ok = False
    else:
        scenario('GATE-no-writer-scales', ddl, contract, 'GATE', src=stripped)

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
    #
    # The stamp is DERIVED from the evidence files rather than hard-coded, so that adding
    # a report does not turn this control into a sample of a stale literal: what it pins
    # down is "a stamp that cites exactly what the reports record is clean", not "these
    # four version strings are the right ones".
    evidence_now, ev_now_problems = evidence_images()
    if ev_now_problems or not evidence_now:
        print('    POSITIVE-control: evidence files unreadable (%s) -- the control below '
              'would fail for the wrong reason' % (ev_now_problems or 'empty set'))
        ok = False
    synth_ddl = ('CREATE TABLE IF NOT EXISTS t (\n'
                 '    a int NOT NULL,\n'
                 '    CONSTRAINT ck_dc_demo CHECK (a > 0)\n'
                 ');\n'
                 # C8 reads the writer's constants against the DDL, so a control with no
                 # numeric column at all would pass C8 for the wrong reason -- by not
                 # exercising it. These five tables carry exactly the columns the real
                 # registry and the real exemption list name, one per line because that
                 # is the only column shape C8 can inventory.
                 'CREATE TABLE IF NOT EXISTS dc_daily_bar (\n'
                 '    open numeric(18,4),\n'
                 '    high numeric(18,4),\n'
                 '    low numeric(18,4),\n'
                 '    close numeric(18,4),\n'
                 '    volume numeric(20,4),\n'
                 '    amount numeric(20,4)\n'
                 ');\n'
                 'CREATE TABLE IF NOT EXISTS dc_adjust_factor (\n'
                 '    adjust_factor numeric(18,8)\n'
                 ');\n'
                 'CREATE TABLE IF NOT EXISTS dc_dividend (\n'
                 '    cash_per_share numeric(18,4)\n'
                 ');\n'
                 'CREATE TABLE IF NOT EXISTS dc_financial_report (\n'
                 '    revenue numeric(20,4),\n'
                 '    net_profit numeric(20,4),\n'
                 '    total_assets numeric(20,4),\n'
                 '    total_equity numeric(20,4),\n'
                 '    roe numeric(10,6)\n'
                 ');\n'
                 'CREATE TABLE IF NOT EXISTS dc_index_member (\n'
                 '    weight numeric(10,6)\n'
                 ');\n'
                 '-- PG-VERIFIED-ON: %s\n' % ' '.join(sorted(evidence_now)))
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
    print('samples=%d' % n_samples[0])
    return 0 if ok else 1


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if '--selftest' in sys.argv:
        return selftest()

    cpath = os.path.join(root, 'docs', '智能量化交易平台-数据中心接口契约文档.md')
    dpath = os.path.join(root, 'db', 'data_center.sql')
    spath = os.path.join(root, 'db', 'data_center.smoke.sql')
    for p in (cpath, dpath, spath, SRC_PATH):
        if not os.path.exists(p):
            print('GATE FAIL: missing input %s' % p)
            return 1

    contract, ddl, smoke = read_text(cpath), read_text(dpath), read_text(spath)
    source = read_text(SRC_PATH)
    print('CRLF-normalised: contract=%d bytes, ddl=%d bytes, smoke=%d bytes, writer=%d bytes'
          % (len(contract.encode('utf-8')), len(ddl.encode('utf-8')),
             len(smoke.encode('utf-8')), len(source.encode('utf-8'))))

    issues, stats = run_checks(ddl, contract, smoke, source)
    print('extracted: tables=%d documented=%d declared=%d in_body=%d mysqlisms=%d '
          'smoke_asserted=%d writer_scales=%d ddl_numeric=%d quanta=%d exempt=%d'
          % (stats['tables'], stats['documented'], stats['declared'],
             stats['in_body'], stats['mysqlisms'], stats.get('smoke_asserted', -1),
             stats.get('writer_scales', -1), stats.get('ddl_numeric_columns', -1),
             stats.get('quanta_registered', -1), stats.get('quanta_exempt', -1)))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))'
          % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: this gate never executes the DDL and never inspects a CHECK '
          'expression. Passing it does NOT mean the constraints reject bad values -- '
          'that is what db/data_center.smoke.sql has to print SMOKE PASS for. That run '
          'has happened (see tools/sql-smoke-report*.txt), and so has the per-constraint '
          'falsification (tools/falsify-report*.txt); both sets are SNAPSHOTS and are '
          'void the moment any db/*.sql or *.smoke.sql file changes. C8 reads the '
          'writer but only compares DECLARED scales: it cannot show that the values that '
          'arrive are within them, and it does not cross-check precision (the DDL uses '
          'numeric(18,4) and numeric(20,4) for the same constant).')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
