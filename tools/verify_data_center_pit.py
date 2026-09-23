#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data-center-pit -- PIT / `as_of` boundary gate (tier A).

This gate **never runs the code and never runs pytest**. `tests/test_data_center_pit.py`
proves "today's 18 cases pass". This gate proves a different thing: that the structure
cannot *silently lose* the two PIT layers when somebody adds a method next month.

Why a second, structural check is needed at all:

  D3 defines "forgot to pass `as_of_date`" as **impossible** -- the query methods' shape
  has no such parameter, so the mistake cannot be expressed. The enemy of that property
  is not a logic bug, it is *a new method added later*. D3 hands "passed it but did not
  filter" to the second layer (`PITGuard`); the enemy there is a missing
  `record_access` call. Existing pytest cases cannot see either degeneration. Only an
  AST check can.

Detectors (each fires independently; `--selftest` has one MUT sample per detector, one
"extraction is empty" sample, and one clean synthetic sample):

  P1  STRUCT-NO-AS-OF-ON-FEED
      No *query* method of `DataFeed` or a subclass may declare a parameter named
      `as_of_date`. `__init__` is exempt on purpose: the snapshot date MUST be baked in
      when the view is created -- that is the design. What D3 forbids is passing it per
      call, because then the date the caller passes can differ from the date the view
      actually froze, and layer 1 stops being structural.
  P2  SOLE-PRODUCER
      Only a class deriving from `DataCenter` may instantiate a `DataFeed` subclass.
  P3  GUARD-WIRED
      Every method that transitively reads `store.select_*` must also transitively reach
      `record_access` (layer 2 really is wired).
  P4  EXPLICIT-WINDOW-GUARDED
      Every *public* method that declares `datetime`/`start`/`end` must transitively call
      `self._require_visible` (layer 1 really is wired). Private helpers are out of scope
      here: a private helper receives no clock to compare against, so all it can do is
      guard row by row -- that is P3's job. `@abstractmethod` declarations are skipped:
      `DataFeed`'s own signature is the contract's shape, not a read path.
  P5  REJECT-QFQ-IN-BACKTEST (D6)
  P6  REJECT-BFILL-IN-BACKTEST (D7)
  P7  REJECT-GATED-ON-SESSION
      Those two raises must sit behind `session_mode is SessionMode.BACKTEST`. Without
      this detector someone "fixes" a test failure by deleting the branch and the raises
      stay in the AST, looking reassuring, while live view is broken too (D6 explicitly
      allows QFQ for live display).

  Scope of P3/P4: only DataFeed subclasses that are PIT-bounded, i.e. that define
  `_require_visible` or assign `self.as_of_date`. `CsvDataFeed` is therefore NOT covered
  -- it has no `as_of` notion at all (it reads a whole CSV file), so using it in a
  backtest means giving up PIT. That is a pre-existing property of the I1 skeleton, not
  something this gate can fix. The uncovered classes are counted and printed
  (`unbounded_feeds=`), so "we checked everything" can never be assumed by accident.

  P5/P6/P7 deliberately read the *test* of an `if`, never a bare mention anywhere in the
  function. Phase one of this gate did the latter and was fooled by its own error
  messages: `raise FutureDataAccessError("...AdjustType.QFQ...")` matched on the string
  literal inside the message, so deleting the whole `if` branch left P5/P6 green.
  P8  GUARD-RAISES
      `RecordingPITGuard.record_access` must raise. A guard that only records lets a
      leaking backtest run to completion and hand back a report that looks normal --
      the single most dangerous invariant in this module.
  P9  NON-VACUITY
      Every detector above must have examined at least one real target. Extraction that
      comes back empty (a rename, a moved file) would otherwise turn the whole gate into
      a no-op that still prints "0 issue(s) PASS". Zero targets is a FAILURE, not a pass.

