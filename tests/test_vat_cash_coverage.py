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

⚠ Templates and static JS get their own text scanner (bottom half, #485): an
AST sweep cannot read HTML, and until #485 the live constant sat in three
template lines while this file reported clean.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ('inventory_app', 'scripts')
OWNER = os.path.join('inventory_app', 'vat_math.py')

# path -> why this file may carry the constant without calling vat_math.
#
# vat_sub.py was here until 2026-09-10, exempted as "the inverse direction, it
# rounds differently — ปัดขึ้น 2 decimals". That is true of the QUOTATION
# renderer; it was not true of the one call site this exemption covered.
# compute_badge divides, divides again by unit_ratio and compares with `>` —
# there is no rounding anywhere in that path, so the exemption was protecting a
# plain duplicate. It then called vat_math.net_from_cash(); #485 deleted it, as
# the live badge is the JS in templates/vat_sub/product_view.html.
#
# The same dict serves the template/JS sweep below (#485): a path is a path.
ALLOWED = {}


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
    """Line numbers where 1.07 (or, since #485, the rate 0.07) appears as code —
    as a number, or inside a string (SQL text and f-string fragments included).
    Docstrings are excluded."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        v = node.value
        if isinstance(v, float) and v in (1.07, 0.07):
            hits.append(node.lineno)
        elif isinstance(v, str) and ('1.07' in v or _VAT_NUMBER.search(v)):
            hits.append(node.lineno)
    # Percent spellings (net * 7 / 100, price * 100/107) are arithmetic on ints,
    # invisible to the Constant checks above.
    hits += [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.BinOp) and _VAT_PERCENT.search(ast.unparse(node))]
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
        found = (find_vat_constants(src) if rel.endswith('.py')
                 else find_vat_constants_in_front_end(src, is_js=rel.endswith('.js')))
        assert found, f'{rel} no longer carries 1.07 — is ALLOWED stale?'


# ── break-it-once: the sweep must SEE each shape this codebase actually had ──

SHAPES = {
    'plain SQL string':   'X = "CASE WHEN vat_type=2 THEN net*1.07 ELSE net END"\n',
    'spaced SQL string':  'X = "CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END"\n',
    'aliased SQL string': 'X = "CASE WHEN st.vat_type=2 THEN st.net*1.07 ELSE st.net END"\n',
    'f-string fragment':  'a = 1\nX = f"SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END) {a}"\n',
    'bare float':         'cash = net * 1.07\n',
    'conditional float':  'cash = base * (1.07 if vt == 2 else 1.0)\n',
    'triple-quoted SQL':  'X = """\n  SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END)\n"""\n',
    # #485: vat_math.VAT_RATE owns the rate too, so a hand-typed one is a copy.
    'rate as a float':    'vat = net * 0.07\n',
    'rate in SQL':        'X = "SELECT SUM(net) * 0.07 FROM sales_transactions"\n',
    # ... and the percent spellings of both, as arithmetic and in SQL text.
    'percent * 7 / 100':   'vat = net * 7 / 100\n',
    'percent * 107 / 100': 'cash = net * 107 / 100\n',
    'percent / 107 * 100': 'ex = price / 107 * 100\n',
    'percent 100/107':     'ex = price * 100/107\n',
    'percent 7/107':       'vat = price * 7/107\n',
    'percent in SQL':      'X = "SELECT SUM(net) * 7 / 100 FROM sales_transactions"\n',
}


@pytest.mark.parametrize('name,src', sorted(SHAPES.items()))
def test_sweep_detects_every_shape(name, src):
    assert find_vat_constants(src), f'the sweep is blind to: {name}'


@pytest.mark.parametrize('src', [
    '"""A module docstring mentioning net * 1.07 in prose."""\nx = 1\n',
    'def f():\n    """Docstring: multiply by 1.07 here."""\n    return 1\n',
    'a = x * 17 / 100\nb = y * 7 / 1000\nc = z / 107.5 * 100\nd = 1000 / 107\n',
])
def test_sweep_ignores_prose(src):
    """The counter-control. Without this, a sweep that flags everything would
    pass every shape test above and still be useless."""
    assert find_vat_constants(src) == []


# ── templates + static JS (#485) ─────────────────────────────────────────────
#
# No AST here, so "prose" means comments, stripped before matching: Jinja
# {# #}, HTML <!-- -->, and // + /* */ inside <script> (or anywhere in a .js
# file). Visible page text is NOT prose: a label that states the rate renders
# it from vat_math like any other figure. CSS is blanked too — <style> blocks
# and style="..." attributes never carry VAT, and .07 is an ordinary CSS
# number there (rgba(26,26,26,.07) is the app's own shadow). Vendored bundles
# (*.min.js) are not ours to change and are skipped. A constant rendered
# through Jinja ({{ vat_multiplier|tojson }}) has no literal and passes.
#
# Known blind spots, deliberately accepted (parsing JS is not worth it):
#   * `//` preceded by a space inside a JS string reads as a line comment and
#     hides the rest of that line (`//` after a colon, as in http://, does not);
#   * a JS string containing `/*` blanks everything up to the next `*/`, across
#     lines, and one containing `<!--` does the same up to `-->` (the
#     HTML-comment pass also runs over <script> bodies);
#   * a regex literal containing `//` reads as a line comment.

FRONT_END_DIRS = (os.path.join('inventory_app', 'templates'),
                  os.path.join('inventory_app', 'static'))

# The rate in percent: * 7 / 100, * 107 / 100, / 107 * 100, 100/107, 7/107.
_PERCENT = (r'\*\s*(?:107|7)\s*/\s*100(?!\d)'
            r'|/\s*107\s*\*\s*100(?!\d)'
            r'|(?<![\d.])(?:100|7)\s*/\s*107(?!\d)')
_VAT_PERCENT = re.compile(_PERCENT)
# ... or 1.07, 0.07, .07 (trailing zeros allowed) as a whole number: not the
# tail of 21.07 or 10.07, not the head of 1.075.
_VAT_NUMBER = re.compile(r'(?<![\d.])[01]?\.070*(?!\d)|' + _PERCENT)
_JINJA_COMMENT = re.compile(r'\{#.*?#\}', re.S)
_HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
_CSS = re.compile(r'<style\b[^>]*>.*?</style\s*>'
                  r'''|\bstyle\s*=\s*(?:"[^"]*"|'[^']*')''', re.S | re.I)
# One alternation so whichever comment OPENS first wins: `// see /* x` is a
# line comment, `/* a // b */` a block one.
_JS_COMMENT = re.compile(r'/\*.*?\*/|(?<!:)//[^\n]*', re.S)
_SCRIPT = re.compile(r'(<script\b[^>]*>)(.*?)(</script\s*>)', re.S | re.I)


def _blank(m):
    """Spaces for everything but newlines, so every line number after the
    comment stays true."""
    return re.sub(r'[^\n]', ' ', m.group(0))


def find_vat_constants_in_front_end(source, is_js=False):
    """Line numbers where 1.07 / 0.07 (or a percent spelling) appears in a
    template or static .js file as anything but a comment or CSS: JS code, a
    Jinja expression, visible text."""
    if is_js:
        code = _JS_COMMENT.sub(_blank, source)
    else:
        # Jinja first: it strips {# #} at compile time, whatever HTML surrounds it.
        code = _HTML_COMMENT.sub(_blank, _JINJA_COMMENT.sub(_blank, source))
        code = _CSS.sub(_blank, code)
        code = _SCRIPT.sub(
            lambda m: m.group(1) + _JS_COMMENT.sub(_blank, m.group(2)) + m.group(3),
            code)
    return sorted({code.count('\n', 0, m.start()) + 1
                   for m in _VAT_NUMBER.finditer(code)})


def _front_end_files(root=ROOT):
    for d in FRONT_END_DIRS:
        for base, _dirs, files in os.walk(os.path.join(root, d)):
            for fn in files:
                if fn.endswith('.html') or (fn.endswith('.js') and not fn.endswith('.min.js')):
                    yield os.path.relpath(os.path.join(base, fn), root)


def test_no_hand_typed_vat_constant_in_templates_or_static_js():
    scanned = 0
    offenders = {}
    for rel in _front_end_files():
        if rel in ALLOWED:
            continue
        scanned += 1
        src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
        lines = find_vat_constants_in_front_end(src, is_js=rel.endswith('.js'))
        if lines:
            offenders[rel] = lines
    assert scanned > 100, f'control: expected to scan every template, scanned {scanned}'
    assert offenders == {}, (
        'these type the VAT constant by hand — render it from vat_math through '
        'the route ({{ vat_multiplier }} / {{ vat_rate }}), or add an entry to '
        'ALLOWED saying why: ' + repr(offenders))


def test_minified_vendor_bundles_are_not_scanned(tmp_path):
    """A vendored bundle can contain 1.07 for reasons that are not ours; it
    must not fail the sweep. The control proves the same tree IS walked."""
    (tmp_path / 'inventory_app' / 'static' / 'js').mkdir(parents=True)
    (tmp_path / 'inventory_app' / 'templates').mkdir(parents=True)
    (tmp_path / 'inventory_app' / 'static' / 'js' / 'vendor.min.js').write_text('a=b*1.07;')
    (tmp_path / 'inventory_app' / 'static' / 'js' / 'own.js').write_text('a=b*1.07;')
    (tmp_path / 'inventory_app' / 'templates' / 'page.html').write_text('<p>1.07</p>')
    assert sorted(_front_end_files(str(tmp_path))) == [
        os.path.join('inventory_app', 'static', 'js', 'own.js'),
        os.path.join('inventory_app', 'templates', 'page.html'),
    ]


# break-it-once: each shape must be SEEN, on the line it sits on.
FRONT_END_SHAPES = {
    'JS divide':            ('<script>\n  const exVat = p / 1.07;\n</script>\n', False, [2]),
    'JS multiply':          ('<script>\n  const cash = n * 1.07;\n</script>\n', False, [2]),
    'Jinja set, the rate':  ('<tr>\n{% set v = t * 0.07 %}\n</tr>\n', False, [2]),
    'Jinja expression':     ('<td>{{ t * 1.07 }}</td>\n', False, [1]),
    'visible text':         ('<div>\n  ราคาจ่ายจริง ÷ 1.07 (ไม่รวม VAT)\n</div>\n', False, [2]),
    'code before a comment': ('<script>\n  x = p / 1.07; // carve it out\n</script>\n', False, [2]),
    'JS after a URL':       ("<script>\n  f('http://h/' + n * 1.07);\n</script>\n", False, [2]),
    'static .js file':      ('const cash = n * 1.07;\n', True, [1]),
    'line after a comment': ('{# a\n  multi-line note #}\n<p>{{ t * 0.07 }}</p>\n', False, [3]),
    'percent * 7 / 100':    ('{% set v = t * 7 / 100 %}\n', False, [1]),
    'percent * 107 / 100':  ('<script>\n  const cash = n * 107 / 100;\n</script>\n', False, [2]),
    'percent / 107 * 100':  ('<script>\n  const ex = p / 107 * 100;\n</script>\n', False, [2]),
    'percent 100/107':      ('<td>{{ p * 100/107 }}</td>\n', False, [1]),
    'percent 7/107':        ('<p>VAT = ราคา × 7/107</p>\n', False, [1]),
}


def test_front_end_sweep_blanks_css_but_still_sees_js():
    """CSS never carries VAT, and .07 is an ordinary CSS number (the app's own
    shadow is rgba(26,26,26,.07)). A <style> block or style="..." attribute must
    not trip the sweep — or push someone to a whole-file ALLOWED entry — while a
    JS carve-out in the same file is still caught, on its own line."""
    src = ('<style>\n'
           '  .c { box-shadow: 0 1px 2px rgba(26,26,26,.07); letter-spacing: 0.07em; }\n'
           '</style>\n'
           '<div style="opacity: 0.07">x</div>\n'
           "<span style='opacity:.07'>y</span>\n"
           '<script>\n'
           '  x = p / 1.07;\n'
           '</script>\n')
    assert find_vat_constants_in_front_end(src) == [7]


@pytest.mark.parametrize('name', sorted(FRONT_END_SHAPES))
def test_front_end_sweep_detects_every_shape(name):
    src, is_js, lines = FRONT_END_SHAPES[name]
    assert find_vat_constants_in_front_end(src, is_js=is_js) == lines, \
        f'the template sweep is blind to: {name}'


@pytest.mark.parametrize('src,is_js', [
    ('{# divide by 1.07 here #}\n<p>x</p>\n', False),
    ('{# a note\n   over two lines: t * 0.07\n#}\n', False),
    ('<!-- t * 1.07 -->\n<p>x</p>\n', False),
    ('<script>\n  // divide by 1.07\n  x = 1;\n</script>\n', False),
    ('<script>\n  /* x * 1.07\n     and 0.07 */\n  x = 1;\n</script>\n', False),
    ('// divide by 1.07\n/* 0.07 */\nx = 1;\n', True),
    ('<p>ราคาจ่ายจริง ÷ {{ vat_multiplier }}</p>\n', False),
    ('<script>\n  const M = {{ vat_multiplier|tojson }};\n</script>\n', False),
    ('<p>21.07 10.07 1.075 0.0701</p>\n', False),
    ('<style>\n  .c { color: rgba(0,0,0,.07); opacity: 0.07; }\n</style>\n', False),
    ('<div style="opacity: 0.07; letter-spacing: .07em">x</div>\n', False),
    ('<p>* 17 / 100 · 7 / 1000 · 3 / 107 · 1000/107 · / 107.5 * 100</p>\n', False),
])
def test_front_end_sweep_ignores_prose_and_rendered_constants(src, is_js):
    """The counter-control: comments are prose, a constant rendered through
    Jinja has no literal, and other numbers that merely contain the digits
    are not the rate."""
    assert find_vat_constants_in_front_end(src, is_js=is_js) == []
