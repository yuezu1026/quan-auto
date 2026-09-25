#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for the I4 performance board -- `quanauto/dashboard.py`.

Why this file exists
--------------------
A board is a *view*, and a view has exactly one way to be wrong that nobody notices:
it recomputes. If the board derives its own Sharpe / drawdown / win rate from the equity
curve while the report also carries those fields, the two can drift by a few basis
points forever and both of them will look perfectly reasonable. There is no crash, no
exception, no red test -- the board simply says a slightly different number than the
artifact it claims to be showing.

So the board gets one checkable statement, enforced by a gate, with a trigger test:

    Every number the board shows is a number `PerformanceAnalyzer` produced from this
    report's own trades and account history.

That is deliberately a **three-way** statement, and the three sides are what the three
checks below measure:

    stored `deterministic.performance`   --(C10)-->  re-run `PerformanceAnalyzer`
                   ^                                          ^
                   |  (C3)                                    |  (C3)
                   +--------------  board read-out  -----------+

`C10` is the side the I4 DoD trigger test aims at ('手工改一份 BacktestResult 里的某个指标，
确认一致性门禁会红'): the file's stored metric is compared against a fresh analyzer run,
so a hand-edited number in the report turns the gate red. `C3` is the side that catches a
board computing its own metrics. Without `C10` the whole thing would be an identity --
comparing a report against the report -- which is exactly the mistake this gate made in
its first version (14 false clean samples proving nothing).

Nothing here judges whether those numbers are *right*. Arithmetic is
`performance.py`'s business and is guarded by `tools/verify_backtest_reproducibility.py`
plus the regression tests. This gate only refuses to let the board become a second,
silently divergent implementation of the same metric definitions (the I4 DoD phrase:
'不得另立一套指标定义').

Checks
------
  C1  GATE  the report yielded a usable `performance` section and `read_view` accepted
            it, and the view carries exactly `len(METRIC_SPECS)` reads. An extraction
            that silently yields 0 reads would make every equality check below run over
            an empty list and print a clean 'verdict: PASS (0 issue(s))' -- the most
            convincing possible lie.
  C2        `METRIC_SPECS` key set == `PerformanceMetrics` dataclass field set
            (bidirectional). Missing = the board hides a metric; extra = the board
            invented one the upstream model does not have.
  C3        for every spec, `MetricRead.value` equals the report field bit for bit, and
            `MetricRead.text == format_metric(spec, value)`. This is the check that
            catches a board which computes its own number.
  C4        the sample itself is non-empty: >= 1 round trip, >= 2 distinct equity
            values, curve length >= 2. C3 on two empty reports is not a verification.
  C5        determinism: `render_text` / `render_html` produce byte-identical output on
            repeated calls, and rendering a report that went through
            `dump_report` -> `json.loads` gives the same text (the I4 DoD artifact
            criterion: '同一份结果文件渲染两次，数值逐位一致').
  C6  STATIC `quanauto/dashboard.py` must not import the analyzer (`performance`) and
            must not contain any identifier that betrays a second implementation
            (`stdev`, `pct_change`, `cumprod`, `mean`, ...). Scanned through the AST, so
            comments and docstrings -- including this module's own prose about *not*
            importing `quanauto.performance` -- cannot produce false positives.
  C7        the two cross-section identities `performance.py` guarantees still hold on
            the real report as read through the board: `final_equity == equity[-1]`
            (`performance.py:172`) and `max_drawdown == max(drawdown_series(equity))`
            (`performance.py:274`). This is also what catches a `min`/`max` mix-up in
            the curve summary -- the drawdown in a report is a *positive* magnitude, so
            taking `min` displays 0.000000 forever and looks like a strategy that never
            drew down.
  C8        built-in falsification, two complementary sweeps:
              (a) change one `performance` field at a time and require the read-out to
                  follow on **that key and only that key** -- a field the board does not
                  actually read is a field whose consistency claim is decoration;
              (b) change the non-`performance` segments (scale the equity curve, append
                  to trades/orders/account_history) and require the read-out to stay
                  **bit for bit identical** -- those segments are exactly what
                  `performance.py` recomputes from, so if a board reads them it is a
                  second implementation wearing the report's clothes.
  C9        the rendered text must encode to GBK. This console is cp936 and a single
            character outside GBK in a printed line raises UnicodeEncodeError
            mid-report, which once turned a hard failure into an apparent crash.
  C10       every stored `performance` field equals what `PerformanceAnalyzer.analyze()`
            produces **right now** from this report's own `trades` / `orders` /
            `account_history`. This is the only side of the triangle that has a source
            of truth outside the file, so it is the one that notices a hand-edited or
            stale number. The analyzer is re-run, not re-implemented -- the reference
            is `quanauto.performance` itself.

What this gate does NOT do
--------------------------
It does not check that the metrics are correct, that the curve is plausible, or that
the board looks good. C2 only says the field *names* line up. C10's reference is the
same analyzer that wrote the numbers, so a metric computed by the wrong formula agrees
with itself and passes; 'the board, the file and the analyzer agree' is not 'the report
is right'.

Measured on 2026-09-25 against a real backtest run (seed 7, tests/fixtures/
sample_prices.csv, slippage 0): 14 metrics, 60 curve points, 3 trades re-analyzed, all
detectors clean, and C8's 18/18 observations caught.
"""

import ast
import contextlib
import copy
import dataclasses
import io
import json
import os
import sys
from datetime import datetime
from enum import Enum
from typing import Union, get_type_hints

sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DASHBOARD_SRC = os.path.join(ROOT, 'quanauto', 'dashboard.py')
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'sample_prices.csv')

# Imported at module level so `_rebuild` / `analyzer_reference` can raise it too; the
# board's own contract is what decides whether a report is usable, and this gate must
# reject a report on the same terms the board does.
from quanauto.errors import DashboardError  # noqa: E402  (ROOT goes on sys.path above)

# The board is allowed to *name* these modules in prose -- only the parsed code counts.
FORBIDDEN_MODULES = ('performance', 'statistics', 'numpy', 'pandas', 'scipy')

# Identifiers that only show up when something is being computed a second time.
# Kept short and specific on purpose: a token that a legitimate formatting helper
# could use would turn this check into noise, and noisy checks get waived.
FORBIDDEN_IDENTIFIERS = (
    'stdev', 'pstdev', 'variance', 'pvariance', 'fmean', 'mean', 'median',
    'percentile', 'quantile', 'cumprod', 'cumsum', 'pct_change',
    'sqrt', 'std', 'corr', 'cov',
)

# Vacuity guard for C6: a file that is not the board module at all must be rejected,
# not scanned-and-declared-clean (an empty .gitignore has no forbidden import either).
REQUIRED_DEFS = ('read_view', 'render_text', 'render_html', 'curve_stats')
REQUIRED_CONSTANTS = ('METRIC_SPECS',)


# ---------------------------------------------------------------------------------
# The '↔' incident, again. This console is cp936, and a message built out of a
# malformed sample can contain any character at all. Printing it raised
# UnicodeEncodeError *while the issue list was being written*, which lost the whole
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

    Normalising first is mandatory: anchors below are compared against bare '\\n'
    text, and a CRLF file would make every substitution a silent no-op.
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


# ---------------------------------------------------------------------------------
# C6 -- static. AST, not text: `dashboard.py`'s own docstring says '本模块不 import
# `quanauto.performance`', and a substring scan would flag that sentence as the bug it
# is warning about. Parsing removes docstrings and comments from consideration for
# free, and it makes 'an identifier named stdev' precise instead of approximate.
# ---------------------------------------------------------------------------------
def code_facts(src):
    """Import module names, identifier names and top-level definitions of a source."""
    tree = ast.parse(src, filename=os.path.basename(DASHBOARD_SRC))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = ('.' * (node.level or 0)) + (node.module or '')
            imports.add(base)
            for alias in node.names:
                imports.add(base + '.' + alias.name)
    idents = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            idents.add(node.id)
        elif isinstance(node, ast.Attribute):
            idents.add(node.attr)
    defs = set()
    consts = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            consts.add(node.target.id)
    return {'imports': imports, 'idents': idents, 'defs': defs, 'consts': consts}


def static_checks(src, label='quanauto/dashboard.py'):
    issues = []
    stats = {'imports': 0, 'source_bytes': len(src.encode('utf-8'))}
    try:
        facts = code_facts(src)
    except SyntaxError as exc:
        issues.append(('GATE', '%s does not parse as Python (%s) -- the C6 static '
                               'check would otherwise pass on any text at all'
                       % (label, exc)))
        return issues, stats

    stats['imports'] = len(facts['imports'])
    missing = [name for name in REQUIRED_DEFS if name not in facts['defs']]
    missing += [name for name in REQUIRED_CONSTANTS if name not in facts['consts']]
    if missing:
        issues.append(('GATE', '%s does not look like the board module: %s not found '
                               'at module level -- an unrelated file has no forbidden '
                               'import either, so C6 must refuse to score it'
                       % (label, ', '.join(missing))))
        return issues, stats

    for name in sorted(facts['imports']):
        parts = [p for p in name.split('.') if p]
        if any(p in FORBIDDEN_MODULES for p in parts):
            issues.append(('C6', '%s imports %r -- the board must not be able to '
                                 'recompute a metric; the only permitted source is the '
                                 'report' % (label, name)))
    for name in sorted(facts['idents'] & set(FORBIDDEN_IDENTIFIERS)):
        issues.append(('C6', '%s uses the identifier %r -- that is a formula trace, '
                             'i.e. a second implementation of a metric definition'
                       % (label, name)))
    return issues, stats


# ---------------------------------------------------------------------------------
# The outside reference (C10). Everything else in this file compares a report against
# itself or against the board; this is the one place a *second* computation is allowed
# in, because it is the only way a check can notice that the file itself is wrong.
#
# It is deliberately the real `PerformanceAnalyzer`, not a re-implementation: a second
# implementation would be the very thing this gate exists to forbid, and it would drift
# from the analyzer's definitions the same silent way a board would.
# ---------------------------------------------------------------------------------
def _convert(annotation, value):
    """Turn one JSON value back into the type `annotation` asks for."""
    if value is None:
        return None
    if getattr(annotation, '__origin__', None) is Union:
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) != 1:
            raise DashboardError('cannot rebuild a value of type %r from JSON' % (annotation,))
        return _convert(args[0], value)
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return annotation(value)
        if annotation is datetime:
            return datetime.fromisoformat(value)
    return value


def _rebuild(cls, data, where):
    """Rebuild a `quanauto.models` dataclass from its JSON form.

    The gate has to hand the analyzer *dataclasses*, because that is what `analyze()`
    takes -- a report on disk is JSON. Three things are checked here that a plain
    `cls(**data)` would not be: every declared field must be present (a renamed or
    dropped key in `to_jsonable` would otherwise raise a bare `TypeError` deep inside
    the analyzer and be reported as a crash); an enum value that is not a member of the
    enum is refused loudly instead of being handed on as a raw string; and the field
    annotations are resolved through `get_type_hints`.

    That last one is not decoration. `quanauto/models.py` writes its annotations as
    strings (`from __future__ import annotations`), so `field.type` is the **string**
    `'Direction'`, not the enum. Testing `isinstance(field.type, type)` against it is
    always False, every value passes through unchanged, and the analyzer then compares
    `trade.direction is Direction.BUY` against the string `'BUY'` -- which is False, so
    every trade silently becomes a sell, no FIFO pairing happens, and the reference
    comes back with `total_trades=0`. The gate's first run showed exactly that, as
    C10 noise on a clean report: three 'the report is stale' findings that were really
    the reference being built out of strings.
    """
    if not isinstance(data, dict):
        raise DashboardError('%s: expected an object, got %s' % (where, type(data).__name__))
    try:
        hints = get_type_hints(cls)
    except NameError as exc:
        raise DashboardError('%s: the annotations of %s cannot be resolved (%s) -- '
                             'an unresolved annotation silently degrades to "leave the '
                             'JSON value alone"' % (where, cls.__name__, exc))
    kwargs = {}
    try:
        for field in dataclasses.fields(cls):
            if field.name not in data:
                raise DashboardError('%s: the report has no %r field -- cannot rebuild a %s'
                                     % (where, field.name, cls.__name__))
            kwargs[field.name] = _convert(hints.get(field.name, field.type), data[field.name])
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        # `_convert` raising ValueError is the interesting one: it means the report
        # carries a value that is not a member of the enum it claims to be (or a
        # timestamp that is not ISO). Left uncaught it escapes as a bare ValueError and
        # the run looks like a crash rather than a verdict.
        raise DashboardError('%s: cannot rebuild a %s from the report (%s: %s)'
                             % (where, cls.__name__, type(exc).__name__, exc))


def analyzer_reference(payload):
    """Re-run `PerformanceAnalyzer` over this report's own inputs.

    Returns the `PerformanceMetrics` the analyzer produces *now* for the trades,
    orders and account snapshots stored in the report. This is the outside reference
    C10 compares the stored metrics against; a board, a hand edit or a stale file all
    end up disagreeing with it, and none of them can be detected by asking the report
    about itself.
    """
    from quanauto.models import AccountSnapshot, Order, Trade
    from quanauto.performance import PerformanceAnalyzer

    deterministic = payload.get('deterministic')
    if not isinstance(deterministic, dict):
        raise DashboardError('the report has no `deterministic` section to re-analyze')

    def segment(name, cls):
        raw = deterministic.get(name)
        if not isinstance(raw, list):
            raise DashboardError('deterministic.%s is missing or not a list -- there is '
                                 'nothing for the analyzer to recompute from' % name)
        return [_rebuild(cls, item, 'deterministic.%s[%d]' % (name, i))
                for i, item in enumerate(raw)]

    trades = segment('trades', Trade)
    orders = segment('orders', Order)
    history = segment('account_history', AccountSnapshot)
    if not history:
        raise DashboardError('deterministic.account_history is empty -- re-running the '
                             'analyzer over it would compare every metric against a '
                             'portfolio that never existed')
    return PerformanceAnalyzer().analyze(trades, orders, history).performance


def stored_metrics(payload):
    """The `performance` mapping as a plain `{field: value}` dict, or None."""
    deterministic = payload.get('deterministic')
    if not isinstance(deterministic, dict):
        return None
    performance = deterministic.get('performance')
    return performance if isinstance(performance, dict) else None


# ---------------------------------------------------------------------------------
# The consistency core. Used by C3 and by both C8 sweeps, so the falsification cannot
# drift away from what the checks actually compare.
# ---------------------------------------------------------------------------------
def metric_reads(payload):
    """Read-out fingerprint: `[(key, value, text)]`. The unit every check compares."""
    from quanauto.dashboard import read_view

    view = read_view(payload)
    return tuple((read.key, read.value, read.text) for read in view.metrics)


CURVE_SEGMENTS = ('equity_curve', 'trades', 'orders', 'account_history')


def bump_curve_segment(payload, name):
    """Change one non-`performance` segment. Returns None when it is not there.

    These are the inputs `performance.py` recomputes everything from -- the equity
    curve feeds every return / risk / drawdown metric, the trade list feeds win rate,
    profit factor and holding period. A board that reads them will move when they move.
    """
    out = copy.deepcopy(payload)
    deterministic = out['deterministic']
    if name == 'equity_curve':
        for point in deterministic['equity_curve']:
            point['equity'] *= 1.5
            point['drawdown'] *= 0.5
        return out
    segment = deterministic.get(name)
    if not isinstance(segment, list) or not segment:
        return None
    segment.append(copy.deepcopy(segment[0]))
    return out


def shift(value):
    """Change a metric's value without changing its type or its unit.

    Type preservation matters: pushing a count to 3.7 makes `read_view` *reject* the
    report (fractional counts are refused rather than truncated), which would make the
    C8 sweep report C1 for that metric instead of the C3 mismatch it is testing for.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 2
    return value + 1.0


def spec_drift_issues(specs):
    """C2: the board's field list and `PerformanceMetrics` must be the same set.

    Compared against the *dataclass*, never against the report: the report is exactly
    what the board is supposed to be reading, so checking it against itself is the
    identity-shaped experiment that tests nothing. Missing a field means the report
    carries a metric the board silently drops (a short report looks healthy); an extra
    field means the board reads a key no report ever writes.

    Both directions are needed. 'Every spec is a real field' alone stays green when the
    board quietly stops showing one metric, and that is the drift this check exists for.
    """
    from quanauto.models import PerformanceMetrics

    if not dataclasses.is_dataclass(PerformanceMetrics):
        return [('GATE', 'PerformanceMetrics is not a dataclass -- C2 has no field list '
                         'to compare against, so its silence would mean nothing')]
    fields = {f.name for f in dataclasses.fields(PerformanceMetrics)}
    keys = {spec.key for spec in specs}
    if not keys:
        return [('GATE', 'METRIC_SPECS is empty -- every comparison below would run '
                         'over an empty set')]
    issues = []
    for name in sorted(fields - keys):
        issues.append(('C2', '%s is a PerformanceMetrics field but no spec shows it -- '
                             'the board silently drops a metric the report carries'
                       % name))
    for name in sorted(keys - fields):
        issues.append(('C2', 'spec %s is not a PerformanceMetrics field -- the board is '
                             'reading a key the report never writes' % name))
    return issues


def run_checks(payload, label='in-process backtest'):
    """The payload detectors: C1, C2, C3, C4, C5, C7, C8, C9, C10."""
    from quanauto.dashboard import (
        METRIC_SPECS,
        format_metric,
        read_view,
        render_html,
        render_text,
    )
    from quanauto.engine import dump_report

    issues = []
    stats = {'metrics': 0, 'trades': None, 'curve_points': 0, 'c8_mutations': 0,
             'c8_caught': 0, 'c8_skipped': 0, 'source': label,
             'analyzer_inputs': None, 'analyzer_fields': 0}

    deterministic = payload.get('deterministic')
    performance = None
    if isinstance(deterministic, dict):
        performance = deterministic.get('performance')
    if not isinstance(performance, dict) or not performance:
        # Vacuity guard. `{}` and 'all 14 metrics happen to be 0' are the same shape;
        # proceeding would compare 0 fields and print a clean PASS.
        issues.append(('GATE', "the report's deterministic.performance section is "
                               "empty or absent (%r) -- every equality check below "
                               "would run over an empty set and report a clean 0"
                       % (performance,)))
        return issues, stats

    try:
        view = read_view(payload)
    except DashboardError as exc:
        issues.append(('C1', 'read_view refused %s: %s' % (label, exc)))
        return issues, stats

    stats['metrics'] = len(view.metrics)
    stats['curve_points'] = len(view.curve)
    stats['trades'] = view.counts.get('trades')

    # --- C1: the extraction produced something to compare ------------------------
    if len(view.metrics) == 0:
        issues.append(('GATE', 'the view carries 0 metric reads -- C3/C8 would iterate '
                               'over nothing and still print PASS'))
        return issues, stats
    if len(view.metrics) != len(METRIC_SPECS):
        issues.append(('C1', 'the view carries %d reads but METRIC_SPECS declares %d '
                             'specs' % (len(view.metrics), len(METRIC_SPECS))))

    # --- C2: the spec list is the dataclass's field list, both directions ---------
    issues.extend(spec_drift_issues(METRIC_SPECS))

    # --- C3: read-out == report, bit for bit ------------------------------------
    for read in view.metrics:
        spec = next(s for s in METRIC_SPECS if s.key == read.key)
        if read.key not in performance:
            issues.append(('C3', 'the report has no %s field but the board shows one'
                           % read.key))
            continue
        if read.value != performance[read.key]:
            issues.append(('C3', '%s: board shows %r, the report says %r -- the board '
                                 'is not showing the report'
                           % (read.key, read.value, performance[read.key])))
        expected = format_metric(spec, performance[read.key])
        if read.text != expected:
            issues.append(('C3', '%s: board renders %r, format_metric yields %r'
                           % (read.key, read.text, expected)))

    # --- C10: the stored metrics are what the analyzer produces from these inputs ---
    # The only check in this file with a source of truth outside the report. C3 (board
    # vs file) and C8a (field moved => read-out moved) both stay perfectly green when
    # the *file* is what was edited -- both sides of their comparison live in the same
    # payload. That is precisely the failure the I4 DoD asks the trigger test to make
    # ('手工改一份 BacktestResult 里的某个指标，确认一致性门禁会红'), so without C10 the gate has
    # no teeth on its own DoD.
    try:
        reference = analyzer_reference(payload)
    except DashboardError as exc:
        reference = None
        # Deliberately GATE, not C10: if the reference cannot be rebuilt, the comparison
        # below would iterate over nothing and report a clean 0 -- the same vacuity
        # failure C1 guards against, one level up.
        issues.append(('GATE', 'the analyzer reference could not be rebuilt from %s '\
                               '(%s) -- the stored-vs-recomputed comparison would '\
                               'then compare nothing and stay silent'
                       % (label, exc)))
    if reference is not None:
        counts = {}
        for name in ('trades', 'orders', 'account_history'):
            value = deterministic.get(name)
            counts[name] = len(value) if isinstance(value, list) else -1
        stats['analyzer_inputs'] = 'trades=%d orders=%d snapshots=%d' % (
            counts['trades'], counts['orders'], counts['account_history'])
        fresh = {f.name: getattr(reference, f.name)
                 for f in dataclasses.fields(reference)}
        for spec in METRIC_SPECS:
            stats['analyzer_fields'] += 1
            if spec.key not in fresh:
                issues.append(('C10', 're-running the analyzer produced no %s -- the '
                                      'reference and the board disagree about which '
                                      'metrics exist' % spec.key))
                continue
            stored = performance.get(spec.key)
            if stored != fresh[spec.key]:
                issues.append(('C10', '%s: the report stores %r but re-running '
                                      'PerformanceAnalyzer over this report\'s own '
                                      'trades/orders/account_history yields %r -- the '
                                      'stored metric is stale, or it was edited by hand'
                               % (spec.key, stored, fresh[spec.key])))

    # --- C4: the sample itself must be able to fail ------------------------------
    if len(view.curve) < 2:
        issues.append(('C4', 'the curve has %d point(s); C7 compares two endpoints and '
                             'has nothing to compare' % len(view.curve)))
    elif len({point.equity for point in view.curve}) < 2:
        issues.append(('C4', 'equity is constant across the whole curve -- a flat sample '
                             'cannot distinguish "agrees" from "never looked"'))
    if not isinstance(stats['trades'], int) or stats['trades'] < 1:
        issues.append(('C4', 'the report carries %r trades; a report with no trades '
                             'still renders, so this gate would be scoring a skeleton'
                       % (stats['trades'],)))

    # --- C5: determinism ----------------------------------------------------------
    text_a = render_text(read_view(payload))
    text_b = render_text(read_view(payload))
    if text_a != text_b:
        issues.append(('C5', 'render_text is not deterministic: two calls on the same '
                             'report differ'))
    html_a = render_html(read_view(payload))
    html_b = render_html(read_view(payload))
    if html_a != html_b:
        issues.append(('C5', 'render_html is not deterministic'))
    try:
        reloaded = json.loads(dump_report(payload))
    except (TypeError, ValueError) as exc:
        issues.append(('C5', 'the report does not survive dump_report -> json.loads '
                             '(%s)' % exc))
        reloaded = None
    if reloaded is not None and render_text(read_view(reloaded)) != text_a:
        issues.append(('C5', 'rendering the round-tripped report differs from rendering '
                             'the in-memory one -- the board depends on a live object, '
                             'not on the file'))

    # --- C7: the identities `performance.py` guarantees --------------------------
    last_equity = view.curve[-1].equity if view.curve else None
    if last_equity is not None and 'final_equity' in performance:
        if last_equity != performance['final_equity']:
            issues.append(('C7', 'final_equity (%r) != the last curve point (%r); '
                                 'performance.py:172 sets one from the other'
                           % (performance['final_equity'], last_equity)))
    if view.curve and 'max_drawdown' in performance:
        curve_max_dd = max(point.drawdown for point in view.curve)
        if curve_max_dd != performance['max_drawdown']:
            issues.append(('C7', 'max_drawdown (%r) != max(drawdown) over the curve '
                                 '(%r); the drawdown column is a positive magnitude, so '
                                 'a min/max mix-up here displays 0.000000 forever'
                           % (performance['max_drawdown'], curve_max_dd)))

    # --- C8a: the report field moved, the read-out must move with it ----------------
    base = metric_reads(payload)
    for spec in METRIC_SPECS:
        mutated = copy.deepcopy(payload)
        original = mutated['deterministic']['performance'][spec.key]
        replacement = shift(original)
        if replacement == original:
            issues.append(('C8', '%s: could not build a mutation (value %r)'
                           % (spec.key, original)))
            continue
        mutated['deterministic']['performance'][spec.key] = replacement
        stats['c8_mutations'] += 1
        try:
            table = {k: (v, t) for k, v, t in metric_reads(mutated)}
        except DashboardError as exc:
            issues.append(('C8', '%s: putting %r into the report made read_view refuse '
                                 'the whole report (%s) instead of showing it'
                           % (spec.key, replacement, exc)))
            continue
        want = (replacement, format_metric(spec, replacement))
        if table.get(spec.key) != want:
            issues.append(('C8', '%s: the report field was set to %r but the board still '
                                 'reads %r -- this field is not covered'
                           % (spec.key, replacement, table.get(spec.key))))
        else:
            stats['c8_caught'] += 1

    # --- C8b: the recomputation inputs moved, the read-out must NOT move -----------
    for name in CURVE_SEGMENTS:
        mutated = bump_curve_segment(payload, name)
        if mutated is None:
            stats['c8_skipped'] += 1
            continue
        stats['c8_mutations'] += 1
        try:
            reads = metric_reads(mutated)
        except DashboardError as exc:
            issues.append(('C8', 'changing %s made read_view refuse the report (%s)'
                           % (name, exc)))
            continue
        if reads != base:
            moved = [k for (k, v, _), (_, w, _) in zip(reads, base) if v != w]
            issues.append(('C8', 'changing %s moved the read-out %s -- the board is '
                                 'deriving metrics from the curve/trades instead of '
                                 'reading them' % (name, moved or '(rendered text only)')))
        else:
            stats['c8_caught'] += 1

    # --- C9: the console can print it -------------------------------------------
    try:
        text_a.encode('gbk')
    except UnicodeEncodeError as exc:
        issues.append(('C9', 'render_text produced a character outside GBK (%s) -- this '
                             'console is cp936 and printing it raises '
                             'UnicodeEncodeError mid-report' % exc))

    return issues, stats


# ---------------------------------------------------------------------------------
# Selftest. One sample class per detector, plus the two vacuity guards, plus a clean
# control. Every sample that mutates a payload is assert-anchored: a missing anchor or
# a no-op replacement makes the sample FAIL rather than quietly run against the
# pristine report -- a trigger test that does not reach the branch under test is
# indistinguishable from a detector that was never written.
# ---------------------------------------------------------------------------------
# Multi-file content marker. A sample whose anchor is missing is *not* a passed sample:
# it never reached the branch it claims to test, and a report full of green samples that
# never ran is exactly what a detector that does not exist looks like. Every skipped
# sample is recorded here and turns the selftest into a FAIL.
ANCHOR_MISSES = []


def mutate(text, old, new, tag):
    if old not in text:
        print('    sample %s: ANCHOR NOT FOUND (%r)' % (tag, old[:60]))
        ANCHOR_MISSES.append(tag)
        return None
    out = text.replace(old, new, 1)
    if out == text:
        print('    sample %s: replacement was a no-op' % tag)
        ANCHOR_MISSES.append(tag)
        return None
    return out


def perturb(payload, key, value, tag):
    """Copy `payload` with performance[key] set to `value`. Returns None if it cannot."""
    out = copy.deepcopy(payload)
    perf = (out.get('deterministic') or {}).get('performance')
    if not isinstance(perf, dict) or key not in perf:
        print('    sample %s: ANCHOR NOT FOUND (performance[%r])' % (tag, key))
        ANCHOR_MISSES.append(tag)
        return None
    if perf[key] == value:
        print('    sample %s: mutation was a no-op' % tag)
        ANCHOR_MISSES.append(tag)
        return None
    perf[key] = value
    return out


def selftest(real_payload, source):
    ok = True
    del ANCHOR_MISSES[:]

    # Imported once, up front: several samples patch these module attributes, and an
    # import placed next to its first use would run *after* the patch that needs it.
    import quanauto.dashboard as dash

    def scenario(tag, payload, code=None, want_clean=False):
        nonlocal ok
        issues, stats = run_checks(payload)
        codes = set(k for k, _ in issues)
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s metrics=%d curve=%d trades=%s c8=%d/%d(skip %d) '
              'c10=%d %s'
              % (tag, len(issues), sorted(codes), stats['metrics'],
                 stats['curve_points'], stats['trades'], stats['c8_caught'],
                 stats['c8_mutations'], stats['c8_skipped'], stats['analyzer_fields'],
                 'OK' if hit else 'MISSED'))
        if not hit:
            for c, m in issues:
                print('      got [%s] %s' % (c, m))
        ok = ok and hit

    def static_scenario(tag, text, code=None, want_clean=False):
        nonlocal ok
        issues, stats = static_checks(text)
        codes = set(k for k, _ in issues)
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s imports=%d %s'
              % (tag, len(issues), sorted(codes), stats['imports'],
                 'OK' if hit else 'MISSED'))
        if not hit:
            for c, m in issues:
                print('      got [%s] %s' % (c, m))
        ok = ok and hit

    if real_payload is None:
        print('  [POSITIVE-clean] SKIPPED (no real report available)')
        ok = False
    else:
        scenario('POSITIVE-clean', real_payload, want_clean=True)

    # C3 + C8: the board recomputes one metric from the equity curve.
    #
    # This is the bug this whole file exists for -- the report says -2.8%, the board
    # derives its own number from the curve. Both C3 (read-out != report) and C8 (the
    # read-out moved when the curve moved) have to fire; the two samples below assert
    # each of them separately. The earlier version of this sample mutated the report's
    # field instead, which turned out to be a no-op experiment: the report and the
    # read-out come from the same dict, so they move together and nothing is tested.
    if real_payload is not None:
        original_read_view = dash.read_view

        def recomputing(payload):
            view = original_read_view(payload)
            curve = [point.equity for point in view.curve]
            factor = curve[-1] / curve[0] - 1.0
            metrics = tuple(
                dataclasses.replace(read, value=factor, text='%.4f%%' % (factor * 100))
                if read.key == 'total_return' else read
                for read in view.metrics
            )
            return dataclasses.replace(view, metrics=metrics)

        if not callable(original_read_view):
            print('    sample C3/C8: ANCHOR NOT FOUND (dashboard.read_view is not '
                  'callable)')
            ok = False
        else:
            try:
                dash.read_view = recomputing
                scenario('C3-recomputing-board', real_payload, 'C3')
                scenario('C8-reads-the-curve', real_payload, 'C8')
            finally:
                dash.read_view = original_read_view

    # C1: a report the board refuses outright, rather than reading a truncated field.
    if real_payload is not None:
        s = perturb(real_payload, 'total_trades', 3.7, 'C1')
        if s:
            scenario('C1-report-rejected', s, 'C1')

    # GATE: the performance section emptied. C3/C7/C8 must not report a clean 0 over
    # an empty set of metrics -- this is the '0 problems, PASS' failure mode.
    if real_payload is not None:
        s = copy.deepcopy(real_payload)
        s['deterministic']['performance'] = {}
        scenario('GATE-empty-performance', s, 'GATE')
        scenario('GATE-no-deterministic-section', {'schema': 'x'}, 'GATE')

    # C2: one spec dropped from the live METRIC_SPECS tuple (a source-level drift, so
    # it is applied to the imported object rather than to the report).
    if real_payload is not None:
        original_specs = dash.METRIC_SPECS
        try:
            dash.METRIC_SPECS = original_specs[:-1]
            if dash.METRIC_SPECS == original_specs:
                print('    sample C2: ANCHOR NOT FOUND (METRIC_SPECS is empty)')
                ok = False
            else:
                scenario('C2-spec-drift', real_payload, 'C2')
        finally:
            dash.METRIC_SPECS = original_specs

    # C10: the I4 DoD's own trigger test ('手工改一份 BacktestResult 里的某个指标，确认一致性
    # 门禁会红'), done in process. The board is untouched and agrees with the file --
    # only the analyzer notices. This is the sample the gate's first version could not
    # have passed, because it had no outside reference to compare against.
    if real_payload is not None:
        s = perturb(real_payload, 'sharpe_ratio', 12345.6789, 'C10-metric-edited')
        if s:
            scenario('C10-metric-edited', s, 'C10')

    # GATE: the outside reference itself cannot be rebuilt. A trade whose `direction` is
    # not a member of the enum is refused while rebuilding the dataclass; if that were
    # swallowed, C10 would iterate over nothing and the DoD's failure would pass.
    if real_payload is not None:
        s = copy.deepcopy(real_payload)
        trades = s.get('deterministic', {}).get('trades')
        if not trades:
            print('    sample GATE-analyzer-reference: ANCHOR NOT FOUND (no trades in '
                  'the real report)')
            ANCHOR_MISSES.append('GATE-analyzer-reference')
        else:
            trades[0]['direction'] = 'sideways'
            scenario('GATE-analyzer-reference-missing', s, 'GATE')

    # C5: nondeterministic rendering, injected the only way it can happen in-process.
    if real_payload is not None:
        calls = {'n': 0}
        original_spark = dash.sparkline

        def flaky(values):
            calls['n'] += 1
            return 'x' * calls['n']

        try:
            dash.sparkline = flaky
            scenario('C5-nondeterministic-render', real_payload, 'C5')
        finally:
            dash.sparkline = original_spark

    if real_payload is not None:
        # C7: break the identity between the curve endpoint and the metric.
        s = copy.deepcopy(real_payload)
        s['deterministic']['equity_curve'][-1]['equity'] += 1.0
        scenario('C7-curve-vs-metric', s, 'C7')

        # C4: a report with no trades and a flat curve is not evidence of anything.
        s = copy.deepcopy(real_payload)
        s['deterministic']['trades'] = []
        flat = s['deterministic']['equity_curve'][0]['equity']
        for point in s['deterministic']['equity_curve']:
            point['equity'] = flat
        scenario('C4-empty-sample', s, 'C4')

    # C6: the analyzer imported into a copy of the real source.
    s = mutate(source, 'from .errors import DashboardError',
               'from .performance import PerformanceAnalyzer\n'
               'from .errors import DashboardError', 'C6-import')
    if s:
        static_scenario('C6-imports-analyzer', s, 'C6')

    # C6: the other branch -- no import, but a second implementation.
    s = mutate(source, 'def curve_stats(view: DashboardView) -> CurveStats:',
               'def _recompute(view):\n'
               '    return statistics.stdev([p.equity for p in view.curve])\n'
               '\n'
               '\n'
               'def curve_stats(view: DashboardView) -> CurveStats:', 'C6-formula')
    if s:
        static_scenario('C6-recompute-helper', s, 'C6')

    # GATE: a file that is not the board at all. Both the vacuity guard (missing
    # definitions) and, in the .gitignore case, a parse failure have to bite.
    static_scenario('GATE-static-not-the-board', '# just a comment\n', 'GATE')
    static_scenario('GATE-static-unparsable', 'def broken(:\n', 'GATE')

    # A skipped sample is a sample that never reached the code it names. Reporting it as
    # green is how a missing detector hides.
    if ANCHOR_MISSES:
        print('  [samples-never-ran] %s' % sorted(ANCHOR_MISSES))
        ok = False

    print('SELFTEST %s' % ('OK: every detector fires, the clean sample stays clean, '
                           'and both vacuity guards bite'
                           if ok else 'FAIL'))
    return 0 if ok else 1


