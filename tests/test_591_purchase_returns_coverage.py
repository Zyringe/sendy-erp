"""Every raw SUM(net)/SUM(qty) over `purchase_transactions` reads through
`sales_filters.supplier_net_sql()` / `supplier_qty_sql()` (#591) — never a
bare column.

The bug: GR (ใบลดหนี้ซื้อ, purchase return/credit note) rows are stored with
POSITIVE net/qty by the DBF importer, same convention as SR on the sales
side, and none of the seven surfaces filtered or negated them. A raw
SUM(net) therefore ADDED returns onto purchases — measured on prod: 2026
Jan-Sep read ฿699,280.59 raw vs ฿693,428.57 net of returns, and
/supplier/ไพบูลย์'s header read ฿462,473 against a true ฿426,473.

The census is per QUERY, not per file — the sibling test for #494
(test_purchase_total_coverage.py) explains why: a file-level allowlist
answers "is this file known", not "is this specific aggregate declared".
Every SQL string in inventory_app/ that reads `purchase_transactions` has
its raw net/qty aggregates counted. Each function holding any must appear
in MUST_USE_HELPER with that exact count — a new raw aggregate inside an
already-listed function changes its count, so it cannot hide behind the old
entry. There is no exemption list: unlike #494's per-customer census (which
keeps AR/ledger/ranking questions on their own raw definitions on purpose),
every purchase_transactions aggregate found here answers the SAME question
(ยอดซื้อ) and money that is display-only, so all of them take the helper.

What it still cannot see: SQL assembled from separate variables, files
outside inventory_app/ (scripts/), and any figure computed in Python from
fetched rows. `bsn_suggest.py::_latest_purchase` and its matching window in
blueprints/bsn.py exclude GR entirely instead (a "latest purchase" is a
single ROW, not a sum) — that exclusion is pinned by
test_591_latest_purchase_excludes_gr.py, the behavioural half.
"""
import ast
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# Raw net/qty aggregates: same shapes test_purchase_total_coverage.py sweeps
# for sales_transactions (bare, aliased, coalesced, rounded, CASE, a digit
# alias, a pre-wrapped column), applied to BOTH `net` and `qty`.
_RAW_AGG = re.compile(
    r'SUM\(\s*(?:(?:(?:COALESCE|ROUND|IFNULL)\(\s*)?(?:\w+\.)?(?:net|qty)\b'
    r'|CASE\b.{0,200}?\b(?:net|qty)\b)',
    re.IGNORECASE | re.DOTALL)
_HELPER = re.compile(r'\{sales_filters\.supplier_(?:net|qty)_sql\(')

# file::function -> exact count of raw aggregates it must hold via the helper.
# Every entry here is a purchase_transactions surface #591 moved onto
# supplier_net_sql()/supplier_qty_sql(); there is no separate ALLOWED list
# because nothing in this sweep answers a different question (contrast
# test_purchase_total_coverage.py, whose census has real exemptions: AR
# balances, per-document VAT-inclusive cash, WACC/ledger).
MUST_USE_HELPER = {
    'models/sales.py::get_trade_dashboard': 4,        # p (net+qty) + weekly_pur + top_suppliers
    'models/sales.py::get_purchases_summary': 1,
    'models/sales.py::get_purchases_summary_by_vat': 2,
    'models/suppliers.py::get_suppliers': 1,
    'models/suppliers.py::get_supplier_summary': 7,   # summary(2) + top_products(2) + monthly(1) + docs(2)
}


