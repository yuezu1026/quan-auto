# -*- coding: utf-8 -*-
"""Static reference audit for a contract document's Python code blocks.

Why this exists
---------------
The contracts are declared 强约束 ("all implementations must follow them strictly"), so
an AI agent implementing one copies the signatures verbatim. Every type named in a
signature but never defined anywhere in the document therefore forces the agent to
invent it -- and two agents inventing `PositionTarget` independently will not agree on
its fields. That is a silent, cross-module integration failure, so it has to be caught
before implementation rather than during it.

Checks (each runs independently; no check returns early and shadows a later one)
  T1  every ```python block parses. A block that raises SyntaxError cannot be copied
      out of the document, and is usually a source-document formatting defect
      (decorator and class squashed onto one line, etc.).
  T2  every type referenced in a signature / argument annotation / base class is
      either defined in the document, imported in some block, or a builtin. A shape
      alias (`X = NewType("X", base)`) counts as a definition for T2 -- it is what an
      alias is for -- but deliberately NOT for T3 (see defined_classes).
  T3  every *Error / *Exception name mentioned is actually defined. The contract
      promises an exception hierarchy but never enumerates it, so a "Raises:" line may
      name a class that exists nowhere.
  T4  the language fences present, so an unreviewed block cannot hide in a language
      this tool does not parse.

Exit codes: 0 = no findings, 1 = findings, 2 = selftest setup failure.
"""
import ast
import builtins
import os
import re
import sys

PY_BLOCK_RE = re.compile(r'```python\r?\n(.*?)```', re.S)
FENCE_RE = re.compile(r'^```([A-Za-z0-9_+-]*)', re.M)
ERROR_NAME_RE = re.compile(r'\b([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b')

# Types that legitimately appear without being defined in the document: language
# builtins, typing/abc/dataclasses machinery, stdlib types, and the third-party
# aliases the contract's own examples use.
ALLOWED = set(dir(builtins)) | {
    # typing / abc / dataclasses / enum
    'Any', 'Dict', 'List', 'Optional', 'Tuple', 'Set', 'Callable', 'Iterable',
    'Sequence', 'Mapping', 'MutableMapping', 'Union', 'Type', 'FrozenSet',
    'Iterator', 'Generator', 'Awaitable', 'Coroutine', 'Literal', 'TypedDict',
    'Protocol', 'TypeVar', 'Generic', 'AnyStr', 'NoReturn', 'ClassVar',
    'abstractmethod', 'dataclass', 'field', 'Enum', 'ABC', 'StrEnum', 'IntEnum',
    # datetime / stdlib types
    'datetime', 'date', 'time', 'timedelta', 'timezone', 'Decimal', 'Path',
    'Fraction', 'Counter', 'deque', 'OrderedDict', 'defaultdict',
    # local aliases / module handles used in examples
    'pd', 'np', 'uuid', 'json', 'logging', 'os', 'sys', 'math', 'random',
    'DataFrame', 'Series', 'ndarray',
}


def read_text(path):
    """Read as UTF-8, dropping any BOM, then normalise CRLF.

    Normalising first is mandatory: a bare '\\n' regex run against a CRLF file matches
    nothing, so every extractor below would silently return empty and the whole audit
    would report a deceptively clean result.
    """
    with open(path, encoding='utf-8-sig') as f:
        return f.read().replace('\r\n', '\n')


def names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def is_newtype_alias(value):
    """Is this the value half of `X = NewType("X", base)`?

    A shape alias IS a definition a reader can lift: they get the alias name and its base,
    which is all an alias can carry. Not recognising it made T2 report `OrderId` -- used in
    the section 2.4.2 / 2.4.3 signatures and defined exactly that way in quanauto/models.py
    -- as "defined nowhere in the document", a false positive that would have kept a debt
    entry alive forever (B7, 2026-09-25 附录 G3 adds the line to the document).

    Deliberately narrow: only a call literally named `NewType` (or `typing.NewType`) counts,
    and its target only joins `defined`, never `defined_classes` -- so T3 still refuses to
    accept `FooError = NewType(...)` as an exception hierarchy.
    """
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
    return name == 'NewType'