Coverage is exactly the files named by SOURCE_RELS -- not "the repository". A new module
that can instantiate a DataFeed has to be registered there. This is deliberate rather than
lazy: a gate that globs the tree cannot tell a reviewer *which* files it looked at, and the
failure mode this constant exists to prevent is precisely "the next module was invisible
while every gate stayed green".

Usage:
    python tools/verify_data_center_pit.py             # check the repository
    python tools/verify_data_center_pit.py --selftest  # prove every detector can go red

Exit codes: 0 = clean, 1 = at least one finding (or a missing input).

Printed runtime text is deliberately ASCII-only: a bare `python tools/verify_*.py` on
this box writes to a cp936 console, where a character outside GBK would raise
UnicodeEncodeError and turn a clean run into a traceback. Chinese lives in the comments.
"""

import ast
import os
import sys

FEED_REL = os.path.join('quanauto', 'datafeed.py')
CENTER_REL = os.path.join('quanauto', 'datacenter.py')

# Modules that must ALSO be parsed even though they are neither the feed nor the centre.
# Why this exists: P2 ("DataCenter.as_of() is the sole legal producer of a DataFeed") is
# only enforced over the files this gate parses, so a brand-new module that instantiates
# DbDataFeed used to be invisible -- every gate stayed green while D3 was bypassed.
# Anything that can reach the feed/store classes belongs in this tuple.
EXTRA_RELS = (os.path.join('quanauto', 'datasources.py'),)
SOURCE_RELS = (FEED_REL, CENTER_REL) + EXTRA_RELS

# `datetime` shadows the type of the same name inside the method body -- that is the
# contract's own spelling (verified character by character by the contract-signature
# gate), so these three names are matched literally, warts and all.
DATE_PARAMS = ('datetime', 'start', 'end')
STORE_READERS = ('select_bars', 'select_symbols')

STAT_KEYS = ('scanned_modules', 'feed_classes', 'pit_feeds', 'unbounded_feeds',
             'feed_methods', 'feed_instantiations', 'store_touchers', 'date_takers',
             'as_of_impls', 'guard_raises')


def read_text(path):
    """UTF-8 (BOM tolerated) then CRLF -> LF.

    Normalising first is mandatory for the same reason as in verify_data_center.py:
    every anchor and regex here assumes bare '\\n', so a CRLF file would silently match
    nothing. This repository stores .py and .md as CRLF (.gitattributes = * -text).
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def parse_module(text, where, issues):
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        issues.append(('P0', '%s does not parse (line %s: %s); every structural check '
                             'below is moot' % (where, exc.lineno, exc.msg)))
        return None


def class_model(trees):
    """{class_name: {'bases': [...], 'methods': {name: FunctionDef}}}.

    The two files are merged into ONE table. `DbDataFeed` lives in datacenter.py but
    inherits `DataFeed` from datafeed.py; modelling them separately would make it look
    like a rootless class and P2/P3/P4 would inspect the wrong target set.
    """
    model = {}
    for tree in trees:
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {}
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[sub.name] = sub
            model[node.name] = {'bases': [_dotted_name(b) for b in node.bases],
                                'methods': methods}
    return model


def _ancestors(model, name):
    seen, stack = set(), list(model.get(name, {}).get('bases', []))
    while stack:
        base = stack.pop()
        if not base or base in seen:
            continue
        seen.add(base)
        stack.extend(model.get(base, {}).get('bases', []))
    return seen


def _derives(model, name, root):
    return name == root or root in _ancestors(model, name)


def _attr_calls(fn):
    """Every `X.y(...)` attribute call name in the function, including
    `self.pit_guard.record_access(...)` and `self.store.select_bars(...)`."""
    return {n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _self_calls(fn):
    """Only `self.X(...)` edges, i.e. sibling dispatch inside the same class.

    `self.store.select_bars(...)` is deliberately excluded: its `func.value` is an
    Attribute, not a Name, so a store read can never be mistaken for a call to a sibling
    method that happens to be named `select_bars`.
    """
    out = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == 'self'):
            out.add(n.func.attr)
    return out


