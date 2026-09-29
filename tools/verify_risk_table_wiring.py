#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
static gate over "does the risk layer really write the tables the contract says?"

What it protects against:
  * the观测 gap itself -- contract section 3.6.1 lists nine `risk_*` tables, and a
    table with no writer is *not* an error anywhere else in this repo: it just sits
    there, empty, and every report about it is vacuously clean. The gap that this
    gate was written for (2026-09-25) was exactly that shape: `risk_decision_log`
    shipped in the DDL, was covered by the DDL/contract consistency gate, and had
    ZERO writers -- so every check over that table passed over an empty set.
  * a mention counted as a writer -- `quanauto/risk.py` line ~498 is a docstring
    saying "persisted to the local file + the risk_switch_state table". A grep-based
    check calls that a writer. It is prose. This gate parses with `ast` and drops
    docstrings, so "it is mentioned in a comment/docstring" can never satisfy it.
  * a capability quietly dropped -- a writer that still writes the row but lost the
    property that made the row useful (`risk_config_version` must *increase*, else the
    cheap change-detection poll in D1 never fires; `risk_equity_peak` must be
    monotone, else the drawdown breaker resets itself to zero). Both are one-token
    edits that keep every other gate green.
  * an undocumented exemption -- exempting a table is allowed, but only when the
    reason is written down in the contract itself and the code really has no writer.
    Both directions are checked, so an exemption can neither be created by editing
    this gate alone nor be left behind after the table gets wired.

What it explicitly does NOT do:
  * It does not run any SQL. "The statement exists in the source" is not "the row
    landed" -- that is `tools/run_sql_smoke.py` and the store tests' job.
  * It does not judge whether a writer is *correct*, only whether it exists, is
    attributed to the component the contract names, and kept its key capability.

Design rules enforced on this script itself:
  * Every check runs independently; no check returns early.
  * The expected policy is hand-written (POLICY below) on purpose: a new table in the
    contract must force a human decision here, instead of being silently absorbed.
    The contract supplies the *table list* and any *named component*, and both
    directions of that comparison are failures.
  * Every extraction is guarded: zero tables extracted, zero parsed sources, or zero
    write keywords anywhere all FAIL rather than reporting a clean "0 issues".
  * `--selftest` runs each negative sample as a SEPARATE run_checks() call plus one
    clean sample, and each sample asserts the SPECIFIC code that must fire.

Usage:
  python verify_risk_table_wiring.py [workspace_root]
  python verify_risk_table_wiring.py --selftest [workspace_root]