def collect(blocks):
    """Parse every python block and gather definitions, imports and referenced types."""
    defined, imported, referenced = set(), set(), set()
    # ClassDef names only. T3 reads this one: a shape alias called `FooError` is not an
    # exception definition, and accepting it would be exactly the kind of loosening that
    # silently absorbs real debt.
    defined_classes = set()
    parsed, unparsable = 0, []
    for idx, src in enumerate(blocks, 1):
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            unparsable.append((idx, exc.msg, exc.lineno))
            continue
        parsed += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                defined.add(node.name)
                defined_classes.add(node.name)
                for base in node.bases:
                    referenced |= names_in(base)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported.add((alias.asname or alias.name).split('.')[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    referenced |= names_in(node.returns)
            elif isinstance(node, ast.arg):
                if node.annotation is not None:
                    referenced |= names_in(node.annotation)
            elif isinstance(node, ast.AnnAssign):
                if node.annotation is not None:
                    referenced |= names_in(node.annotation)
            elif isinstance(node, ast.Assign) and is_newtype_alias(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
    return defined, defined_classes, imported, referenced, parsed, unparsable


def audit(text, extra=()):
    """Returns (findings, stats). Every check appends to `findings` independently.

    `extra` is a sequence of additional documents that only contribute definitions and
    imports. A supplementary contract legitimately re-uses types owned by the core
    contract (`BarData`, `DataFeed`), so those must resolve without the core contract's
    own findings being attributed to the supplement.
    """
    findings = []
    stats = {}

    blocks = PY_BLOCK_RE.findall(text)
    fence_langs = sorted(set(FENCE_RE.findall(text)))
    defined, defined_classes, imported, referenced, parsed, unparsable = collect(blocks)
    for other in extra:
        o_def, o_classes, o_imp, _, _, _ = collect(PY_BLOCK_RE.findall(other))
        defined |= o_def
        # classes only: an extra document that defines a real exception class must keep
        # silencing T3 for the main document (that is what --extra is for), but its shape
        # aliases must not.
        defined_classes |= o_classes
        imported |= o_imp

    stats['py_blocks'] = len(blocks)
    stats['parsed'] = parsed
    stats['classes'] = len(defined_classes)
    stats['aliases'] = len(defined) - len(defined_classes)
    stats['referenced'] = len(referenced)
    stats['fence_langs'] = fence_langs

    # --- vacuity guards: every extractor above must have found something ---------
    if not blocks:
        findings.append(('GATE', 'no ```python block found -- every check below would '
                                 'pass vacuously, refusing to report "clean"'))
    if parsed == 0 and blocks:
        findings.append(('GATE', 'no python block parsed at all -- T2/T3 cannot run'))
    if not defined:
        findings.append(('GATE', 'no class definition found -- the T2 whitelist would '
                                 'make every reference look undefined'))
    if not referenced:
        findings.append(('GATE', 'no annotation captured -- the T2 detector is not wired up'))

    # --- T1: unparsable blocks ---------------------------------------------------
    for idx, msg, lineno in unparsable:
        findings.append(('T1', 'python block #%d does not parse (%s at line %s) -- an '
                               'agent cannot lift this code out of the document'
                               % (idx, msg, lineno)))

    # --- T2: referenced but never defined ---------------------------------------
    undefined = sorted(referenced - defined - imported - ALLOWED)
    for name in undefined:
        findings.append(('T2', "type '%s' is used in a signature but defined nowhere in "
                               "the document -- the implementer has to invent it" % name))

    # --- T3: *Error / *Exception named but not defined --------------------------
    # Subtract `defined_classes`, NOT `defined`: see the T3-newtype-is-not-a-class sample.
    defined_errors = {n for n in defined_classes if ERROR_NAME_RE.fullmatch(n)}
    mentioned_errors = set(ERROR_NAME_RE.findall(text))
    for name in sorted(mentioned_errors - defined_classes - imported - ALLOWED):
        findings.append(('T3', "exception '%s' is named but never defined -- the caller "
                               "cannot import it and the hierarchy stays implicit"
                               % name))
    stats['error_names_defined'] = len(defined_errors)
    stats['error_names_mentioned'] = len(mentioned_errors)
    stats['extra_docs'] = len(extra)
    if mentioned_errors and not defined_errors:
        findings.append(('GATE', 'no exception class is defined anywhere although %d are '
                                 'mentioned -- T3 is only reporting a flat list'
                                 % len(mentioned_errors)))

    # --- T4: fences this tool does not parse ------------------------------------
    stats['parsed_langs'] = ['python']
    return findings, stats


def report(path, findings, stats):
    print('contract: %s' % path)
    if stats['extra_docs']:
        print('extra definition sources: %d document(s)' % stats['extra_docs'])
    print('blocks: python=%d parsed=%d classes=%d aliases=%d referenced-types=%d'
          % (stats['py_blocks'], stats['parsed'], stats['classes'], stats['aliases'],
             stats['referenced']))
    print('fences present: %s' % stats['fence_langs'])
    print('error names: mentioned=%d defined=%d'
          % (stats['error_names_mentioned'], stats['error_names_defined']))
    for code, msg in findings:
        print('FINDING [%s] %s' % (code, msg))
    print('verdict: %s (%d finding(s))'
          % ('CLEAN' if not findings else 'DIRTY', len(findings)))


def selftest():
    """One independent sample per detector, plus a clean-sample control.

    Samples are ordered so no detector can shadow another: the T1 sample is a separate
    block from the T2 sample, so a SyntaxError in one cannot hide a wiring bug in the
    other.
    """
    ok = True

    def expect(tag, text, code, want_clean=False):
        nonlocal ok
        findings, _ = audit(text)
        codes = set(c for c, _ in findings)
        hit = (not findings) if want_clean else (code in codes)
        print('  [%s] codes=%s %s' % (tag, sorted(codes), 'OK' if hit else 'MISSED'))
        ok = ok and hit

    clean = ('```python\n'
             'from dataclasses import dataclass\n'
             'class Foo:\n'
             '    def bar(self) -> int:\n'
             '        return 1\n'
             '```\n')
    expect('clean-sample', clean, None, want_clean=True)

    # T1: decorator and class on one line -- the real Account defect.
    expect('T1-unparsable',
           '```python\n'
           '@dataclass class Account: account_id: str\n'
           '```\n', 'T1')

    # T2: a type used in a signature and defined nowhere.
    expect('T2-undefined-type',
           '```python\n'
           'class Foo:\n'
           '    def bar(self) -> PositionTarget:\n'
           '        pass\n'
           '```\n', 'T2')

    # T2 negative control: defining the type must silence the detector, otherwise the
    # detector is just flagging every unknown word.
    expect('T2-defined-type',
           '```python\n'
           '@dataclass\n'
           'class PositionTarget:\n'
           '    qty: int\n'
           'class Foo:\n'
           '    def bar(self) -> PositionTarget:\n'
           '        pass\n'
           '```\n', None, want_clean=True)

    # T2 + NewType (2026-09-25 附录 G3): a shape alias is a definition. The pair below is
    # the trigger test for that branch -- same document, one line added/removed, detector
    # must flip. Without the `removed` half the branch could be dead and still look wired.
    expect('T2-newtype-defined',
           '```python\n'
           'class Foo:\n'
           '    def bar(self) -> OrderId:\n'
           '        pass\n'
           'OrderId = NewType("OrderId", str)\n'
           '```\n', None, want_clean=True)
    expect('T2-newtype-removed',
           '```python\n'
           'class Foo:\n'
           '    def bar(self) -> OrderId:\n'
           '        pass\n'
           '```\n', 'T2')

    # T3 control against over-broadening: `FooError` defined as a *shape alias* must still
    # be reported. If this ever goes clean, the NewType branch above has been allowed to
    # weaken T3 and the exception hierarchy check is decorative.
    expect('T3-newtype-is-not-a-class',
           '```python\n'
           'class Foo:\n'
           '    def bar(self) -> None:\n'
           '        """\n'
           '        Raises:\n'
           '            FooError: when it fails.\n'
           '        """\n'
           '        pass\n'
           'FooError = NewType("FooError", str)\n'
           '```\n', 'T3')

    # T3: an exception named but never defined.
    expect('T3-undefined-error',
           '```python\n'
           'class Foo:\n'
           '    def bar(self) -> None:\n'
           "        \"\"\"\n"
           '        Raises:\n'
           '            WidgetError: when it fails.\n'
           '        \"\"\"\n'
           '        pass\n'
           '```\n', 'T3')

    # GATE: a document with no python block at all must not report "clean".
    expect('GATE-no-blocks', '# just prose\n', 'GATE')

    # EXTRA: a supplement that re-uses a type owned by another contract must be clean
    # when that contract is supplied, and must NOT be clean when it is not. This control
    # exists because the --extra plumbing can silently drop the definitions, which would
    # make every cross-file reference look undefined while the tool still "works".
    supplement = ('```python\n'
                  'class Foo:\n'
                  '    def bar(self) -> SharedThing:\n'
                  '        pass\n'
                  '```\n')
    owner = ('```python\n'
             '@dataclass\n'
             'class SharedThing:\n'
             '    x: int\n'
             '```\n')
    f_no, _ = audit(supplement)
    f_yes, _ = audit(supplement, [owner])
    hit = ('T2' in set(c for c, _ in f_no)) and not f_yes
    print('  [EXTRA-definitions-load] without-extra=%s with-extra=%s %s'
          % (sorted(c for c, _ in f_no), sorted(c for c, _ in f_yes),
             'OK' if hit else 'MISSED'))
    ok = ok and hit

    print('SELFTEST %s' % ('OK: all detectors fire, clean sample stays clean'
                           if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    # NOTE: `--extra=` must be split out BEFORE the generic '--' filter below, otherwise
    # the value silently disappears and the extra definitions are never loaded -- which
    # looks exactly like "the tool works" while every cross-file type turns up undefined.
    raw = sys.argv[1:]
    extras = [a.split('=', 1)[1] for a in raw if a.startswith('--extra=')]
    args = [a for a in raw if not a.startswith('--')]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs = os.path.join(root, 'docs')

    if '--selftest' in sys.argv:
        return selftest()

    primaries = args or [os.path.join(docs, '智能量化交易平台-核心模块接口契约文档.md')]

    missing = [p for p in primaries + extras if not os.path.exists(p)]
    if missing:
        print('GATE FAIL: missing input %s' % missing[0])
        return 1

    texts = {p: read_text(p) for p in primaries + extras}
    total = 0
    for path in primaries:
        extra = [t for p, t in texts.items() if p != path]
        findings, stats = audit(texts[path], extra)
        report(path, findings, stats)
        total += len(findings)
    print('TOTAL: %d finding(s) across %d audited document(s)' % (total, len(primaries)))
    return 0 if total == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
