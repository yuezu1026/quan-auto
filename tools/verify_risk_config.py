#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
static gate over the risk-control contract + its DDL seed data.

What it protects against (in order of how much pain they cause):
  * D3 unit accident  -- a RATIO threshold written as 10 instead of 0.10 (100x error).
  * D2 layering gap   -- a rule_id that has no GLOBAL fallback row.
  * D4 masking        -- the two drawdown thresholds crossing over (circuit breaker
                         must fire strictly earlier than the lifecycle retire gate).
  * contract/DDL drift -- the default table in the contract no longer matches the
                         seed rows in db/risk_control.sql.
  * code/contract drift -- quanauto/risk.py carries its OWN copy of the six
                         rule_ids, units, directions and default thresholds. Edit
                         one number there and every other check stays green while
                         the running engine enforces a value the contract never
                         authorised. C16 closes that hole.
  * D7/D8 prerequisites -- the breaker-state / equity-peak tables going missing,
                         which would silently disable the drawdown breaker.
  * incomplete migration -- MySQL-only syntax (backticks, ENGINE=InnoDB,
                         ON DUPLICATE KEY, TINYINT ...) surviving in a PostgreSQL
                         script. This fails at CREATE TABLE time, i.e. at deploy,
                         not at the statement level.
  * a paper guard      -- a database-enforced invariant that was quietly deleted or
                         renamed, so it is only upheld by application code, which a
                         hand-written UPDATE walks straight around.

What it explicitly does NOT do:
  * It never executes the DDL. No static gate can prove that a CHECK constraint
    actually rejects anything -- a bad expression or an over-tight bound passes
    every regex. db/risk_control.smoke.sql is the trigger test for that, and C15
    below exists only to force the two files to stay in step.

Design rules enforced on this script itself:
  * Every check runs independently. No check returns early, so a failure in check N
    can never hide a failure in check N+1 (the "serial shadowing" trap).
  * Every extraction is guarded. If a regex silently extracts nothing, we FAIL loudly
    instead of reporting a beautifully clean "0 issues".
  * --selftest runs each negative sample as a SEPARATE run_checks() invocation so no
    detector can shadow another, plus one clean sample that must report zero issues.

Usage:
  python verify_risk_config.py [workspace_root]
  python verify_risk_config.py --selftest [workspace_root]