"""

import ast
import glob
import os
import re
import sys

# ---------------------------------------------------------------------------
# Expected policy -- hand-written, one entry per table in contract 3.6.1
# ---------------------------------------------------------------------------
# kind:
#   'python' -> some module under quanauto/ must issue a write for it.
#   'ops'    -> the contract says it is maintained through an ops entry point, so
#               the product code must NOT write it. A writer appearing here means
#               either the contract's writer column is stale or an unintended path
#               was added; both need a human.
# class:  the component the contract names in its writer column, when it names one.
#         When present, that class must exist AND be one of the detected writers.
# capability: a regex that must match the writer's own literals. This is the
#         "shape, not name" half: the table being written is not enough if the
#         property that makes the row worth writing is gone.
POLICY = {
    'risk_rule': {
        'kind': 'python',
        'class': 'DbRiskRuleStore',
    },
    'risk_config_version': {
        'kind': 'python',
        'class': 'DbRiskRuleStore',
        # D1's cheap change detection is a poll on a number that must move. A writer
        # that stops incrementing leaves a perfectly written, permanently constant
        # version -- every consumer keeps reading the old rules forever.
        'capability': r'version\s*=\s*version\s*\+\s*1',
        'capability_what': 'version 必须递增（D1 的低成本变更检测靠它变化）',
    },
    'risk_rule_audit': {
        'kind': 'python',
        'class': 'DbRiskRuleStore',
    },
    'risk_blacklist': {
        # Contract 3.6.3: the blacklist is a name list maintained by an operator, on
        # purpose not through risk_rule (whose threshold is numeric).
        'kind': 'ops',
    },
    'risk_intercept_log': {
        'kind': 'python',
    },
    'risk_breaker_state': {
        'kind': 'python',
    },
    'risk_equity_peak': {
        'kind': 'python',
        # D8: the peak is what makes drawdown-from-peak meaningful. "peak_value =
        # EXCLUDED.peak_value" writes the row on every tick and destroys the meaning.
        'capability': r'GREATEST',
        'capability_what': 'peak_value 必须只增不减（GREATEST），否则回撤自峰值恒为 0（D8）',
    },
    'risk_switch_state': {
        'kind': 'python',
        'class': 'KillSwitch',
    },
    'risk_decision_log': {
        'kind': 'python',
    },
}

# Tables whose missing writer is registered in the contract instead of being fixed
# yet. Two conditions are enforced for every entry, in both directions:
#   * the contract text must carry a 'WIRING-EXEMPT: <table>' marker, so the reason
#     lives in the authoritative document rather than in this file alone;
#   * the product code must really have no writer. The moment one appears, the
#     exemption is stale and must be deleted -- an exemption that outlives its gap
#     is how a table ends up permanently un-watched.
EXEMPT = {
    'risk_switch_state': (
        'KillSwitch 只写本地 JSON（os.replace 原子替换）；存储侧走 '
        'getattr(self._store, "save_switch_state") 这个可选钩子，'
        '但目前没有任何实现类提供它，也没有任何 SQL 写这张表'
    ),
}

EXEMPT_MARKER = 'WIRING-EXEMPT'

# Components forbidden from doing IO inside RiskEngine.check() (contract D1).
D1_METHOD = 'check'
D1_CLASS = 'RiskEngine'

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------
# The 3.6.1 listing is sliced out of the whole document rather than matched
# globally: several later sections carry tables whose first column is also a
# backticked identifier, and a global regex would happily adopt them.
SECTION_HEAD_RE = re.compile(r'^####\s+3\.6\.1\b.*$', re.MULTILINE)
SECTION_END_RE = re.compile(r'^####\s', re.MULTILINE)

# | `risk_rule` | 权威阈值配置 | 仅 `DbRiskRuleStore` |
TABLE_ROW_RE = re.compile(
    r'^\|\s*`([a-z][a-z0-9_]*)`\s*\|([^|\n]*)\|([^|\n]*)\|\s*$', re.MULTILINE,
)

WRITE_KEYWORD_RE = re.compile(r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b', re.IGNORECASE)

# A component the contract names has to look like a Python class, not like prose
# that happens to sit in backticks. The second character must be lowercase on
# purpose: a writer cell that grows a backticked `` `PASS` `` / `` `REJECT` ``
# (all-caps vocabulary, not a class name) would otherwise be demanded to exist as
# a class -- and a check that fails on correct documents is a check that gets
# switched off, taking the real half with it.
NAMED_CLASS_RE = re.compile(r'`([A-Z][a-z][A-Za-z0-9_]*)`')

# Names that mean "this method performs IO" when called from inside check().
IO_CALL_NAMES = ('record', 'insert_sql', 'execute', 'execute_sql', 'commit')


def read_text(path):
    """Read as UTF-8 and normalise CRLF, so no pattern can silently miss.

    The 2026-09-15 lesson: a bare `\\n` pattern against a CRLF file extracts
    nothing and every downstream check then passes vacuously.
    """
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read().replace('\r\n', '\n')


def extract_contract_tables(contract_text):
    """Return [(table_name, writer_cell), ...] from contract section 3.6.1."""
    head = SECTION_HEAD_RE.search(contract_text)
    if not head:
        return []
    tail = SECTION_END_RE.search(contract_text, head.end())
    block = contract_text[head.end():tail.start() if tail else len(contract_text)]
    rows = []
    for m in TABLE_ROW_RE.finditer(block):
        rows.append((m.group(1), m.group(3).strip()))
    return rows


def _collect_docstrings(tree):
    """Every docstring value in the module, as a set.

    Values rather than nodes: `ast.get_docstring` returns the string itself, and a
    SQL literal that is byte-identical to some docstring does not exist. Dropping
    them is what makes "a mention is not a writer" true for docstrings. Comments
    need no handling at all -- they never reach the AST.
    """
    values = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                values.add(doc)
    return values


def _literals(scope_node, docstrings, exclude_classes=False):
    """Non-docstring string constants inside a scope.

    `exclude_classes` is needed for the module scope: `ast.walk(Module)` reaches
    into every class body, so without it the module would be credited with every
    write performed anywhere in the file. A miss of that kind does not show up as a
    wrong finding -- it shows up as a check that can no longer fail.
    """
    out = []

    if exclude_classes:
        def descend(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    continue
                if (isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and child.value not in docstrings):
                    out.append(child.value)
                descend(child)
        descend(scope_node)
        return out

    for node in ast.walk(scope_node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                out.append(node.value)
    return out


def _mentions(literal, table):
    """Boundary-aware: `risk_rule` must not be satisfied by `risk_rule_audit`."""
    return re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(table),
                     literal) is not None


def scan_source(relpath, text):
    """Return (writer_scopes, capability_text) for one source file.

    writer_scopes: {table: set(scope_label)} where scope_label is 'Name' for a
    class and '<module>' for module scope (including statements that are not in
    any class).

    Two detection shapes, because the two styles in this repo differ:
      * the statement literal carries the table name (DbRiskRuleStore, the
        intercept writer) -- matched directly;
      * the statement is templated (`"INSERT INTO %s ..." % cls.TABLE`) and the
        table name lives in a class constant (RiskDecisionLogWriter) -- matched by
        requiring a table-named constant AND a write keyword in the SAME class.
    """
    tree = ast.parse(text)
    docstrings = _collect_docstrings(tree)

    writer_scopes = {}

    def record(scope_label, table):
        writer_scopes.setdefault(table, set()).add(scope_label)

    def visit(scope_node, scope_label):
        lits = _literals(scope_node, docstrings,
                         exclude_classes=(scope_label == '<module>'))
        has_keyword = any(WRITE_KEYWORD_RE.search(lit) for lit in lits)
        for lit in lits:
            if not WRITE_KEYWORD_RE.search(lit):
                continue
            for table in POLICY:
                if _mentions(lit, table):
                    record(scope_label, table)
        # Rule B, class scope only. At module scope `risk.py` contains every class,
        # so a table-named constant anywhere in it would credit the module with a
        # write it never performs.
        if scope_label != '<module>' and has_keyword:
            for lit in lits:
                if lit in POLICY:
                    record(scope_label, lit)

    visit(tree, '<module>')
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            visit(node, node.name)
    return writer_scopes


def scan_sources(sources):
    """Merge scan_source over {relpath: text}. Returns {table: {file::scope}}."""
    merged = {}
    for relpath in sorted(sources):
        for table, scopes in scan_source(relpath, sources[relpath]).items():
            for scope in scopes:
                merged.setdefault(table, set()).add('%s::%s' % (relpath, scope))
    return merged


def _scope_text(relpath, scope, sources):
    """The literals of one scope, for the capability check."""
    text = sources.get(relpath)
    if text is None:
        return ''
    tree = ast.parse(text)
    docstrings = _collect_docstrings(tree)
    if scope == '<module>':
        return '\n'.join(_literals(tree, docstrings, exclude_classes=True))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == scope:
            return '\n'.join(_literals(node, docstrings))
    return ''


def collect_classes(sources):
    """{class_name: relpath} for every class defined in the scanned sources."""
    found = {}
    for relpath in sorted(sources):
        try:
            tree = ast.parse(sources[relpath])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found.setdefault(node.name, relpath)
    return found


def collect_methods(sources):
    """{class_name: set(method names)} -- so "the class exists" and "the method
    exists" are not confused with each other."""
    found = {}
    for relpath in sorted(sources):
        try:
            tree = ast.parse(sources[relpath])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bucket = found.setdefault(node.name, set())
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    bucket.add(child.name)
    return found


