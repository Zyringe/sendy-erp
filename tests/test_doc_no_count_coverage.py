"""Every `COUNT(DISTINCT … doc_no)` / `GROUP BY … doc_no` in the app's SQL reads a
table where doc_no IS the document, or is listed below with a reason.

Why this exists (#496): `sales_transactions.doc_no` is one LINE of an invoice
(`IV6901304-1`); the invoice is `doc_base`. Readers that counted or grouped by
doc_no and labelled the result เอกสาร/บิล showed /trade-dashboard 263
"documents" for 175 invoices (dev DB, July 2026), and /products/362/trade's
list, capped at 200 lines, dropped 28 of that product's 138 invoices. #493 had
fixed the same bug on the customer page, one reader at a time. Reading code
file by file is how the rest were missed; this sweep does it mechanically.

On `purchase_transactions` the very same SQL is right (doc_no is the document
there), and one file queries both tables (models/sales.py). So each hit is
judged by the table ITS query reads (the FROM/JOIN of the SELECT that holds it,
resolving `alias.doc_no` through that FROM clause), never by the file.

Scope: Python under inventory_app/ (the app). Strings are read through the AST:
plain and triple-quoted SQL, f-strings (a {placeholder} reads as '{}'),
implicit and `+` concatenation, and the receiver of `.format()`. Comments and
docstrings are prose and are skipped.

⚠ What this sweep CANNOT see, so a green run says nothing about them:
  - Python `len()` over line rows. The price resolver's R4 "fewer than 3
    bills" rule (`n_bills = len(_evidence_rows(...))` in price_lookup.py)
    counts evidence LINES in exactly this shape; it is tracked in #512.
  - Jinja `|length` over line rows in a template.
  - JavaScript counting rows in the browser.
  - A line count spelled any other way: COUNT(*) or COUNT(id) on a line
    table, SUM(1), and so on.
  - SQL whose table arrives at run time (`FROM {table}`): such a hit cannot
    be attributed, so it FAILS here until someone states what it reads.
"""
import ast
import functools
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

LINE_TABLE = 'sales_transactions'

# table -> why doc_no on it is the document, so counting/grouping it is right.
DOCUMENT_TABLES = {
    'purchase_transactions':
        'doc_no is the document: the purchase importer stores the bare document '
        'number (HP6900044, no -N suffix) and tells lines apart by line_seq '
        '(mig 091).',
    'paid_invoices':
        'paid_invoices.doc_no is the invoice base (= sales_transactions.doc_base), '
        'one row per (receipt, invoice) link; see the cache contract in CLAUDE.md.',
}

# (file, function) -> why a sales_transactions line count is right there.
# Empty is the goal state: #496 moved every reader to doc_base.
ALLOWED = {}

_SQL_KEYWORDS = {
    'WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'UNION', 'EXCEPT', 'INTERSECT',
    'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'NATURAL', 'ON', 'USING',
    'AS', 'SELECT', 'FROM', 'WINDOW', 'INDEXED', 'NOT',
}
_COUNT = re.compile(r'\bCOUNT\s*\(\s*DISTINCT\s+(?:(\w+)\s*\.\s*)?doc_no\s*\)', re.I)
_GROUP = re.compile(r'\bGROUP\s+BY\b', re.I)
_GROUP_END = re.compile(r'\bHAVING\b|\bORDER\b|\bLIMIT\b|\bUNION\b|\bEXCEPT\b'
                        r'|\bINTERSECT\b|\bWINDOW\b|;', re.I)
_GROUP_COL = re.compile(r'(?:\b(\w+)\s*\.\s*)?\bdoc_no\b', re.I)
_FROM_ITEM = re.compile(r'\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?', re.I)
_STATEMENT_BREAK = re.compile(r'\bUNION\b|\bEXCEPT\b|\bINTERSECT\b|;', re.I)


# ── reading strings out of Python ─────────────────────────────────────────────