"""

import os
import re
import sys

# ---------------------------------------------------------------------------
# Expected registry -- mirrors contract section 3.1.1
# ---------------------------------------------------------------------------
EXPECTED_RULES = {
    'max_position_pct':      ('RATIO', 0.10),
    'max_daily_trades':      ('COUNT', 3),
    'strategy_drawdown_pct': ('RATIO', 0.15),
    'account_drawdown_pct':  ('RATIO', 0.20),
    'max_order_amount_pct':  ('RATIO', 0.10),
    'max_sector_pct':        ('RATIO', 0.30),
}

# Tables the contract section 3.6.1 claims to persist to.
CONTRACT_TABLES = [
    'risk_rule', 'risk_config_version', 'risk_rule_audit', 'risk_blacklist',
    'risk_intercept_log', 'risk_breaker_state', 'risk_equity_peak',
    'risk_switch_state', 'risk_decision_log',
]

# Tables whose absence silently disables a safety mechanism.
REQUIRED_SAFETY_TABLES = [
    'risk_breaker_state',   # D7 -- breaker must survive restart
    'risk_equity_peak',     # D8 -- peak loss resets drawdown to 0 forever
    'risk_rule_audit',      # D6/D10 -- who relaxed what, and when
    'risk_config_version',  # D1 -- cheap change detection
]

VALID_UNITS = ('RATIO', 'COUNT', 'ABSOLUTE')
VALID_DIRECTIONS = ('DECREASE', 'INCREASE')

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------
# PostgreSQL: CREATE TABLE IF NOT EXISTS risk_rule (...) with unquoted lowercase names.
# The optional "..." branch is tolerated only so a quoted-but-lowercase style still
# gets checked; mixed-case quoted identifiers are not supported on purpose.
CREATE_TABLE_RE = re.compile(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"?([a-z0-9_]+)"?')

# 'CREATE TABLE IF NOT EXISTS <name> (' ... ') ;' -- the body is needed so a constraint
# can be verified inside the table it belongs to, instead of merely somewhere in the
# file (a mention inside a comment must not count as "enforced").
CREATE_TABLE_BODY_RE = re.compile(
    r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"?([a-z0-9_]+)"?\s*\(', re.IGNORECASE,
)
TABLE_TERMINATOR_RE = re.compile(r'^\s*\)\s*;', re.MULTILINE)

SEED_BLOCK_RE = re.compile(
    r"INSERT\s+INTO\s+risk_rule\b(.*?)ON\s+CONFLICT",
    re.IGNORECASE | re.DOTALL,
)

# ('rule_id', 'GLOBAL', '*', 0.10, 'RATIO', 'LTE', TRUE, 1, 'system', CURRENT_TIMESTAMP(3), 'desc')
SEED_ROW_RE = re.compile(
    r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*,\s*'(RATIO|COUNT|ABSOLUTE)'\s*,\s*'(LTE|GTE)'\s*,\s*"
    r"(TRUE|FALSE|[01])\s*,\s*([0-9]+)\s*,\s*'([^']*)'\s*,\s*"
    r"(?:CURRENT_TIMESTAMP\(3\)|NOW\(3\))\s*,\s*'([^']*)'\s*\)",
    re.IGNORECASE,
)

# Leftovers from the MySQL 8.0 revision of this DDL. Every one of these is a hard
# syntax error in PostgreSQL, so a hit means the migration is incomplete.
MYSQLISM_RE = re.compile(
    r"`"                            # backtick-quoted identifier
    r"|ENGINE\s*="                  # ENGINE=InnoDB
    r"|AUTO_INCREMENT"
    r"|ON\s+DUPLICATE\s+KEY"
    r"|TINYINT"
    r"|DATETIME\s*\("               # timestamptz(3) is the intended type
    r"|SET\s+NAMES\s+utf8"
    r"|CHARSET\s*=",
    re.IGNORECASE,
)

# CHECK constraints the DDL must carry, each with the reason it exists. These are the
# last line of defence: they still hold when someone edits the table by hand, bypassing
# the risk-control process entirely. A regex gate cannot prove a constraint actually
# rejects anything (db/risk_control.smoke.sql does that); it CAN prove the constraint
# was not quietly deleted or renamed.
#   D2 = layered override, D3 = RATIO stored as 0~1, 2.2 = unit caliber.
REQUIRED_CONSTRAINTS = [
    ('risk_rule',           'ck_risk_rule_scope'),              # scope enum
    ('risk_rule',           'ck_risk_rule_unit'),               # unit enum
    ('risk_rule',           'ck_risk_rule_comparison'),         # comparison enum
    ('risk_rule',           'ck_risk_rule_ratio_range'),        # D3  : RATIO in (0, 1]
    ('risk_rule',           'ck_risk_rule_global_key'),         # D2  : GLOBAL key is *
    ('risk_rule',           'ck_risk_rule_nonneg'),             # 2.2 : COUNT/ABSOLUTE >= 0
    ('risk_rule',           'ck_risk_rule_count_int'),          # 2.2 : COUNT is integral
    ('risk_config_version', 'ck_risk_config_version_singleton'),
    ('risk_rule_audit',     'ck_risk_rule_audit_direction'),
    ('risk_intercept_log',  'ck_risk_intercept_action'),
    ('risk_decision_log',   'ck_risk_decision_action'),
    ('risk_decision_log',   'ck_risk_decision_run_state'),
    ('risk_breaker_state',  'ck_risk_breaker_state'),           # D7  : no implicit recovery
    ('risk_switch_state',   'ck_risk_switch_state_singleton'),  # kill switch is single-row
]

# | `max_position_pct` | name | RATIO | `DECREASE` | `0.10` | caliber |
SPEC_ROW_RE = re.compile(
    r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|\s*([^|]*?)\s*\|\s*(RATIO|COUNT|ABSOLUTE)\s*\|"
    r"\s*`?(DECREASE|INCREASE)`?\s*\|\s*`([0-9]+(?:\.[0-9]+)?)`\s*\|",
    re.MULTILINE,
)

# quanauto/risk.py -- the implementation's own copy of the registry:
#     RULE_SPECS: Tuple[RuleSpec, ...] = (
#         RuleSpec(
#             "max_position_pct", RuleTypeEnum.MAX_POSITION_PCT, RuleUnitEnum.RATIO,
#             TighteningDirectionEnum.DECREASE, 0.10, "单票仓位上限（…）",
#         ),
#     )
# The `\n\)` tail is what keeps the block from running into whatever comes next:
# the entries are all indented, the closing paren is not.
RULE_SPECS_BLOCK_RE = re.compile(r'RULE_SPECS\b[^=\n]*=\s*\((.*?)\n\)', re.DOTALL)
RULE_SPECS_ENTRY_RE = re.compile(
    r'RuleSpec\(\s*"([a-z][a-z0-9_]*)"\s*,\s*RuleTypeEnum\.[A-Z0-9_]+\s*,\s*'
    r'RuleUnitEnum\.(RATIO|COUNT|ABSOLUTE)\s*,\s*'
    r'TighteningDirectionEnum\.(DECREASE|INCREASE)\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*,',
    re.DOTALL,
)

# | `risk_rule` | duty | writer |
TABLE_LIST_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|", re.MULTILINE)


def read_text(path):
    """Read a file and normalise CRLF -> LF.

    A bare \\n regex against a CRLF file matches nothing and every downstream check
    then vacuously passes. Normalising first is mandatory, not cosmetic.
    """
    with open(path, 'r', encoding='utf-8-sig') as fh:
        return fh.read().replace('\r\n', '\n').replace('\r', '\n')


def extract_seed_rows(sql_text):
    """Return list of dicts for the risk_rule GLOBAL seed rows."""
    rows = []
    block = SEED_BLOCK_RE.search(sql_text)
    if not block:
        return rows
    for m in SEED_ROW_RE.finditer(block.group(1)):
        rows.append({
            'rule_id': m.group(1),
            'scope': m.group(2),
            'scope_key': m.group(3),
            'threshold_raw': float(m.group(4)),
            'unit': m.group(5),
            'comparison': m.group(6),
            'enabled': m.group(7),
            'version': int(m.group(8)),
            'updated_by': m.group(9),
            'description': m.group(10),
        })
    return rows


def extract_spec_rows(contract_text):
    """Return {rule_id: {'unit': .., 'direction': .., 'default': float}} from 3.1.1."""
    spec = {}
    for m in SPEC_ROW_RE.finditer(contract_text):
        spec[m.group(1)] = {
            'unit': m.group(3),
            'direction': m.group(4),
            'default': float(m.group(5)),
        }
    return spec


def extract_rule_specs(specs_text):
    """Return the same shape as extract_spec_rows(), but from the implementation.

    Parsed with a regex instead of imported on purpose: this gate must stay runnable
    in a bare checkout (no venv, no install), and an import would mean a syntax error
    in risk.py silently turns C16 -- and only C16 -- into a no-op.
    """
    block = RULE_SPECS_BLOCK_RE.search(specs_text)
    if not block:
        return {}
    specs = {}
    for m in RULE_SPECS_ENTRY_RE.finditer(block.group(1)):
        specs[m.group(1)] = {
            'unit': m.group(2),
            'direction': m.group(3),
            'default': float(m.group(4)),
        }
    return specs


def extract_contract_tables(contract_text):
    """Backticked first-column names from the section 3.6.1 table listing."""
    names = []
    for m in TABLE_LIST_ROW_RE.finditer(contract_text):
        name = m.group(1)
        if name.startswith('risk_') or name.startswith('db/'):
            names.append(name)
    return names


def extract_table_bodies(sql_text):
    """Return {table_name: body_text} for every CREATE TABLE in the DDL.

    The body stops at the first line that is just `);`, which is how these tables are
    formatted. Having the body lets a check ask "is this constraint defined *on this
    table*" rather than "does this string appear anywhere in the file".
    """
    bodies = {}
    for m in CREATE_TABLE_BODY_RE.finditer(sql_text):
        tail = sql_text[m.end():]
        end = TABLE_TERMINATOR_RE.search(tail)
        bodies[m.group(1).lower()] = tail[:end.start()] if end else tail
    return bodies


def run_checks(sql_text, contract_text, smoke_text, specs_text):
    """Run every check independently. Returns (issues, stats).

    smoke_text is the content of db/risk_control.smoke.sql. It is a required
    argument, not an optional one: a defaulted argument would silently turn C15
    into a check that runs only in main() and never in --selftest, i.e. exactly the
    kind of detector that can never be seen to be broken. specs_text (the content of
    quanauto/risk.py) is required for the same reason -- it feeds C16.
    """
    issues = []
    stats = {}

    def fail(code, msg):
        issues.append((code, msg))

    # ------------------------------------------------------------------
    # Extraction + vacuity gates. These MUST come first: if an extraction
    # returns nothing, every semantic check below becomes a no-op and we
    # would print a deceptively clean "0 issues".
    # ------------------------------------------------------------------
    seed_rows = extract_seed_rows(sql_text)
    ddl_tables = CREATE_TABLE_RE.findall(sql_text)
    spec_rows = extract_spec_rows(contract_text)
    rule_specs = extract_rule_specs(specs_text)
    contract_tables = extract_contract_tables(contract_text)
    table_bodies = extract_table_bodies(sql_text)

    stats['seed_rows'] = len(seed_rows)
    stats['ddl_tables'] = len(ddl_tables)
    stats['spec_rows'] = len(spec_rows)
    stats['rule_specs'] = len(rule_specs)
    stats['contract_table_refs'] = len(contract_tables)
    stats['table_bodies'] = len(table_bodies)

    if not seed_rows:
        fail('GATE', 'extracted 0 risk_rule seed rows from db/risk_control.sql '
                     '-- every downstream threshold check would pass vacuously')
    if not ddl_tables:
        fail('GATE', 'extracted 0 CREATE TABLE statements from db/risk_control.sql '
                     '-- schema checks would pass vacuously')
    if not table_bodies:
        fail('GATE', 'extracted 0 CREATE TABLE bodies from db/risk_control.sql '
                     '-- the CHECK constraint inventory would pass vacuously')
    if not spec_rows:
        fail('GATE', 'extracted 0 registry rows from contract section 3.1.1 '
                     '-- contract/DDL consistency checks would pass vacuously')
    if not rule_specs:
        fail('GATE', 'extracted 0 RuleSpec entries from quanauto/risk.py -- C16 '
                     'would compare two empty registries and report a clean run, '
                     'leaving the code/contract drift hole wide open')
    if not contract_tables:
        fail('GATE', 'extracted 0 table references from contract section 3.6.1 '
                     '-- persistence coverage checks would pass vacuously')

    # ------------------------------------------------------------------
    # C1 / C2: registry coverage, both directions.
    # ------------------------------------------------------------------
    seed_ids = [r['rule_id'] for r in seed_rows]
    seen = set(seed_ids)

    for rule_id in sorted(set(EXPECTED_RULES) - seen):
        fail('C1', "expected rule_id '%s' has no seed row" % rule_id)

    for rule_id in sorted(seen - set(EXPECTED_RULES)):
        fail('C2', "seed row for undeclared rule_id '%s' (not in contract 3.1.1)" % rule_id)

    for rule_id in sorted(seen & set(EXPECTED_RULES) - set(spec_rows)):
        fail('C1', "rule_id '%s' is seeded but missing from the contract registry" % rule_id)

    # ------------------------------------------------------------------
    # C3 / C8: exactly one GLOBAL row per rule, scope_key must be '*'.
    # ------------------------------------------------------------------
    global_counts = {}
    for r in seed_rows:
        if r['scope'] == 'GLOBAL':
            global_counts[r['rule_id']] = global_counts.get(r['rule_id'], 0) + 1

    for rule_id in sorted(set(EXPECTED_RULES)):
        n = global_counts.get(rule_id, 0)
        if n == 0:
            fail('C3', "rule_id '%s' has no GLOBAL fallback row (D2 requires one)" % rule_id)
        elif n > 1:
            fail('C3', "rule_id '%s' has %d GLOBAL rows, expected exactly 1" % (rule_id, n))

    for r in seed_rows:
        if r['scope'] == 'GLOBAL' and r['scope_key'] != '*':
            fail('C8', "GLOBAL row for '%s' has scope_key=%r, must be '*'"
                       % (r['rule_id'], r['scope_key']))

    # ------------------------------------------------------------------
    # C4: the 100x guard. RATIO thresholds live in (0, 1], COUNT are integers.
    # ------------------------------------------------------------------
    for r in seed_rows:
        rid, unit, value = r['rule_id'], r['unit'], r['threshold_raw']
        if unit == 'RATIO':
            if not (0.0 < value <= 1.0):
                fail('C4', "RATIO threshold for '%s' is %s, outside (0, 1] "
                           "-- likely the 100x unit accident (D3)" % (rid, value))
        elif unit == 'COUNT':
            if value != int(value) or value < 0:
                fail('C4', "COUNT threshold for '%s' is %s, expected a non-negative "
                           "integer" % (rid, value))
        elif unit not in VALID_UNITS:
            fail('C4', "unknown unit %r for '%s'" % (unit, rid))

        if r['comparison'] not in ('LTE', 'GTE'):
            fail('C4', "unknown comparison %r for '%s'" % (r['comparison'], rid))

    # ------------------------------------------------------------------
    # C5 / C6 / C11: contract registry must agree with the DDL seed.
    # ------------------------------------------------------------------
    by_id = {r['rule_id']: r for r in seed_rows}

    for rule_id, spec in sorted(spec_rows.items()):
        if rule_id not in EXPECTED_RULES:
            fail('C11', "contract registry lists '%s', which is not in EXPECTED_RULES "
                        "-- checker and contract have drifted apart" % rule_id)
        if spec['unit'] not in VALID_UNITS:
            fail('C11', "contract registry unit %r for '%s' is invalid" % (spec['unit'], rule_id))
        if spec['direction'] not in VALID_DIRECTIONS:
            fail('C11', "contract registry direction %r for '%s' is invalid"
                        % (spec['direction'], rule_id))

        row = by_id.get(rule_id)
        if row is None:
            fail('C5', "contract registry declares '%s' but no seed row exists" % rule_id)
            continue
        if abs(row['threshold_raw'] - spec['default']) > 1e-9:
            fail('C5', "threshold drift for '%s': contract says %s, seed says %s"
                       % (rule_id, spec['default'], row['threshold_raw']))
        if row['unit'] != spec['unit']:
            fail('C6', "unit drift for '%s': contract says %s, seed says %s"
                       % (rule_id, spec['unit'], row['unit']))

    # ------------------------------------------------------------------
    # C7: circuit breaker must fire strictly earlier than lifecycle retire.
    # Checked on BOTH sides on purpose: if only the seed were validated, the
    # document a human reads (and copies an implementation from) could state the
    # opposite ordering and nothing would notice.
    # ------------------------------------------------------------------
    strat = by_id.get('strategy_drawdown_pct')
    acct = by_id.get('account_drawdown_pct')
    if strat and acct:
        if not strat['threshold_raw'] < acct['threshold_raw']:
            fail('C7', "seed: strategy_drawdown_pct (%s) must be strictly tighter than "
                       "account_drawdown_pct (%s): breaker brakes first, retire later"
                       % (strat['threshold_raw'], acct['threshold_raw']))

    strat_spec = spec_rows.get('strategy_drawdown_pct')
    acct_spec = spec_rows.get('account_drawdown_pct')
    if strat_spec and acct_spec:
        if not strat_spec['default'] < acct_spec['default']:
            fail('C7', "contract: strategy_drawdown_pct (%s) must be strictly tighter "
                       "than account_drawdown_pct (%s): breaker brakes first, retire "
                       "later" % (strat_spec['default'], acct_spec['default']))

    # ------------------------------------------------------------------
    # C16: the implementation carries its own copy of the registry
    # (quanauto/risk.py RULE_SPECS). It must agree with the contract field by field.
    # Everything else in this file checks the DDL and the contract against each
    # other; without C16 the code could enforce 0.12 for a rule the contract calls
    # 0.10 and remain green -- a drift that only surfaces as "the breaker fired at a
    # number nobody agreed on".
    # ------------------------------------------------------------------
    for rule_id in sorted(set(rule_specs) - set(spec_rows)):
        fail('C16', "quanauto/risk.py defines '%s', which contract 3.1.1 does not "
                    "list -- an implementation-only rule bypasses the registry" % rule_id)
    for rule_id in sorted(set(spec_rows) - set(rule_specs)):
        fail('C16', "contract 3.1.1 lists '%s', which quanauto/risk.py does not "
                    "define -- the contract would promise a guard that is not wired "
                    "up" % rule_id)

    for rule_id in sorted(set(rule_specs) & set(spec_rows)):
        impl, spec = rule_specs[rule_id], spec_rows[rule_id]
        if impl['unit'] != spec['unit']:
            fail('C16', "unit drift for '%s': risk.py=%s, contract=%s"
                        % (rule_id, impl['unit'], spec['unit']))
        if impl['direction'] != spec['direction']:
            fail('C16', "tightening direction drift for '%s': risk.py=%s, contract=%s "
                        "-- a flipped direction turns a relaxation guard into its "
                        "opposite" % (rule_id, impl['direction'], spec['direction']))
        if abs(impl['default'] - spec['default']) > 1e-12:
            fail('C16', "default threshold drift for '%s': risk.py=%s, contract=%s"
                        % (rule_id, impl['default'], spec['default']))

    # ------------------------------------------------------------------
    # C9 / C10: every contract-declared table must exist in the DDL, and the
    # safety-critical ones must never go missing.
    # ------------------------------------------------------------------
    ddl_set = set(ddl_tables)
    for name in contract_tables:
        if name not in ddl_set:
            fail('C9', "contract section 3.6.1 declares table '%s' but the DDL "
                       "never creates it" % name)

    for name in REQUIRED_SAFETY_TABLES:
        if name not in ddl_set:
            fail('C10', "safety table '%s' is missing from the DDL -- its absence "
                        "silently disables a protection mechanism" % name)

    # ------------------------------------------------------------------
    # C12: nothing in the DDL may make a threshold nullable, because NULL would
    # read back as 0.0 and quietly reject every order (or every position check).
    # ------------------------------------------------------------------
    rule_ddl = table_bodies.get('risk_rule', '')
    if rule_ddl:
        for col in ('threshold', 'unit', 'scope', 'scope_key', 'enabled'):
            m = re.search(r'^\s*%s\s+([^\n]*)' % col, rule_ddl, re.MULTILINE)
            if not m:
                fail('C12', "column '%s' not found in risk_rule DDL" % col)
            elif 'NOT NULL' not in m.group(1):
                fail('C12', "column '%s' in risk_rule is nullable -- NULL would read "
                            "back as a falsy threshold" % col)
    else:
        fail('C12', "could not locate the risk_rule table body, so the nullability "
                    "check could not run -- refusing to pass vacuously")

    # ------------------------------------------------------------------
    # C14: the database-enforced invariants must still be there. Verified INSIDE each
    # table's own body with word boundaries, because (a) a mention inside a comment is
    # not a constraint and (b) renaming one to '<name>_disabled' keeps the original name
    # as a substring, so a plain `in` test would report the guard as present while it is
    # gone. A missing table is skipped here on purpose: C9/C10 already report it, and
    # duplicating would just bury the real message.
    # ------------------------------------------------------------------
    for table, cname in REQUIRED_CONSTRAINTS:
        body = table_bodies.get(table)
        if body is not None and not re.search(r'\b%s\b' % re.escape(cname), body):
            fail('C14', "table '%s' is missing CHECK constraint '%s' -- a "
                        "database-enforced invariant was removed, leaving the rule to "
                        "application code that a manual UPDATE can bypass"
                        % (table, cname))

    # ------------------------------------------------------------------
    # C13: the script must be valid PostgreSQL. Any MySQL-ism left behind is a
    # hard failure at CREATE TABLE time, i.e. it breaks deployment outright.
    # ------------------------------------------------------------------
    leftovers = sorted(set(m.group(0).strip().lower()
                           for m in MYSQLISM_RE.finditer(sql_text)))
    if leftovers:
        fail('C13', "db/risk_control.sql still contains MySQL-only syntax %s -- "
                    "the PostgreSQL migration is incomplete" % leftovers)

    # ------------------------------------------------------------------
    # C15: the trigger test must keep up with the constraint inventory. A new
    # database-enforced invariant that ships without a sample which must fail is an
    # unproven guard -- and "it is written in the DDL" is precisely the claim C14
    # already makes and cannot substantiate on its own.
    # ------------------------------------------------------------------
    if smoke_text is None:
        fail('GATE', 'the DDL smoke test was not supplied to run_checks -- the '
                     'constraint coverage check would pass vacuously')
    elif not smoke_text.strip():
        fail('GATE', 'db/risk_control.smoke.sql is empty -- there is no trigger test '
                     'for the database-level guards')
    else:
        untested = [cname for _, cname in REQUIRED_CONSTRAINTS
                    if not re.search(r'\b%s\b' % re.escape(cname), smoke_text)]
        if untested:
            fail('C15', "db/risk_control.smoke.sql never exercises %s -- every "
                        "database-enforced invariant needs a sample that must fail"
                        % untested)
        stats['smoke_constraints_tested'] = len(REQUIRED_CONSTRAINTS) - len(untested)

    return issues, stats


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _mutate(text, old, new, tag):
    """Assert the mutation actually happened. A silent no-op mutation would make a
    'green' self-test meaningless (it would be testing the unmodified file)."""
    if old not in text:
        print('  SELFTEST SETUP FAIL: %s -- anchor not found' % tag)
        return None
    return text.replace(old, new, 1)


def selftest(root):
    contract_path = os.path.join(root, 'docs', '智能量化交易平台-风控层接口契约文档.md')
    sql_path = os.path.join(root, 'db', 'risk_control.sql')
    smoke_path = os.path.join(root, 'db', 'risk_control.smoke.sql')
    specs_path = os.path.join(root, 'quanauto', 'risk.py')

    for path in (contract_path, sql_path, smoke_path, specs_path):
        if not os.path.exists(path):
            print('SELFTEST FAIL: missing %s' % path)
            return 1

    sql = read_text(sql_path)
    contract = read_text(contract_path)
    smoke = read_text(smoke_path)
    specs = read_text(specs_path)

    ok = True

    # ---- POSITIVE CONTROL: the real files must be clean. -------------------
    issues, stats = run_checks(sql, contract, smoke, specs)
    print('  [positive ] real files        -> issues=%d (extracted seed=%d ddl=%d spec=%d impl=%d)'
          % (len(issues), stats['seed_rows'], stats['ddl_tables'], stats['spec_rows'],
             stats['rule_specs']))
    if issues:
        ok = False
        for code, msg in issues:
            print('             UNEXPECTED: [%s] %s' % (code, msg))

    # ---- NEGATIVE CONTROL 1: GATE vacuity guard. --------------------------
    # Strip the seed block entirely. If the gate is wired up we must see GATE
    # failures; without the gate this input would report a perfect 0 issues.
    no_seed = re.sub(r"INSERT\s+INTO\s+risk_rule\b.*?\n\s*ON\s+CONFLICT[^;]*;",
                     "", sql, flags=re.IGNORECASE | re.DOTALL)
    if no_seed == sql:
        print('  SELFTEST SETUP FAIL: NEG1 could not strip the seed block')
        ok = False
    else:
        issues, _ = run_checks(no_seed, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'GATE' in codes and 'C3' in codes
        print('  [negative1] seed stripped     -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 2: the 100x unit accident (C4). -----------------
    bad_unit = _mutate(sql,
                       "('max_position_pct',      'GLOBAL', '*', 0.10,",
                       "('max_position_pct',      'GLOBAL', '*', 10,",
                       'NEG2')
    if bad_unit is None:
        ok = False
    else:
        issues, _ = run_checks(bad_unit, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C4' in codes and 'C5' in codes
        print('  [negative2] ratio 0.10 -> 10  -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 3: contract/DDL threshold drift (C5). -----------
    bad_gate = _mutate(contract, '| `0.15` |', '| `0.25` |', 'NEG3')
    if bad_gate is None:
        ok = False
    else:
        issues, _ = run_checks(sql, bad_gate, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C5' in codes and 'C7' in codes
        print('  [negative3] contract 0.15->0.25 -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 4: GLOBAL layer dropped (C3). -------------------
    bad_global = _mutate(sql,
                         "('max_sector_pct',        'GLOBAL',",
                         "('max_sector_pct',        'STRATEGY',",
                         'NEG4')
    if bad_global is None:
        ok = False
    else:
        issues, _ = run_checks(bad_global, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C3' in codes
        print('  [negative4] GLOBAL -> STRATEGY -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 5: safety table removed (C10). ------------------
    bad_tbl = _mutate(sql,
                      'CREATE TABLE IF NOT EXISTS risk_equity_peak',
                      'CREATE TABLE IF NOT EXISTS risk_equity_peak_unused',
                      'NEG5')
    if bad_tbl is None:
        ok = False
    else:
        issues, _ = run_checks(bad_tbl, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C10' in codes and 'C9' in codes
        print('  [negative5] peak table gone  -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 6: MySQL-ism left behind (C13). -----------------
    # Simulates a half-finished migration: one backtick-quoted identifier slips
    # back in. PostgreSQL rejects this at CREATE TABLE time.
    bad_my = _mutate(sql,
                     'PRIMARY KEY (rule_id, scope, scope_key),',
                     'PRIMARY KEY (`rule_id`, scope, scope_key),',
                     'NEG6')
    if bad_my is None:
        ok = False
    else:
        issues, _ = run_checks(bad_my, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C13' in codes
        print('  [negative6] backtick back    -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 7: RATIO guard demoted to a paper guard (C14). --
    bad_guard = _mutate(sql,
                        'CONSTRAINT ck_risk_rule_ratio_range CHECK',
                        'CONSTRAINT ck_risk_rule_ratio_range_disabled CHECK',
                        'NEG7')
    if bad_guard is None:
        ok = False
    else:
        issues, _ = run_checks(bad_guard, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C14' in codes
        print('  [negative7] ratio guard gone -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 8: a constraint removed from a DIFFERENT table. --
    # NEG7 proves C14 fires for risk_rule; on its own that would not prove the
    # inventory loop ever looks at the other 13 entries. This sample does.
    bad_other = _mutate(sql,
                        'CONSTRAINT ck_risk_breaker_state CHECK',
                        'CONSTRAINT ck_risk_breaker_state_disabled CHECK',
                        'NEG8')
    if bad_other is None:
        ok = False
    else:
        issues, _ = run_checks(bad_other, contract, smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C14' in codes
        print('  [negative8] breaker CHECK gone -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 9: a constraint with no trigger test (C15). -----
    # Drop one constraint NAME from the smoke test only. The DDL still has it, so
    # WITHOUT C15 this input would look perfectly healthy -- which is the whole
    # point: an unproven guard is indistinguishable from a working one until it is
    # asked to reject something.
    bad_smoke = _mutate(smoke,
                        'ck_risk_rule_count_int',
                        'ck_risk_rule_count_int_decoy',
                        'NEG9')
    if bad_smoke is None:
        ok = False
    else:
        issues, _ = run_checks(sql, contract, bad_smoke, specs)
        codes = set(c for c, _ in issues)
        hit = 'C15' in codes
        print('  [negative9] smoke test gap -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 10: implementation registry drift (C16). --------
    # The implementation keeps its own copy of the six thresholds. Change one there
    # and, without C16, every other check stays green while the engine enforces 0.12
    # for a rule the contract calls 0.10.
    bad_impl = _mutate(specs,
                       'TighteningDirectionEnum.DECREASE, 0.10, "单票仓位上限',
                       'TighteningDirectionEnum.DECREASE, 0.12, "单票仓位上限',
                       'NEG10')
    if bad_impl is None:
        ok = False
    else:
        issues, _ = run_checks(sql, contract, smoke, bad_impl)
        codes = set(c for c, _ in issues)
        hit = 'C16' in codes
        print('  [negative10] risk.py 0.10->0.12 -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 11: a renamed rule in the implementation (C16). --
    # Proves the id-set comparison is not vacuous: a rule that exists in one registry
    # and not the other must be reported, not quietly intersected away.
    bad_impl_id = _mutate(specs,
                          '"max_sector_pct", RuleTypeEnum.MAX_SECTOR_PCT',
                          '"max_sector_share", RuleTypeEnum.MAX_SECTOR_PCT',
                          'NEG11')
    if bad_impl_id is None:
        ok = False
    else:
        issues, _ = run_checks(sql, contract, smoke, bad_impl_id)
        codes = set(c for c, _ in issues)
        hit = 'C16' in codes
        print('  [negative11] rule renamed   -> issues=%d codes=%s %s'
              % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    # ---- NEGATIVE CONTROL 12: C16's own vacuity guard. ---------------------
    # Feed it a source with no RULE_SPECS block at all. If C16 were wired up to
    # compare two empty registries it would print a perfect "0 issues" -- the exact
    # failure mode this whole gate exists to prevent, applied to the gate itself.
    no_specs = 'GLOBAL_RULE_IDS = ()\n'
    issues, _ = run_checks(sql, contract, smoke, no_specs)
    codes = set(c for c, _ in issues)
    hit = 'GATE' in codes
    print('  [negative12] no RULE_SPECS  -> issues=%d codes=%s %s'
          % (len(issues), sorted(codes), 'OK' if hit else 'MISSED'))
    ok = ok and hit

    print('SELFTEST %s' % ('OK: all 12 negative controls fire, clean sample stays clean'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = args[0] if args else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if '--selftest' in sys.argv:
        print('selftest root: %s' % root)
        sys.exit(selftest(root))

    contract_path = os.path.join(root, 'docs', '智能量化交易平台-风控层接口契约文档.md')
    sql_path = os.path.join(root, 'db', 'risk_control.sql')
    smoke_path = os.path.join(root, 'db', 'risk_control.smoke.sql')
    specs_path = os.path.join(root, 'quanauto', 'risk.py')

    for path in (contract_path, sql_path, smoke_path, specs_path):
        if not os.path.exists(path):
            print('GATE FAIL: missing input %s' % path)
            sys.exit(1)

    sql = read_text(sql_path)
    contract = read_text(contract_path)
    smoke = read_text(smoke_path)
    specs = read_text(specs_path)

    print('CRLF-normalised: contract=%d bytes, sql=%d bytes, smoke=%d bytes, specs=%d bytes'
          % (len(contract.encode('utf-8')), len(sql.encode('utf-8')),
             len(smoke.encode('utf-8')), len(specs.encode('utf-8'))))

    issues, stats = run_checks(sql, contract, smoke, specs)

    print('extracted: seed_rows=%d ddl_tables=%d spec_rows=%d rule_specs=%d '
          'contract_table_refs=%d required_constraints=%d covered_by_smoke=%d'
          % (stats['seed_rows'], stats['ddl_tables'], stats['spec_rows'],
             stats['rule_specs'], stats['contract_table_refs'], len(REQUIRED_CONSTRAINTS),
             stats.get('smoke_constraints_tested', 0)))

    for code, msg in issues:
        print('FAIL [%s] %s' % (code, msg))

    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    sys.exit(0 if not issues else 1)


if __name__ == '__main__':
    main()