def d1_violations(sources):
    """() -> [(relpath, detail)] for IO reached from inside RiskEngine.check().

    Contract D1 forbids IO on the check() path. The wiring style chosen for both log
    writers exists precisely to keep the SQL out of that call, and it is easy to
    break by "helpfully" writing the log right where the verdict is computed.
    """
    problems = []
    for relpath in sorted(sources):
        try:
            tree = ast.parse(sources[relpath])
        except SyntaxError:
            continue
        docstrings = _collect_docstrings(tree)
        for cls in ast.walk(tree):
            if not (isinstance(cls, ast.ClassDef) and cls.name == D1_CLASS):
                continue
            for fn in cls.body:
                if not (isinstance(fn, ast.FunctionDef) and fn.name == D1_METHOD):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Constant)
                            and isinstance(node.value, str)
                            and node.value not in docstrings
                            and WRITE_KEYWORD_RE.search(node.value)):
                        problems.append((relpath, 'check() 里出现了 SQL 字面量：%r'
                                         % node.value.strip()[:60]))
                    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        if node.func.attr in IO_CALL_NAMES:
                            problems.append((relpath, 'check() 里调用了 IO 方法 %r'
                                             % node.func.attr))
                    elif isinstance(node, ast.Name) and node.id.endswith('LogWriter'):
                        problems.append((relpath, 'check() 里引用了写入方 %r' % node.id))
    return problems