def _raises(fn):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]


def _raised_exception_name(node):
    """The raised exception's class name. `raise Foo("...")` carries a Call as `exc`,
    `raise Foo` carries a Name -- phase one only handled the latter and therefore reported
    every `raise FutureDataAccessError("…")` as "raises something other than
    FutureDataAccessError"."""
    exc = node.exc
    if isinstance(exc, ast.Call):
        return _dotted_name(exc.func)
    return _dotted_name(exc)


def _is_abstract(fn):
    for d in fn.decorator_list:
        if isinstance(d, ast.Name) and d.id == 'abstractmethod':
            return True
        if isinstance(d, ast.Attribute) and d.attr == 'abstractmethod':
            return True
    return False


def _self_attributes(fn):
    """Names assigned as `self.X = ...` inside the function."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == 'self'):
                    out.add(target.attr)
    return out


def _mentions(node, enum_name, member):
    """True when the node references `EnumName.Member` anywhere, test or expression.

    Note what this canNOT see: the characters inside a string literal. `Constant` nodes
    carry no `Attribute` children, so an error message that spells "AdjustType.QFQ" does
    not count as a reference -- which is exactly the property P5/P6 depend on.
    """
    for n in ast.walk(node):
        if (isinstance(n, ast.Attribute) and n.attr == member
                and isinstance(n.value, ast.Name) and n.value.id == enum_name):
            return True
    return False


def _if_tests(fn):
    return [n.test for n in ast.walk(fn) if isinstance(n, ast.If)]


def _raise_guarded_by(fn, enum_name, member):
    """The `raise` nodes sitting in the body of an `if` whose TEST mentions
    `enum_name.member`. String mentions in the raise message do not qualify."""
    hits = []
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and _mentions(n.test, enum_name, member):
            hits.extend(r for stmt in n.body for r in ast.walk(stmt)
                        if isinstance(r, ast.Raise))
    return hits


def _closure(seed, self_calls):
    """Bottom-up propagation: a method is 'X-reachable' when it calls `X` itself, or
    when it calls a sibling that is X-reachable.

    Running to a fixpoint rather than one hop matters: `is_symbol_available` reaches the
    store only through `get_bar`, and that path really is guarded -- so it must count as
    guarded, or the gate would demand a redundant guard and teach people to ignore it.
    """
    reached = set(seed)
    changed = True
    while changed:
        changed = False
        for name, calls in self_calls.items():
            if name not in reached and (calls & reached):
                reached.add(name)
                changed = True
    return reached


class _InstantiationFinder(ast.NodeVisitor):
    """Records (callee_name, enclosing_class_or_None, lineno) for every call."""

    def __init__(self):
        self.stack = []
        self.hits = []

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node):
        self.hits.append((_dotted_name(node.func),
                          self.stack[-1] if self.stack else None,
                          node.lineno))
        self.generic_visit(node)


def run_checks(feed_text, center_text, extra_texts):
    """feed_text, center_text and extra_texts are REQUIRED, never defaulted.

    A defaulted argument would silently turn this into a check that only runs in main():
    every mutation sample would then be diffing the real file against itself and "clean"
    would mean nothing.

    `extra_texts` carries SOURCE_RELS[2:] one-for-one. Defaulting it to () would make
    "which files are scanned" depend on the caller, and a forgotten file is a rule that
    silently stops being enforced -- so the count is compared against SOURCE_RELS and a
    mismatch is a P0 finding, not a shrug.
    """
    issues, stats = [], {}
    texts = [feed_text, center_text] + list(extra_texts)
    if len(texts) != len(SOURCE_RELS):
        issues.append(('P0', 'SOURCE_RELS declares %d file(s) (%s) but %d text(s) were '
                             'supplied -- an unparsed file is a silently unchecked file, '
                             'and this gate would still print "0 issue(s) PASS" over it'
                             % (len(SOURCE_RELS), ', '.join(SOURCE_RELS), len(texts))))
        return issues, stats
    trees = []
    for text, where in zip(texts, SOURCE_RELS):
        tree = parse_module(text, where, issues)
        if tree is not None:
            trees.append(tree)
    if len(trees) != len(SOURCE_RELS):
        return issues, stats
    stats['scanned_modules'] = len(trees)

    model = class_model(trees)
    feed_classes = sorted(c for c in model if _derives(model, c, 'DataFeed'))
    center_classes = sorted(c for c in model if _derives(model, c, 'DataCenter'))
    impl_centers = [c for c in center_classes if c != 'DataCenter']
    stats['feed_classes'] = len(feed_classes)

    # PIT-bounded = carries a clock. `_require_visible` or an `as_of_date` attribute is
    # the structural fingerprint; anything else (CsvDataFeed) has no `as_of` notion to
    # hold, and demanding a guard from it would be demanding a function it cannot have.
    pit_feeds = sorted(
        c for c in feed_classes
        if '_require_visible' in model[c]['methods']
        or any('as_of_date' in _self_attributes(fn)
               for fn in model[c]['methods'].values()))
    stats['pit_feeds'] = len(pit_feeds)
    stats['unbounded_feeds'] = len(feed_classes) - len(pit_feeds)

    # ── P1: the shape must make "forgot as_of_date" inexpressible ─────────────
    feed_methods = 0
    for cls in feed_classes:
        for mname, fn in model[cls]['methods'].items():
            if mname == '__init__':
                # The snapshot date belongs here: it is baked in when the view is made.
                continue
            if _is_abstract(fn):
                # `DataFeed`'s declarations are the contract's shape, not a read path.
                continue
            feed_methods += 1
            args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
            if 'as_of_date' in args:
                issues.append(('P1', '%s.%s declares an as_of_date parameter -- D3 requires '
                                     'the snapshot date to be baked into the view at '
                                     'creation time, not passed per call: a caller-supplied '
                                     'date can disagree with the frozen one, and layer 1 '
                                     'stops being a structural guarantee'
                                     % (cls, mname)))
    stats['feed_methods'] = feed_methods

    # ── P2: DataCenter.as_of() is the only legal entrance ─────────────────────
    finder = _InstantiationFinder()
    for tree in trees:
        finder.visit(tree)
    instantiations = [h for h in finder.hits if h[0] in feed_classes]
    stats['feed_instantiations'] = len(instantiations)
    for name, owner, lineno in instantiations:
        if owner is None or not _derives(model, owner, 'DataCenter'):
            issues.append(('P2', 'line %d instantiates DataFeed subclass %s inside %s -- '
                                 'D3 makes DataCenter.as_of() the sole legal producer of '
                                 'a DataFeed' % (lineno, name, owner or '<module level>')))

    # ── P3 / P4: both layers must be reachable from every read path ───────────
    store_touchers = 0
    date_takers = 0
    for cls in pit_feeds:
        methods = model[cls]['methods']
        calls = {m: _attr_calls(fn) for m, fn in methods.items()}
        edges = {m: _self_calls(fn) for m, fn in methods.items()}
        guarded = _closure({m for m, c in calls.items() if 'record_access' in c}, edges)
        visible = _closure({m for m, c in calls.items() if '_require_visible' in c}, edges)
        readers_direct = {m for m, c in calls.items() if c & set(STORE_READERS)}
        readers = _closure(readers_direct, edges)
        store_touchers += len(readers_direct)

        for mname in sorted(readers - guarded):
            issues.append(('P3', '%s.%s reads the store (%s) yet cannot reach '
                                 'record_access -- layer 2 is not wired: whatever the store '
                                 'over-returns goes straight into the backtest'
                                 % (cls, mname,
                                    ', '.join(sorted(calls[mname] & set(STORE_READERS))))))

        for mname, fn in sorted(methods.items()):
            if mname.startswith('_') or _is_abstract(fn):
                continue  # P3 covers private helpers: they hold no clock to compare.
            args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
            if not (set(args) & set(DATE_PARAMS)):
                continue
            date_takers += 1
            if mname not in visible:
                issues.append(('P4', '%s.%s takes a date but never calls _require_visible -- '
                                     'layer 1 is not wired, so any date the caller names '
                                     'will be served as long as the store has it'
                                     % (cls, mname)))
    stats['store_touchers'] = store_touchers
    stats['date_takers'] = date_takers

    # ── P5 / P6 / P7: D6 and D7 live behind the backtest branch ───────────────
    as_of_impls = 0
    for cls in impl_centers:
        fn = model[cls]['methods'].get('as_of')
        if fn is None:
            continue
        as_of_impls += 1
        for tag, enum_name, member, dref, why in (
                ('P5', 'AdjustType', 'QFQ', 'D6',
                 'forward adjustment uses today\'s latest share count'),
                ('P6', 'FillPolicy', 'BFILL', 'D7',
                 'backward fill writes future values into the past')):
            hits = _raise_guarded_by(fn, enum_name, member)
            if not hits:
                issues.append((tag, '%s.as_of has no if-branch on %s.%s that raises -- %s (%s) '
                                    'must surface as FutureDataAccessError, not as a '
                                    'silently accepted parameter'
                                    % (cls, enum_name, member, dref, why)))
            elif not any(_raised_exception_name(r) == 'FutureDataAccessError' for r in hits):
                issues.append((tag, '%s.as_of rejects %s.%s but raises something other than '
                                    'FutureDataAccessError -- callers catching DATA_002 will '
                                    'misclassify a future-data refusal as a normal error'
                                    % (cls, enum_name, member)))
        if not any(_mentions(t, 'SessionMode', 'BACKTEST') for t in _if_tests(fn)):
            issues.append(('P7', '%s.as_of shows no SessionMode.BACKTEST branch -- the two '
                                 'refusals must be conditional on the session, otherwise a '
                                 'deleted branch leaves both raises in the AST looking '
                                 'reassuring while the live view breaks too (D6 allows QFQ '
                                 'for live display)' % cls))
    stats['as_of_impls'] = as_of_impls

    # ── P8: a guard that only records is not a guard ──────────────────────────
    guard_raises = 0
    guard = model.get('RecordingPITGuard')
    if guard is None:
        issues.append(('P8', 'RecordingPITGuard is missing -- it is the landing point of '
                             'the whole second layer'))
    else:
        fn = guard['methods'].get('record_access')
        if fn is None:
            issues.append(('P8', 'RecordingPITGuard has no record_access method'))
        else:
            guard_raises = len(_raises(fn))
            if not guard_raises:
                issues.append(('P8', 'RecordingPITGuard.record_access never raises -- a guard '
                                     'that only records lets a leaked backtest run to '
                                     'completion and hand back a report that looks normal'))
    stats['guard_raises'] = guard_raises

    # ── P9: zero targets is a failure, not a pass ────────────────────────────
    required = (
        ('scanned_modules', 'source modules actually parsed (must equal SOURCE_RELS)'),
        ('feed_classes', 'DataFeed subclasses'),
        ('pit_feeds', 'PIT-bounded DataFeed subclasses (P3/P4 only cover these)'),
        ('feed_methods', 'DataFeed query methods'),
        ('feed_instantiations', 'DataFeed instantiation sites'),
        ('store_touchers', 'methods that read the store directly'),
        ('date_takers', 'public methods taking a date'),
        ('as_of_impls', 'as_of implementations'),
        ('guard_raises', 'raises inside record_access'),
    )
    for key, what in required:
        if not stats.get(key):
            issues.append(('P9', 'zero targets: %s (%s) extracted as 0 -- that check is '
                                 'spinning in place and cannot count as passing'
                                 % (what, key)))

    return issues, stats


def _mutate(text, old, new, tag):
    """Unique-anchor, self-asserting replacement.

    Returns None when the anchor is absent OR ambiguous OR the replacement is a no-op,
    and the caller must treat that as a failure. A silent no-op is the classic fake gate:
    the "mutation" is byte-identical to the original, the detector stays quiet, and the
    quiet gets read as evidence that the detector works.
    """
    count = text.count(old)
    if count != 1:
        print('    sample %s: ANCHOR %s (%d occurrence(s), need exactly 1) %r'
              % (tag, 'AMBIGUOUS' if count else 'NOT FOUND', count, old[:60]))
        return None
    line = text[:text.index(old)].count('\n') + 1
    out = text.replace(old, new, 1)
    if out == text:
        print('    sample %s: NO-OP (replacement is byte-identical to the anchor)' % tag)
        return None
    print('    applied: %s (line %d)' % (tag, line))
    return out


CLEAN_FEED = """\
class DataFeed(ABC):
    @abstractmethod
    def get_bar(self, symbol: str, datetime: datetime):
        raise NotImplementedError