# ---------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------
def build_real_report():
    """A real engine run, with its console summary captured.

    In-process and seed-pinned on purpose: a gate that reads a checked-in report would
    be checking yesterday's artifact, and a gate that shells out to the CLI would
    depend on how `run_backtest` prints.
    """
    from quanauto.cli import build_parser, run_backtest

    args = build_parser().parse_args(['backtest', '--strategy-csv', FIXTURE,
                                      '--slippage-pct', '0', '--seed', '7'])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        payload = run_backtest(args)
    return payload, buffer.getvalue()


def load_report(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main():
    harden_stdout()
    if '--selftest' in sys.argv:
        # No source means no C6 samples at all. Carrying on would print a selftest that
        # looks complete while a whole detector class never ran.
        if not os.path.exists(DASHBOARD_SRC):
            print('SELFTEST FAIL: %s is missing -- the C6 samples cannot run'
                  % DASHBOARD_SRC)
            return 1
        source = read_text(DASHBOARD_SRC)
        payload = None
        try:
            payload, _ = build_real_report()
        except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
            print('  [POSITIVE-clean] SKIPPED (backtest failed: %s)' % exc)
        return selftest(payload, source)

    overrides = [a for a in sys.argv[1:] if a.startswith('--report=')]
    label = 'in-process backtest (seed 7, tests/fixtures/sample_prices.csv)'
    if overrides:
        path = overrides[-1].split('=', 1)[1]
        if not os.path.exists(path):
            print('GATE FAIL: missing input %s' % path)
            return 1
        label = path
        try:
            payload = load_report(path)
        except (OSError, ValueError) as exc:
            print('GATE FAIL: %s is not readable JSON report (%s)' % (path, exc))
            return 1
    else:
        try:
            payload, summary = build_real_report()
        except Exception as exc:  # noqa: BLE001
            print('GATE FAIL: could not produce a report to check (%s)' % exc)
            return 1
        print('engine run: %d line(s) of summary captured from stdout'
              % len(summary.splitlines()))

    issues, stats = run_checks(payload, label)
    print('report: %s' % label)
    print('extracted: metrics=%d curve_points=%d trades=%s c8_falsification=%d/%d '
          '(skipped %d)'
          % (stats['metrics'], stats['curve_points'], stats['trades'],
             stats['c8_caught'], stats['c8_mutations'], stats['c8_skipped']))
    print('analyzer: re-ran PerformanceAnalyzer over %s -> %d metric(s) compared '
          'against the stored ones'
          % (stats['analyzer_inputs'] or 'NOTHING (the reference was refused)',
             stats['analyzer_fields']))
    if not os.path.exists(DASHBOARD_SRC):
        issues.append(('GATE', 'missing %s -- C6 cannot run' % DASHBOARD_SRC))
    else:
        source = read_text(DASHBOARD_SRC)
        static_issues, static_stats = static_checks(source)
        print('static: %s (%d bytes, %d import statements)'
              % (os.path.relpath(DASHBOARD_SRC, ROOT), static_stats['source_bytes'],
                 static_stats['imports']))
        issues.extend(static_issues)

    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: C2 only says the board field names match PerformanceMetrics. It does '
          'not say the metric is computed correctly -- the board faithfully showing a '
          'wrong number passes every check here.')
    print('NOTE: C8a moves one performance field at a time (the read-out must follow on '
          'that key), C8b moves the curve/trades/orders/account_history segments (the '
          'read-out must not move at all). C8b is the one that would catch a board that '
          'quietly recomputes; neither sweep says anything about whether the metric is '
          'computed correctly -- the board faithfully showing a wrong number is green '
          'here.')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