def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _render(node):
    """The text of a string-building expression, or None. Anything not known
    at import time (a name, a call, an f-string field) reads as '{}'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant) else '{}' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is not None or right is not None:
            return (left if left is not None else '{}') + (right if right is not None else '{}')
    return None


def _owners(tree):
    """id(node) -> name of the function the node sits in."""
    owner = {}

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[id(child)] = name
            visit(child, name)
    visit(tree, '<module>')
    return owner


def _strings(source):
    """(function, lineno, text) for every string expression that is code."""
    tree = ast.parse(source)
    skip, owner, seen = _docstring_ids(tree), _owners(tree), set()
    for node in ast.walk(tree):            # breadth-first: a `+` chain before its parts
        if id(node) in seen or id(node) in skip:
            continue
        text = _render(node)
        if text is None:
            continue
        seen.update(id(sub) for sub in ast.walk(node))
        yield owner.get(id(node), '<module>'), node.lineno, text


# ── attributing a hit to its query's table ────────────────────────────────────

def _clean(sql):
    """Drop SQL comments and quoted literals: both may hold parentheses."""
    sql = re.sub(r'--[^\n]*', ' ', sql)
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.S)
    return re.sub(r"'[^']*'", "''", sql)


def _depths(sql):
    """Parenthesis depth at each character; a bracket carries its outer depth."""
    depth, out = 0, []
    for ch in sql:
        if ch == ')':
            depth -= 1
        out.append(depth)
        if ch == '(':
            depth += 1
    return out


def _table_for(sql, depths, pos, alias):
    """The table the query holding `pos` reads doc_no from, or None."""
    d = depths[pos]
    start = max((i for i in range(pos) if depths[i] < d), default=-1) + 1
    end = next((i for i in range(pos, len(sql)) if depths[i] < d), len(sql))
    for m in _STATEMENT_BREAK.finditer(sql, start, end):     # one SELECT of a UNION
        if depths[m.start()] == d:
            if m.start() < pos:
                start = m.end()
            else:
                end = m.start()
                break
    items = []                                                 # (table, alias) at this level
    for m in _FROM_ITEM.finditer(sql, start, end):
        if depths[m.start()] == d:
            a = m.group(2) if m.group(2) and m.group(2).upper() not in _SQL_KEYWORDS else None
            items.append((m.group(1).lower(), (a or m.group(1)).lower()))
    if not items:
        return None
    if alias:
        return next((t for t, a in items if a == alias.lower() or t == alias.lower()), None)
    return items[0][0]                                         # bare column: the FROM table


def find_doc_no_hits(source):
    """[(function, lineno, shape, table-or-None, snippet)] for every doc_no count or
    grouping in `source`."""
    hits = []
    for fn, lineno, text in _strings(source):
        sql = _clean(text)
        depths = _depths(sql)
        found = [('COUNT(DISTINCT doc_no)', m.start(), m.group(1)) for m in _COUNT.finditer(sql)]
        for g in _GROUP.finditer(sql):
            # The GROUP BY list runs to the next clause at its own depth, so a
            # key after a function call (strftime(...), s.doc_no) is still read.
            d = depths[g.start()]
            end = next((i for i in range(g.end(), len(sql)) if depths[i] < d), len(sql))
            end = next((m.start() for m in _GROUP_END.finditer(sql, g.end(), end)
                        if depths[m.start()] == d), end)
            for c in _GROUP_COL.finditer(sql, g.end(), end):
                found.append(('GROUP BY doc_no', g.start(), c.group(1)))
        for shape, pos, alias in found:
            snippet = ' '.join(sql[pos:pos + 60].split())
            hits.append((fn, lineno + sql.count('\n', 0, pos), shape,
                         _table_for(sql, depths, pos, alias), snippet))
    return hits


# ── the sweep over the app ────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _app_hits():
    out = []
    for root, _dirs, names in os.walk(APP):
        if any(part in root for part in ('__pycache__', 'instance', 'static')):
            continue
        for n in sorted(names):
            if n.endswith('.py'):
                path = os.path.join(root, n)
                rel = os.path.relpath(path, APP).replace(os.sep, '/')
                src = open(path, encoding='utf-8').read()
                out.extend((rel,) + h for h in find_doc_no_hits(src))
    return tuple(out)


def _py_file_count():
    return sum(n.endswith('.py') for root, _d, names in os.walk(APP)
               if '__pycache__' not in root for n in names)


def test_no_sales_line_counted_as_a_document():
    assert _py_file_count() > 50, 'control: the sweep must walk the whole app'
    bad = []
    for rel, fn, lineno, shape, table, snippet in _app_hits():
        if table in DOCUMENT_TABLES:
            continue
        if table == LINE_TABLE and (rel, fn) in ALLOWED:
            continue
        why = ('counts invoice LINES: use doc_base' if table == LINE_TABLE else
               f'reads {table!r}: add it to DOCUMENT_TABLES saying what doc_no is there'
               if table else 'its table could not be resolved: state it in ALLOWED')
        bad.append(f'{rel}:{lineno} {fn}() {shape} -> {why}\n      {snippet}')
    assert not bad, ('sales_transactions.doc_no is one LINE of an invoice (#496):\n  '
                     + '\n  '.join(bad))


def test_the_sweep_sees_the_purchase_side_it_lets_through():
    """Positive control: the purchase-side document counts are real hits the
    sweep resolves to purchase_transactions. If this finds nothing, the
    scanner is not reading SQL, and the test above is green for nothing."""
    purchase = [(rel, fn) for rel, fn, _l, _s, table, _sn in _app_hits()
                if table == 'purchase_transactions']
    assert len(purchase) >= 7
    assert {rel for rel, _fn in purchase} >= {'models/sales.py', 'models/suppliers.py'}
    assert ('models/sales.py', 'get_trade_dashboard') in purchase


def test_allowlist_entries_still_apply():
    """A stale entry would hide the next real line count as intentional."""
    live = {(rel, fn) for rel, fn, _l, _s, table, _sn in _app_hits() if table == LINE_TABLE}
    assert not set(ALLOWED) - live, f'stale ALLOWED entries: {sorted(set(ALLOWED) - live)}'


@pytest.mark.parametrize('key', sorted(DOCUMENT_TABLES) + sorted(ALLOWED))
def test_every_exemption_carries_a_reason(key):
    reason = DOCUMENT_TABLES.get(key) or ALLOWED.get(key)
    assert reason and len(reason) > 40, f'{key}: say WHY, in a sentence'


# ── break-it-once: the sweep must SEE each shape it claims ────────────────────

SALES_SHAPES = {
    'bare COUNT':
        'Q = "SELECT COUNT(DISTINCT doc_no) AS n FROM sales_transactions WHERE x = 1"\n',
    'aliased COUNT':
        'Q = "SELECT COUNT(DISTINCT st.doc_no) FROM sales_transactions st"\n',
    'GROUP BY bare':
        'Q = "SELECT doc_no, SUM(qty) FROM sales_transactions GROUP BY doc_no"\n',
    'GROUP BY aliased, second key':
        'Q = "SELECT 1 FROM sales_transactions s JOIN products p ON p.id = s.product_id '
        'GROUP BY s.customer, s.doc_no ORDER BY 1"\n',
    'GROUP BY key after a function call':
        'Q = "SELECT 1 FROM sales_transactions s '
        'GROUP BY strftime(\'%Y-%m\', s.date_iso), s.doc_no"\n',
    'multi-line':
        'Q = """\n    SELECT s.unit,\n           COUNT( DISTINCT\n                  s.doc_no ) AS n\n'
        '    FROM sales_transactions s\n    GROUP BY\n        s.unit\n"""\n',
    'multi-line GROUP BY':
        'Q = """\n    SELECT s.doc_no FROM sales_transactions s\n    GROUP BY\n'
        '        s.doc_no\n    LIMIT 200\n"""\n',
    'f-string':
        'w = "1=1"\nQ = f"SELECT COUNT(DISTINCT s.doc_no) FROM sales_transactions s WHERE {w}"\n',
    'f-string, FROM after a field':
        'c = "x"\nQ = f"""SELECT {c}, COUNT(DISTINCT doc_no)\n  FROM sales_transactions\n'
        '  WHERE customer IN ({c})"""\n',
    '.format() receiver':
        'Q = "SELECT COUNT(DISTINCT st.doc_no) FROM sales_transactions st '
        'WHERE st.customer IN ({})".format("?")\n',
    '+ concatenation':
        'Q = "SELECT COUNT(DISTINCT doc_no) " + "FROM sales_transactions"\n',
    'implicit concatenation':
        'Q = ("SELECT COUNT(DISTINCT doc_no) "\n     "FROM sales_transactions")\n',
    'subquery in the select list':
        'Q = "SELECT c.code, (SELECT COUNT(DISTINCT st.doc_no) FROM sales_transactions st '
        'WHERE st.customer = c.name) FROM customers c"\n',
    'the SELECT of a UNION that reads sales':
        'Q = "SELECT COUNT(DISTINCT doc_no) FROM purchase_transactions UNION ALL '
        'SELECT COUNT(DISTINCT doc_no) FROM sales_transactions"\n',
    'SQL comment holding a parenthesis':
        'Q = """SELECT COUNT(DISTINCT doc_no) -- one per line (see #496\n'
        '       FROM sales_transactions"""\n',
}