class DbFeed(DataFeed):
    def __init__(self, store, as_of_date):
        self.store = store
        self.as_of_date = as_of_date
        self.pit_guard = None

    def _require_visible(self, when, where):
        return when

    def _select(self, symbol, start, end):
        out = []
        for row in self.store.select_bars(symbol, start, end):
            self.pit_guard.record_access(row.symbol, row.trade_date, "bar")
            out.append(row)
        return out

    def get_bar(self, symbol: str, datetime: datetime):
        when = self._require_visible(datetime, "get_bar")
        return self._select(symbol, when, when)

    def get_available_symbols(self):
        return [s for s in self.store.select_symbols()
                if self._select(s, MIN_DATE, self.as_of_date)]
"""

CLEAN_CENTER = """\
class DataCenter(ABC):
    @abstractmethod
    def as_of(self, as_of_date, adjust_type=None, fill_policy=None):
        raise NotImplementedError


class Impl(DataCenter):
    def __init__(self, store):
        self.store = store
        self.session_mode = SessionMode.BACKTEST

    def as_of(self, as_of_date, adjust_type=None, fill_policy=None):
        if self.session_mode is SessionMode.BACKTEST:
            if adjust_type is AdjustType.QFQ:
                raise FutureDataAccessError("D6")
            if fill_policy is FillPolicy.BFILL:
                raise FutureDataAccessError("D7")
        return DbFeed(self.store, as_of_date)


