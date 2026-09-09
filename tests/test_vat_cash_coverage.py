"""Every place that converts `net` to cash either calls vat_math or is listed here.

Why this exists: the net→cash rule was the most-repeated money rule in the app —
23 hand-typed call sites in four spellings — and its own test
(`tests/test_vat_math.py`) re-typed the SQL instead of importing it, so deleting
`* 1.07` from a production query left the suite green. Reading code file-by-file
is what let four spellings accumulate; this does the sweep mechanically.

The rule getting inverted once already produced ~฿446k of customer credit that
did not exist (see inventory_app/vat_math.py).

⚠ This sweep scans STRINGS as well as numbers, because most of the call sites
were SQL text. It skips comments and docstrings — prose may discuss the rule.
The break-it-once block at the bottom proves it can see each shape that actually
occurred in this codebase; a sweep that cannot fail is what we are replacing.

The allowlist is the record of DELIBERATE exceptions. Adding an entry is fine —
silently leaving a conversion unguarded is not.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ('inventory_app', 'scripts')
OWNER = os.path.join('inventory_app', 'vat_math.py')

# path -> why this file may carry the constant without calling vat_math.
ALLOWED = {
    os.path.join('inventory_app', 'models', 'vat_sub.py'):
        'The INVERSE direction: price ÷ 1.07 carves VAT out of a VAT-inclusive '
        'price for the VAT sub-book. It rounds differently (ปัดขึ้น 2 decimals, '
        'to match how Express stores a line — see .claude/rules/'
        'quoting-and-pricing.md), so it is a different rule that happens to '
        'share the constant. Folding it into vat_math would apply the wrong '
        'rounding to one of the two.',
}


def _docstring_nodes(tree):
    """Constant nodes that are docstrings — prose, not code."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, 'body', None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def find_vat_constants(source):
    """Line numbers where 1.07 appears as code — as a number, or inside a string
    (SQL text and f-string fragments included). Docstrings are excluded."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        v = node.value
        if isinstance(v, float) and v == 1.07:
            hits.append(node.lineno)
        elif isinstance(v, str) and '1.07' in v:
            hits.append(node.lineno)
    return sorted(set(hits))


def _py_files():
    for d in SCAN_DIRS:
        for base, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for fn in files:
                if fn.endswith('.py'):
                    yield os.path.relpath(os.path.join(base, fn), ROOT)


def test_no_hand_typed_vat_conversion_outside_the_owner():
    scanned = 0
    offenders = {}
    for rel in _py_files():
        if rel == OWNER or rel in ALLOWED:
            continue
        scanned += 1
        src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
        lines = find_vat_constants(src)
        if lines:
            offenders[rel] = lines
    assert scanned > 50, f'control: expected to scan the app, scanned {scanned}'
    assert offenders == {}, (
        'these convert net to cash by hand — call vat_math.cash_sql() / '
        'cash_from_net(), or add an entry to ALLOWED saying why: ' + repr(offenders))


def test_the_owner_and_every_allowlisted_file_really_do_carry_it():
    """Anti-vacuity: if these stop containing the constant, the sweep above is
    guarding nothing and this test says so instead of going quietly green."""
    for rel in [OWNER] + sorted(ALLOWED):
        src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
        assert find_vat_constants(src), f'{rel} no longer carries 1.07 — is ALLOWED stale?'


# ── break-it-once: the sweep must SEE each shape this codebase actually had ──

SHAPES = {
    'plain SQL string':   'X = "CASE WHEN vat_type=2 THEN net*1.07 ELSE net END"\n',
    'spaced SQL string':  'X = "CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END"\n',
    'aliased SQL string': 'X = "CASE WHEN st.vat_type=2 THEN st.net*1.07 ELSE st.net END"\n',
    'f-string fragment':  'a = 1\nX = f"SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END) {a}"\n',
    'bare float':         'cash = net * 1.07\n',
    'conditional float':  'cash = base * (1.07 if vt == 2 else 1.0)\n',
    'triple-quoted SQL':  'X = """\n  SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END)\n"""\n',
}


@pytest.mark.parametrize('name,src', sorted(SHAPES.items()))
def test_sweep_detects_every_shape(name, src):
    assert find_vat_constants(src), f'the sweep is blind to: {name}'


@pytest.mark.parametrize('src', [
    '"""A module docstring mentioning net * 1.07 in prose."""\nx = 1\n',
    'def f():\n    """Docstring: multiply by 1.07 here."""\n    return 1\n',
])
def test_sweep_ignores_prose(src):
    """The counter-control. Without this, a sweep that flags everything would
    pass every shape test above and still be useless."""
    assert find_vat_constants(src) == []