def run_checks(sources, other_sources, contract_text):
    """Run every check independently. Returns (issues, notes, stats).

    sources maps quanauto/*.py (product code) to text; other_sources maps
    tests/** and tools/** to text and is used ONLY to say "the only writers are in
    the tests", which is a materially different situation from "there is no writer
    at all" and should not have to be guessed from the message.
    """
    issues = []
    notes = []
    stats = {}

    def fail(code, msg):
        issues.append((code, msg))

    # ------------------------------------------------------------------
    # Extraction + vacuity gates. These must come first: an extraction that
    # returns nothing turns every check below into a no-op, and the report then
    # looks cleaner than a real pass.
    # ------------------------------------------------------------------
    rows = extract_contract_tables(contract_text)
    stats['contract_tables'] = len(rows)
    stats['sources'] = len(sources)
    if not rows:
        fail('GATE', 'extracted 0 rows from contract section 3.6.1 -- every check in '
                     'this gate would pass over an empty table list')
    if not sources:
        fail('GATE', 'no product sources were supplied -- the writer scan would find '
                     'nothing anywhere and report a clean run')
    if not collect_classes(sources):
        fail('GATE', 'no class was parsed out of quanauto/*.py -- the writer scan is '
                     'not actually reading the product code')

    keyword_files = [p for p, t in sources.items() if WRITE_KEYWORD_RE.search(t)]
    stats['sources_with_write_keyword'] = len(keyword_files)
    if not keyword_files:
        fail('GATE', 'no write keyword (INSERT INTO / UPDATE / DELETE FROM) appears in '
                     'any product source -- the scan is broken, not the code')

    contract_tables = [name for name, _ in rows]
    cells = dict(rows)

    # ------------------------------------------------------------------
    # W1: the contract's table list and this gate's policy must agree, both
    # directions. Without the second direction a table deleted from the contract
    # would keep its stale policy entry forever.
    # ------------------------------------------------------------------
    for table in contract_tables:
        if table not in POLICY:
            fail('WIRING-POLICY-UNKNOWN',
                 "contract 3.6.1 lists '%s' but POLICY has no entry -- a new table "
                 "must be classified here (wired / ops-only / exempt), otherwise it "
                 "silently joins the set of tables nobody writes" % table)
    for table in sorted(set(POLICY) - set(contract_tables)):
        fail('WIRING-POLICY-STALE',
             "POLICY covers '%s', which contract 3.6.1 no longer lists -- delete the "
             "stale entry rather than leaving a checkpoint on a dropped table" % table)

    # ------------------------------------------------------------------
    # The scan itself.
    # ------------------------------------------------------------------
    writers = scan_sources(sources)
    other_writers = scan_sources(other_sources) if other_sources else {}
    classes = collect_classes(sources)
    stats['tables_with_writer'] = len(writers)
    stats['writer_sites'] = sum(len(v) for v in writers.values())

    # ------------------------------------------------------------------
    # W2: per-table policy.
    # ------------------------------------------------------------------
    for table in contract_tables:
        spec = POLICY.get(table)
        if spec is None:
            continue
        found = sorted(writers.get(table, ()))
        named = NAMED_CLASS_RE.findall(cells.get(table, ''))
        exempt = EXEMPT.get(table)

        if spec.get('class'):
            if spec['class'] not in classes:
                fail('WIRING-CLASS-MISSING',
                     "POLICY attributes '%s' to class '%s', which does not exist in "
                     "quanauto/*.py" % (table, spec['class']))

        # ...and the other direction of the same idea: a class named in the CONTRACT's
        # writer column must exist too. POLICY is hand-written and therefore only as
        # fresh as the last person who read this file; the contract is the document a
        # reader actually trusts. A renamed/deleted component leaves the contract
        # naming a class that is gone, which reads as "someone writes this table" when
        # nobody does.
        for name in named:
            if name not in classes:
                fail('WIRING-CLASS-MISSING',
                     "contract 3.6.1 writes '%s' as written by '%s', but no such class "
                     "exists in quanauto/*.py -- the writer column names a component "
                     "that is gone" % (table, name))

        if spec['kind'] == 'ops':
            if found:
                fail('WIRING-UNEXPECTED',
                     "contract 3.6.1 says '%s' is maintained through an ops entry "
                     "point, but product code writes it at %s -- either the contract's "
                     "writer column is stale or an unintended path was added"
                     % (table, found))
            continue

        if exempt:
            if found:
                fail('WIRING-EXEMPT-STALE',
                     "'%s' is exempted in this gate but product code now writes it at "
                     "%s -- the exemption has outlived its gap and must be removed "
                     "(along with its '%s: %s' marker in the contract)"
                     % (table, found, EXEMPT_MARKER, table))
            if ('%s: %s' % (EXEMPT_MARKER, table)) not in contract_text:
                fail('WIRING-EXEMPT-MISSING',
                     "'%s' is exempted here but the contract carries no '%s: %s' "
                     "marker -- an exemption whose reason is only in this file is "
                     "invisible to whoever reads the contract"
                     % (table, EXEMPT_MARKER, table))
            continue

        if not found:
            extra = sorted(other_writers.get(table, ()))
            hint = ('' if not extra else
                    '（只有测试/工具里有写入方 %s -- 那不算产品写入方）' % extra)
            fail('WIRING-GAP',
                 "contract 3.6.1 lists '%s' but nothing under quanauto/ writes it "
                 "-- the table is dead weight and every report about it is "
                 "vacuously clean%s" % (table, hint))
            continue

        if spec.get('class'):
            scopes = set(site.split('::', 1)[1] for site in found)
            if spec['class'] not in scopes:
                fail('WIRING-GAP',
                     "contract attributes '%s' to '%s', but the writers found are %s "
                     "-- the named component no longer writes the table"
                     % (table, spec['class'], found))

        capability = spec.get('capability')
        if capability:
            pattern = re.compile(capability)
            hits = []
            for site in found:
                relpath, scope = site.split('::', 1)
                if pattern.search(_scope_text(relpath, scope, sources)):
                    hits.append(site)
            if not hits:
                fail('WIRING-GAP-CAPABILITY',
                     "'%s' is written at %s but no writer matches /%s/ -- %s"
                     % (table, found, capability,
                        spec.get('capability_what', 'a required capability is gone')))

        # NOTE, not a failure: several contract cells describe *who decides* rather
        # than *which class issues the SQL* ("风控引擎" is the engine, but the
        # statement is issued by DbRiskRuleStore / the log writers). Printed so the
        # difference stays visible instead of being rediscovered by hand each time.
        if not named:
            notes.append("'%s' 契约写入方写作「%s」（未点名类），实测写入作用域是 %s"
                         % (table, cells.get(table, ''), found))

    # ------------------------------------------------------------------
    # W3: D1 -- no IO on the check() path.
    # ------------------------------------------------------------------
    for relpath, detail in d1_violations(sources):
        fail('WIRING-D1', '%s: %s（契约 D1 明令 check() 内禁止 IO）' % (relpath, detail))

    if D1_CLASS not in classes:
        notes.append('未在产品代码里找到 %s 类，D1 结构检查可能没生效' % D1_CLASS)
    elif D1_METHOD not in collect_methods(sources).get(D1_CLASS, set()):
        notes.append('未在产品代码里找到 %s.%s，D1 结构检查可能没生效'
                     % (D1_CLASS, D1_METHOD))

    stats['exempt'] = len(EXEMPT)
    return issues, notes, stats


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _mutate(text, old, new, tag):
    """Assert the mutation happened: a silent no-op would leave a 'green' selftest
    that is really testing the unmodified file."""
    if old not in text:
        print('  SELFTEST SETUP FAIL: %s -- anchor not found' % tag)
        return None
    if text.replace(old, new, 1) == text:
        print('  SELFTEST SETUP FAIL: %s -- mutation was a no-op' % tag)
        return None
    return text.replace(old, new, 1)