@pytest.mark.parametrize('shape', sorted(SALES_SHAPES))
def test_the_sweep_flags_every_sales_shape(shape):
    hits = find_doc_no_hits(SALES_SHAPES[shape])
    assert [h[3] for h in hits].count(LINE_TABLE) == 1, \
        f'{shape}: the sweep is blind to this shape: {hits}'


NOT_A_SALES_LINE_COUNT = {
    'purchase table':
        ('Q = "SELECT COUNT(DISTINCT doc_no) FROM purchase_transactions"\n',
         ['purchase_transactions']),
    'one file, both tables (judged per query, not per file)':
        ('S = "SELECT COUNT(DISTINCT doc_base) FROM sales_transactions"\n'
         'P = "SELECT COUNT(DISTINCT doc_no) FROM purchase_transactions"\n',
         ['purchase_transactions']),
    'alias resolves to the purchase side of a join':
        ('Q = "SELECT COUNT(DISTINCT pt.doc_no) FROM purchase_transactions pt '
         'LEFT JOIN sales_transactions st ON st.product_id = pt.product_id"\n',
         ['purchase_transactions']),
    'purchase subquery inside a sales query':
        ('Q = "SELECT (SELECT COUNT(DISTINCT doc_no) FROM purchase_transactions), '
         'COUNT(DISTINCT doc_base) FROM sales_transactions"\n',
         ['purchase_transactions']),
    'paid_invoices grouped while a sales subquery sits in the select list':
        ('Q = f"""SELECT pi.doc_no, (SELECT SUM(net) FROM sales_transactions st2\n'
         '  WHERE st2.doc_base = pi.doc_no) FROM received_payments rp\n'
         '  JOIN paid_invoices pi ON pi.re_id = rp.id GROUP BY pi.doc_no"""\n',
         ['paid_invoices']),
    'the invoice column':
        ('Q = "SELECT COUNT(DISTINCT doc_base) FROM sales_transactions GROUP BY doc_base"\n', []),
    'a longer name ending in doc_no':
        ('Q = "SELECT COUNT(DISTINCT ref_doc_no) FROM sales_transactions"\n', []),
    'docstring prose':
        ('"""We used to COUNT(DISTINCT doc_no) FROM sales_transactions."""\nx = 1\n', []),
    'comment':
        ('# COUNT(DISTINCT doc_no) FROM sales_transactions\nx = 1\n', []),
}


@pytest.mark.parametrize('case', sorted(NOT_A_SALES_LINE_COUNT))
def test_the_sweep_attributes_by_query_not_by_file(case):
    src, tables = NOT_A_SALES_LINE_COUNT[case]
    assert [h[3] for h in find_doc_no_hits(src)] == tables


def test_an_unresolvable_table_is_reported_not_passed():
    hits = find_doc_no_hits('t = "x"\nQ = f"SELECT COUNT(DISTINCT doc_no) FROM {t}"\n')
    assert len(hits) == 1 and hits[0][3] is None