class RecordingPITGuard(PITGuard):
    def record_access(self, symbol, trade_date, field):
        if trade_date > self.as_of_date:
            raise FutureDataAccessError("leak")
"""

# A synthetic stand-in for SOURCE_RELS[2:]: real enough to have a class and a method, and
# -- being outside the PIT pair -- it must instantiate no DataFeed. Written with explicit
# \n escapes rather than a multi-line literal because the mutation anchor below has to be
# byte-exact: a physical newline in a literal depends on how the lexer translates CRLF,
# and an anchor that is off by one byte fails silently as "ANCHOR NOT FOUND".
CLEAN_EXTRA = "class SomeAdapter:\n    def fetch(self):\n        return None\n"


def selftest():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(root, rel) for rel in SOURCE_RELS]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        print('SELFTEST FAIL: missing input(s) %s' % ', '.join(missing))
        return 1
    texts = [read_text(p) for p in paths]
    feed, center, extras = texts[0], texts[1], tuple(texts[2:])

    ok = True

    def scenario(tag, feed_text, center_text, code, extras, want_clean=False):
        nonlocal ok
        issues, _ = run_checks(feed_text, center_text, extras)
        codes = sorted(set(k for k, _ in issues))
        hit = (not issues) if want_clean else (code in codes)
        print('  [%s] issues=%d codes=%s %s'
              % (tag, len(issues), codes, 'OK' if hit else 'MISSED'))
        if not hit:
            for c, m in issues:
                print('      %s: %s' % (c, m[:120]))
        ok = ok and hit

    # CONTROL: the real artifacts -- reported, never asserted clean. The gate is allowed
    # to be red on them; that is the entire point of being able to run it.
    issues, stats = run_checks(feed, center, extras)
    print('  [control-real-artifacts] issues=%d codes=%s %s'
          % (len(issues), sorted(set(k for k, _ in issues)),
             ' '.join('%s=%d' % (k, stats.get(k, -1)) for k in STAT_KEYS)))

    # P1: a query method grows an as_of_date parameter. The anchor spans two lines because
    # `DataFeed.get_bar` and `CsvDataFeed.get_bar` carry byte-identical signatures -- a
    # one-line anchor is ambiguous, and _mutate refuses ambiguous anchors instead of
    # silently mutating whichever occurrence `.replace(count=1)` happens to reach.
    bad = _mutate(center,
                  '    def get_market_status(self, datetime: datetime) -> MarketStatus:\n'
                  '        when = self._require_visible(datetime, "DbDataFeed.get_market_status")',
                  '    def get_market_status(self, datetime: datetime,\n'
                  '                          as_of_date=None) -> MarketStatus:\n'
                  '        when = self._require_visible(datetime, '
                  '"DbDataFeed.get_market_status")',
                  'NEG1-as-of-on-query')
    if bad is None:
        ok = False
    else:
        scenario('NEG1-as-of-on-query', feed, bad, 'P1', extras)

    # P2: a DataFeed built outside DataCenter.as_of().
    bad = _mutate(center,
                  '    def data_version(self) -> str:\n        return self._active_version\n',
                  '    def data_version(self) -> str:\n        return self._active_version\n'
                  '\n\n_BYPASS_FEED = DbDataFeed(None, None, "v2026.09.23")\n',
                  'NEG2-bypass-entry')
    if bad is None:
        ok = False
    else:
        scenario('NEG2-bypass-entry', feed, bad, 'P2', extras)

    # P3: layer 2 unwired -- the store is read without a single record_access.
    bad = _mutate(center,
                  '            self.pit_guard.record_access(row.symbol, row.trade_date, '
                  'BAR_FIELD)\n',
                  '            pass  # MUT: store read is no longer guarded\n',
                  'NEG3-guard-unwired')
    if bad is None:
        ok = False
    else:
        scenario('NEG3-guard-unwired', feed, bad, 'P3', extras)

    # P4: layer 1 unwired on one public method.
    bad = _mutate(center,
                  'when = self._require_visible(datetime, "DbDataFeed.get_market_status")',
                  'when = _as_date(datetime)  # MUT: layer 1 dropped',
                  'NEG4-window-unwired')
    if bad is None:
        ok = False
    else:
        scenario('NEG4-window-unwired', feed, bad, 'P4', extras)

    # P5: D6 refusal turns into "just use HFQ".
    bad = _mutate(center,
                  '            if adjust_type is AdjustType.QFQ:',
                  '            if adjust_type is AdjustType.HFQ:  # MUT: D6 gone',
                  'NEG5-qfq-accepted')
    if bad is None:
        ok = False
    else:
        scenario('NEG5-qfq-accepted', feed, bad, 'P5', extras)

    # P6: D7 refusal turns into the default.
    bad = _mutate(center,
                  '            if fill_policy is FillPolicy.BFILL:',
                  '            if fill_policy is FillPolicy.NONE:  # MUT: D7 gone',
                  'NEG6-bfill-accepted')
    if bad is None:
        ok = False
    else:
        scenario('NEG6-bfill-accepted', feed, bad, 'P6', extras)

    # P7: both raises survive, but the branch no longer names the backtest session. This
    # is the "delete the branch to make a test pass" degeneration -- P5/P6 stay quiet
    # because the raise nodes are still in the AST, which is exactly why P7 exists.
    bad = _mutate(center,
                  '        if self.session_mode is SessionMode.BACKTEST:\n',
                  '        if self.session_mode is SessionMode.LIVE:  # MUT: branch moved\n',
                  'NEG7-session-ungated')
    if bad is None:
        ok = False
    else:
        scenario('NEG7-session-ungated', feed, bad, 'P7', extras)

    # P8: the guard records the leak and returns, so the backtest finishes "normally".
    bad = _mutate(center,
                  'raise FutureDataAccessError(\n            "PITGuard',
                  '_recorded_only = (\n            "PITGuard',
                  'NEG8-guard-silent')
    if bad is None:
        ok = False
    else:
        scenario('NEG8-guard-silent', feed, bad, 'P8', extras)

    # P9: the extraction itself comes back empty. Both files must still PARSE (a syntax
    # error would be P0 and would prove nothing about the vacuity guard).
    scenario('NEG9-extraction-empty', 'X = 1\n', 'Y = 2\n', 'P9', ('Z = 3\n',))

    # P2 ACROSS SOURCE_RELS[2:]: a new module instantiating a DataFeed. This sample is the
    # entire reason EXTRA_RELS exists -- without it, "the new module is scanned" would be
    # an assumption, and an unverified assumption is how the hole opened in the first
    # place. The anchor self-asserts: a no-op mutation would make this sample prove
    # nothing while still printing OK.
    bad_extra = _mutate(CLEAN_EXTRA,
                        '        return None\n',
                        '        return None, DbDataFeed(None, None, "v2026.09.23")\n',
                        'NEG10-feed-in-extra-module')
    if bad_extra is None:
        ok = False
    else:
        scenario('NEG10-feed-in-extra-module', feed, center, 'P2', (bad_extra,))

    # P0: the caller forgets a file. This is the guard that keeps EXTRA_RELS honest -- a
    # future refactor that stops passing the extra texts must go red here instead of
    # quietly shrinking the gate back to its pre-fix coverage.
    scenario('NEG11-file-count-mismatch', feed, center, 'P0', ())

    # POSITIVE: a synthetic triple that satisfies all detectors. Without this sample a
    # detector that simply always fires would look perfect in every NEG above.
    scenario('POSITIVE-clean-synthetic', CLEAN_FEED, CLEAN_CENTER, None, (CLEAN_EXTRA,),
             want_clean=True)

    print('SELFTEST %s' % ('OK: every detector fires on its own sample, one vacuity guard, '
                           'one clean sample stays clean' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if '--selftest' in sys.argv:
        return selftest()

    paths = [os.path.join(root, rel) for rel in SOURCE_RELS]
    for p in paths:
        if not os.path.exists(p):
            print('GATE FAIL: missing input %s' % p)
            return 1

    texts = [read_text(p) for p in paths]
    feed, center, extras = texts[0], texts[1], tuple(texts[2:])
    print('CRLF-normalised: ' + ', '.join('%s=%d bytes' % (rel, len(text.encode('utf-8')))
                                          for rel, text in zip(SOURCE_RELS, texts)))

    issues, stats = run_checks(feed, center, extras)
    print('extracted: ' + ' '.join('%s=%d' % (k, stats.get(k, -1)) for k in STAT_KEYS))
    for code, msg in issues:
        print('ISSUE [%s] %s' % (code, msg))
    print('verdict: %s (%d issue(s))' % ('PASS' if not issues else 'FAIL', len(issues)))
    print('NOTE: this gate parses the source, it never executes it. Passing it does NOT '
          'mean the PIT guard rejects at runtime -- that is what tests/test_data_center_pit.py '
          'shows (18 cases, including the I2 trigger test where a store ignores the window '
          'and record_access must refuse). This gate proves the opposite direction: that '
          'the two layers cannot be dropped silently when a method is added later.')
    return 0 if not issues else 1


if __name__ == '__main__':
    sys.exit(main())