def _read_sources(root, subdir):
    out = {}
    pattern = os.path.join(root, subdir, '**', '*.py')
    for path in sorted(glob.glob(pattern, recursive=True)):
        out[os.path.relpath(path, root).replace(os.sep, '/')] = read_text(path)
    return out


# A synthetic module used by the samples that need "product code really writes this".
# It is fed as an extra source file, so the REAL detector runs over it; the only
# thing faked is which files exist.
STUB_TEMPLATE = '''\
class _StubWriter:
    SQL = "%s"
    TABLE = "%s"
'''


def selftest(root):
    contract_path = os.path.join(root, 'docs', '智能量化交易平台-风控层接口契约文档.md')
    if not os.path.exists(contract_path):
        print('SELFTEST FAIL: missing %s' % contract_path)
        return 1

    contract = read_text(contract_path)
    sources = _read_sources(root, 'quanauto')
    others = {}
    others.update(_read_sources(root, 'tests'))
    others.update(_read_sources(root, 'tools'))
    if not sources:
        print('SELFTEST FAIL: no quanauto/*.py read')
        return 1

    ok = True

    def sample(label, issues, expect_codes, expect_in_message=None):
        nonlocal ok
        codes = set(c for c, _ in issues)
        hit = all(c in codes for c in expect_codes)
        if hit and expect_in_message is not None:
            hit = any(expect_in_message in m for _, m in issues)
        print('  [%-11s] issues=%-3d codes=%-42s %s'
              % (label, len(issues), ','.join(sorted(codes)) or '(none)',
                 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- POSITIVE CONTROL: the real files must be clean. -------------------
    issues, notes, stats = run_checks(sources, others, contract)
    print('  [positive   ] real files  -> issues=%d (tables=%d writers=%d sites=%d '
          'exempt=%d)'
          % (len(issues), stats['contract_tables'], stats['tables_with_writer'],
             stats['writer_sites'], stats['exempt']))
    if issues:
        ok = False
        for code, msg in issues:
            print('             UNEXPECTED: [%s] %s' % (code, msg))
    if not notes:
        print('  SELFTEST SETUP FAIL: expected the scope notes to be non-empty')
        ok = False

    # ---- NEG1: the whole 3.6.1 listing removed (vacuity). ---------------
    # Every row must go, not one of them: with a single row left the extraction is
    # non-empty and this guard would correctly stay silent.
    stripped = re.sub(r'^\|\s*`risk_[a-z_]+`\s*\|[^\n]*\|\s*$', '', contract,
                      flags=re.MULTILINE)
    if stripped == contract:
        print('  SELFTEST SETUP FAIL: NEG1 removed no rows')
        ok = False
    else:
        issues, _, _ = run_checks(sources, others, stripped)
        sample('NEG1-empty', issues, ['GATE'])

    # ---- NEG2: no product sources at all (vacuity). ---------------------
    issues, _, _ = run_checks({}, {}, contract)
    sample('NEG2-nosrc', issues, ['GATE'])

    # ---- NEG3: sources with no write keyword at all (vacuity). ----------
    issues, _, _ = run_checks({'quanauto/dummy.py': 'x = 1\n'}, {}, contract)
    sample('NEG3-nokw', issues, ['GATE'])

    # ---- NEG4: the decision-log writer loses its table binding. ---------
    # The row is no longer written anywhere, while every other table stays fine --
    # i.e. exactly the shape of the original 零写入方 gap.
    mut = _mutate(sources['quanauto/risk.py'],
                  '    TABLE = "risk_decision_log"',
                  '    TABLE = "risk_decision_log_tmp"', 'NEG4')
    if mut is None:
        ok = False
    else:
        src = dict(sources, **{'quanauto/risk.py': mut})
        issues, _, _ = run_checks(src, others, contract)
        sample('NEG4-nowriter', issues, ['WIRING-GAP'])

    # ---- NEG5: capability gone, row still written (version not bumped). --
    mut = _mutate(sources['quanauto/risk.py'],
                  '"UPDATE risk_config_version SET version = version + 1, updated_at = %s "',
                  '"UPDATE risk_config_version SET version = version, updated_at = %s "',
                  'NEG5')
    if mut is None:
        ok = False
    else:
        src = dict(sources, **{'quanauto/risk.py': mut})
        issues, _, _ = run_checks(src, others, contract)
        sample('NEG5-version', issues, ['WIRING-GAP-CAPABILITY'])

    # ---- NEG6: capability gone on a SECOND table (peak may go down). -----
    # NEG5 alone would not prove the capability machinery is a general check rather
    # than one hand-fitted string.
    mut = _mutate(sources['quanauto/risk.py'],
                  'peak_value = GREATEST(risk_equity_peak.peak_value, EXCLUDED.peak_value), ',
                  'peak_value = EXCLUDED.peak_value, ', 'NEG6')
    if mut is None:
        ok = False
    else:
        src = dict(sources, **{'quanauto/risk.py': mut})
        issues, _, _ = run_checks(src, others, contract)
        sample('NEG6-peak', issues, ['WIRING-GAP-CAPABILITY'])

    # ---- NEG7: the exemption marker removed from the contract. ----------
    mut = _mutate(contract,
                  '%s: risk_switch_state' % EXEMPT_MARKER, 'WIRING-PENDING', 'NEG7')
    if mut is None:
        ok = False
    else:
        issues, _, _ = run_checks(sources, others, mut)
        sample('NEG7-exmark', issues, ['WIRING-EXEMPT-MISSING'])

    # ---- NEG8: the exempted table gets a writer (stale exemption). ------
    stub = STUB_TEMPLATE % ('INSERT INTO risk_switch_state (a) VALUES (%s)', 'unused')
    src = dict(sources, **{'quanauto/_neg8.py': stub})
    issues, _, _ = run_checks(src, others, contract)
    sample('NEG8-exstale', issues, ['WIRING-EXEMPT-STALE'])

    # ---- NEG9: an ops-only table written by code. ----------------------
    stub = STUB_TEMPLATE % ('INSERT INTO risk_blacklist (symbol) VALUES (%s)', 'unused')
    src = dict(sources, **{'quanauto/_neg9.py': stub})
    issues, _, _ = run_checks(src, others, contract)
    sample('NEG9-ops', issues, ['WIRING-UNEXPECTED'])

    # ---- NEG10: a new contract row with no policy entry. ---------------
    mut = _mutate(contract,
                  '| --- | --- | --- |\n| `risk_rule` |',
                  '| --- | --- | --- |\n| `risk_future_table` | 占位 | 风控引擎 |\n'
                  '| `risk_rule` |', 'NEG10')
    if mut is None:
        ok = False
    else:
        issues, _, _ = run_checks(sources, others, mut)
        sample('NEG10-newrow', issues, ['WIRING-POLICY-UNKNOWN'])

    # ---- NEG11: a contract row deleted, policy entry left behind. ------
    mut = _mutate(contract,
                  '| `risk_decision_log` | 决策日志，含 `rule_version`（D9） | '
                  '风控引擎（异步批量） |\n', '', 'NEG11')
    if mut is None:
        ok = False
    else:
        issues, _, _ = run_checks(sources, others, mut)
        sample('NEG11-stale', issues, ['WIRING-POLICY-STALE'])

    # ---- NEG12: the contract names a class that does not exist. --------
    mut = _mutate(contract, '| 仅 `DbRiskRuleStore` |\n| `risk_config_version`',
                  '| 仅 `DbRiskRuleStoreXX` |\n| `risk_config_version`', 'NEG12')
    if mut is None:
        ok = False
    else:
        issues, _, _ = run_checks(sources, others, mut)
        sample('NEG12-noclass', issues, ['WIRING-CLASS-MISSING'])

    # ---- NEG13: IO reached from inside check() (D1). -------------------
    # "Helpfully" writing the log where the verdict is computed. Injected into the
    # real check() body so the detector walks real code, not a stub; the anchor is
    # asserted by _mutate, so a renamed line can never turn this sample into a no-op.
    mut = _mutate(sources['quanauto/risk.py'],
                  '        self._observe_peak(account_peak, snapshot.total_asset)\n',
                  '        self._observe_peak(account_peak, snapshot.total_asset)\n'
                  '        self._decision_log.record(request, None)\n', 'NEG13')
    if mut is None:
        ok = False
    else:
        src = dict(sources, **{'quanauto/risk.py': mut})
        issues, _, _ = run_checks(src, others, contract)
        sample('NEG13-d1', issues, ['WIRING-D1'])

    # ---- NEG14: the only writer lives in the tests. --------------------
    # Same code as NEG4, but the message must say so: "there is no writer anywhere"
    # and "the tests write it" call for different responses.
    mut = _mutate(sources['quanauto/risk.py'],
                  '    TABLE = "risk_decision_log"',
                  '    TABLE = "risk_decision_log_tmp"', 'NEG14')
    if mut is None:
        ok = False
    else:
        src = dict(sources, **{'quanauto/risk.py': mut})
        others2 = dict(others, **{
            'tests/_neg14.py': STUB_TEMPLATE
            % ('INSERT INTO risk_decision_log (a) VALUES (%s)', 'unused')})
        issues, _, _ = run_checks(src, others2, contract)
        sample('NEG14-testonly', issues, ['WIRING-GAP'], '只有测试/工具里有写入方')

    print('SELFTEST %s' % ('OK: all 14 negative controls fire, clean sample stays clean'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = args[0] if args else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if '--selftest' in sys.argv:
        print('selftest root: %s' % root)
        sys.exit(selftest(root))

    contract_path = os.path.join(root, 'docs', '智能量化交易平台-风控层接口契约文档.md')
    if not os.path.exists(contract_path):
        print('GATE FAIL: missing input %s' % contract_path)
        sys.exit(1)

    contract = read_text(contract_path)
    sources = _read_sources(root, 'quanauto')
    others = {}
    others.update(_read_sources(root, 'tests'))
    others.update(_read_sources(root, 'tools'))

    print('CRLF-normalised: contract=%d bytes, product sources=%d file(s)'
          % (len(contract.encode('utf-8')), len(sources)))

    issues, notes, stats = run_checks(sources, others, contract)

    print('extracted: contract_tables=%d sources=%d sources_with_write_keyword=%d '
          'tables_with_writer=%d writer_sites=%d exempt=%d'
          % (stats['contract_tables'], stats['sources'],
             stats['sources_with_write_keyword'], stats['tables_with_writer'],
             stats['writer_sites'], stats['exempt']))
    for note in notes:
        print('NOTE %s' % note)
    for code, msg in issues:
        print('FAIL [%s] %s' % (code, msg))

    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    sys.exit(0 if not issues else 1)


if __name__ == '__main__':
    main()