def _render(node):
    """Source text of a string expression, f-string holes kept as {expr}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant)
                       else '{' + ast.unparse(v.value) + '}' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _queries(src):
    """(function qualname, text) for every string VALUE in `src`. A docstring
    or any other bare string statement is prose, never a query."""
    out = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
            elif isinstance(child, ast.Expr) and _render(child.value) is not None:
                continue
            elif _render(child) is not None:
                out.append(('.'.join(scope) or '<module>', _render(child)))
            else:
                visit(child, scope)

    visit(ast.parse(src), [])
    return out


def _per_function(src, pattern):
    """{function: hits of `pattern` in its purchase_transactions queries}"""
    counts = {}
    for func, sql in _queries(src):
        if 'purchase_transactions' not in sql:
            continue
        n = len(pattern.findall(sql))
        if n:
            counts[func] = counts.get(func, 0) + n
    return counts


def _py_files():
    for root, _dirs, names in os.walk(APP):
        if any(part in root for part in ('__pycache__', 'instance', 'static')):
            continue
        for n in names:
            if n.endswith('.py'):
                path = os.path.join(root, n)
                yield os.path.relpath(path, APP).replace(os.sep, '/'), path


def _app_counts(pattern):
    out = {}
    for rel, path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        for func, n in _per_function(src, pattern).items():
            out[f'{rel}::{func}'] = n
    return out


# ── The census ───────────────────────────────────────────────────────────────

def test_every_purchase_transactions_aggregate_is_the_helper():
    """No raw SUM(net)/SUM(qty) over purchase_transactions may exist anywhere
    in inventory_app/ — every one must go through the helper instead."""
    found = _app_counts(_RAW_AGG)
    assert not found, (
        'A raw net/qty aggregate over purchase_transactions must use '
        'sales_filters.supplier_net_sql()/supplier_qty_sql() instead:\n'
        f'  {found}')


def test_the_purchase_surfaces_use_the_helper_at_the_expected_count():
    """Positive control: the census above is worthless if it passes only
    because the helper quietly stopped being called somewhere."""
    found = _app_counts(_HELPER)
    assert found == MUST_USE_HELPER, (
        f'expected {MUST_USE_HELPER}, found {found}')


# ── WHICH helper, not just how many (#626 review) ─────────────────────────────
#
# MUST_USE_HELPER above pins a per-function COUNT of "the helper" (either
# kind, added together). A reviewer proved that is not "which": repointing
# get_trade_dashboard's weekly_pur and top_suppliers from supplier_net_sql()
# to supplier_qty_sql() leaves the function's total helper-call count at 4
# either way, so the count-only census stays green while every purchase
# figure on the trade dashboard silently becomes a unit count. Same failure
# shape as #554 ("counts presence, not which") — this repo has been bitten
# by it twice now.
#
# The fix pins the helper to the SQL column alias it feeds: every
# supplier_net_sql() call must feed an alias containing "net" (total_net, or
# weekly_pur's own bare `net`), and every supplier_qty_sql() call must feed
# an alias containing "qty". That is the actual contract these two functions
# exist to keep — a "net" figure must be money, a "qty" figure must be a
# count — so pinning the alias pins the intent, not just the call count.
_HELPER_CALL = re.compile(
    r"\{sales_filters\.supplier_(net|qty)_sql\([^)]*\)\}[\s\d,\)]*\bAS\s+(\w+)")


def _helper_alias_pairs(src):
    """(function, helper_kind, alias) for every supplier_net_sql()/
    supplier_qty_sql() call over purchase_transactions whose result feeds a
    SELECT column alias."""
    out = []
    for func, sql in _queries(src):
        if 'purchase_transactions' not in sql:
            continue
        for kind, alias in _HELPER_CALL.findall(sql):
            out.append((func, kind, alias))
    return out


def _mismatched_helper_aliases():
    """{'file::function: supplier_<kind>_sql() feeds alias <alias>'} for every
    call whose alias does not name its own kind."""
    mismatches = []
    for rel, path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        for func, kind, alias in _helper_alias_pairs(src):
            if kind not in alias.lower():
                mismatches.append(f'{rel}::{func}: supplier_{kind}_sql() feeds alias {alias!r}')
    return mismatches


def test_each_helper_call_feeds_an_alias_of_its_own_kind():
    mismatches = _mismatched_helper_aliases()
    assert not mismatches, (
        'a helper call feeds a column alias of the WRONG kind (a money '
        "helper fed to a qty column, or vice versa):\n" + '\n'.join(mismatches))


def test_the_alias_check_is_not_vacuous_on_a_synthetic_mismatch():
    """CONTROL: prove _mismatched_helper_aliases() can actually fire, on a
    synthetic source shaped exactly like the reviewer's repoint (net_sql
    feeding a qty-named alias)."""
    src = _src(
        "SELECT SUM({sales_filters.supplier_qty_sql()}) AS total_net "
        "FROM purchase_transactions WHERE supplier = ?", f_prefix='f')
    found = []
    for func, kind, alias in _helper_alias_pairs(src):
        if kind not in alias.lower():
            found.append((func, kind, alias))
    assert found == [('report', 'qty', 'total_net')], found


# ── The sweep's own coverage: one rogue source per shape ─────────────────────
#
# A sweep is only worth what its pattern can see. Every entry is a shape that
# existed in this app before #591 (see models/sales.py + models/suppliers.py
# git history for the pre-fix originals).

def _src(sql, func='report', f_prefix=''):
    return f'def {func}(conn, supplier):\n    return conn.execute({f_prefix}"""{sql}""", (supplier,))\n'


AGGREGATE_SHAPES = {
    'bare_net':        'SELECT SUM(net) FROM purchase_transactions WHERE supplier = ?',
    'bare_qty':        'SELECT SUM(qty) FROM purchase_transactions WHERE supplier = ?',
    'aliased':         'SELECT SUM(pt.net) FROM purchase_transactions pt WHERE pt.supplier = ?',
    'coalesced':       'SELECT COALESCE(SUM(net), 0) FROM purchase_transactions WHERE supplier = ?',
    'rounded':         'SELECT ROUND(SUM(net), 2) FROM purchase_transactions WHERE supplier = ?',
    'case':            "SELECT SUM(CASE WHEN doc_base LIKE 'GR%' THEN -net ELSE net END) "
                        'FROM purchase_transactions WHERE supplier = ?',
    'digit_alias':     'SELECT SUM(p2.net) FROM purchase_transactions p2 WHERE p2.supplier = ?',
    'wrapped':         'SELECT SUM(COALESCE(net, 0)) FROM purchase_transactions WHERE supplier = ?',
    'both_columns':    'SELECT SUM(qty), SUM(net) FROM purchase_transactions WHERE supplier = ?',
}

NOT_SITES = {
    'the helper itself':    _src('SELECT SUM({sales_filters.supplier_net_sql()}) '
                                 'FROM purchase_transactions WHERE supplier = ?', f_prefix='f'),
    'both helpers':         _src('SELECT SUM({sales_filters.supplier_net_sql()}), '
                                 'SUM({sales_filters.supplier_qty_sql()}) '
                                 'FROM purchase_transactions WHERE supplier = ?', f_prefix='f'),
    'another table':        _src('SELECT SUM(net) FROM sales_transactions WHERE customer = ?'),
    'no money column':      _src('SELECT COUNT(*), MAX(date_iso) FROM purchase_transactions '
                                 'WHERE supplier = ?'),
    'prose in a docstring': ('def note():\n'
                             '    """Was SUM(net) FROM purchase_transactions WHERE supplier = ?"""\n'
                             '    return 1\n'),
}


@pytest.mark.parametrize('shape', sorted(AGGREGATE_SHAPES))
def test_the_sweep_sees_every_aggregate_shape(shape):
    src = _src(AGGREGATE_SHAPES[shape])
    n = sum(_per_function(src, _RAW_AGG).values())
    expected = 2 if shape == 'both_columns' else 1
    assert n == expected, f'{shape}: unswept (got {n})'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_raw_purchase_aggregate(shape):
    assert _per_function(NOT_SITES[shape], _RAW_AGG) == {}, f'{shape}: false positive'


@pytest.mark.parametrize('shape', ['implicit concatenation', 'plus concatenation', 'method'])
def test_the_sweep_sees_every_string_shape(shape):
    src = {
        'implicit concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) "\n'
            '        "FROM purchase_transactions WHERE supplier = ?")\n',
        'plus concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) " +\n'
            '        "FROM purchase_transactions WHERE supplier = ?")\n',
        'method':
            'class Repo:\n    def report(self, conn):\n'
            '        return conn.execute("SELECT SUM(net) FROM purchase_transactions '
            'WHERE supplier = ?")\n',
    }[shape]
    assert sum(_per_function(src, _RAW_AGG).values()) == 1, f'{shape}: unswept'
