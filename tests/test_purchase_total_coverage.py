"""Every per-customer purchase total reads ยอดซื้อรวม from ONE definition (#494).

`sales_filters.purchase_net_sql()` is that definition: net before VAT, negated
on a credit-note line. Before #494 the figure was typed out by hand on five
surfaces in two shapes (a bare SUM(net) on four, SUM of the VAT-inclusive cash
on the /call list), and every one of them ADDED returns — prod 56ช001's header
read ฿94,140.00 against the ฿50,460.00 its own document list sums to.

The sweep, per QUERY rather than per file (a file-level allowlist answers "is
this file known", not "is this aggregate declared"): every SQL string in
inventory_app/ that reads sales_transactions and is keyed to a customer (it
names the customer or customer_code column, or it lives in a function whose
name says customer) has its raw net aggregates counted — SUM(net),
SUM(s2.net), SUM(COALESCE(net, 0)), SUM(CASE ... net ...), anything over
{vat_math.cash_sql()}. Each function holding any must be listed in ALLOWED
with that exact count and a reason. A new raw aggregate inside an
already-listed function changes its count, so it cannot hide behind the old
entry.

What it still cannot see: SQL assembled from separate variables (the FROM in
one string, the SUM in another), files outside inventory_app/ (scripts/), and
any figure computed in Python from fetched rows. The cross-surface test in
test_494_purchase_total.py is the behavioural half; this is the census.
"""
import ast
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# Raw net aggregates: the shapes test_revenue_filter_coverage.py sweeps, plus
# two it is blind to (review of #519): an alias with a digit (`s2.net`) and a
# net wrapped before it is summed (`SUM(COALESCE(net, 0))`).
_RAW_NET_AGG = re.compile(
    r'SUM\(\s*(?:(?:(?:COALESCE|ROUND|IFNULL)\(\s*)?(?:\w+\.)?net\b'
    r'|CASE\b.{0,200}?\bnet\b)'
    r'|\{vat_math\.cash_sql\(',
    re.IGNORECASE | re.DOTALL)
_HELPER = re.compile(r'\{sales_filters\.purchase_net_sql\(')
_CUSTOMER_KEY = re.compile(r'\b(?:customer|customer_code)\b', re.IGNORECASE)

# file::function -> (raw net aggregates it is known to hold, why that is right).
# Each is a per-customer money figure that answers a DIFFERENT question from
# ยอดซื้อรวม, so it keeps its own definition on purpose.
ALLOWED = {
    # ── per-document and per-product figures on the customer pages ──
    'models/customers.py::_customer_documents': (1,
        'ยอดรวมเอกสาร: one row per DOCUMENT, VAT added on แยก VAT documents, a '
        'credit note negated in Python. A document total, VAT-inclusive by design.'),
    'models/customers.py::_customer_product_cards': (1,
        'one row per (product, unit) over the price resolver\'s evidence '
        'population (paid invoice lines only), used to order the product cards '
        'by money. A per-product figure, never the customer\'s total.'),
    # ── money owed (AR), not money spent ──
    'blueprints/mobile.py::sales_trip': (1,
        'the sales-trip list\'s outstanding: unpaid invoices per customer, '
        'VAT-inclusive cash. Money the customer owes, not what it bought.'),
    'models/payments.py::get_payment_status': (1,
        'the /ar รายบิล document ledger: one row per bill, VAT-inclusive billed '
        'against paid. An AR balance, not a purchase total.'),
    'models/payments.py::get_ar_reconciliation': (1,
        'the "Ledger (Sendy)" unpaid column of /ar?tab=กระทบยอด, deliberately '
        'unfiltered so it can be held against the Express snapshot.'),
    'models/payments.py::find_payment_candidates': (1,
        'matches an incoming transfer to a customer\'s unpaid bills '
        '(VAT-inclusive cash owed). An AR search, not a purchase total.'),
    'payments_alloc.py::_settlement_rows': (1,
        'per-invoice billed vs collected for AR allocation (VAT-inclusive cash, '
        'credit notes handled by the allocation itself).'),
    # ── one document or one product at a time ──
    'commission.py::get_invoices_for_salesperson': (1,
        'the sales-rep commission tab: one row per INVOICE (IV/HS) with its '
        'customer. A document total, never summed per customer.'),
    'models/pricing_ap.py::get_product_pricing': (1,
        'realised selling price of ONE product per customer (cash ÷ qty). Price '
        'evidence, not a purchase total.'),
    # ── revenue: a different question (#514) ──
    'revenue.py::revenue_summary': (1,
        '/revenue KPIs over revenue_filter(); it names customer only to COUNT '
        'distinct customers. Not per customer.'),
    'revenue.py::unmapped_revenue_drilldown': (2,
        'revenue grouped by BSN code / brand over revenue_filter(); customer is '
        'only counted. Not per customer.'),
    'revenue.py::top_customers_by_revenue': (1,
        '/revenue top customers answers the REVENUE question: revenue_filter() '
        'leaves returns and HS out rather than subtracting them. #494 leaves '
        'revenue alone (HS in revenue is #514).'),
}
# #627 moved four former entries off raw `net` and onto
# sales_filters.sales_net_sql() (the trade screens' ยอดขาย, net of returns):
# customers._customer_sales_aggregates' top_products, call_card._assemble_products,
# and the per-customer queries of models/sales.py's get_product_trade_summary and
# get_trade_dashboard. They are neither raw nor purchase_net_sql() here, so this
# census no longer lists them; test_627_sales_returns_coverage.py pins them.

# The surfaces #494 moved onto the helper, and how many aggregates each holds.
# The call card is not listed: it reads _customer_sales_aggregates through
# models.get_customer_summary.
MUST_USE_HELPER = {
    'models/customers.py::_customer_sales_aggregates': 2,   # header + monthly
    'models/customers.py::get_customers': 1,                # /customers list
    'blueprints/mobile.py::customer_detail': 1,             # /m/customer ยอดสะสม
    'call_card.py::get_call_list': 1,                       # /call spend
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
    """{function: hits of `pattern` in its per-customer sales_transactions queries}"""
    counts = {}
    for func, sql in _queries(src):
        if 'sales_transactions' not in sql:
            continue
        if not (_CUSTOMER_KEY.search(sql) or 'customer' in func.lower()):
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

def test_every_per_customer_net_aggregate_is_the_helper_or_declared():
    found = _app_counts(_RAW_NET_AGG)
    declared = {site: n for site, (n, _why) in ALLOWED.items()}
    undeclared = {s: n for s, n in found.items() if declared.get(s) != n}
    stale = {s: n for s, n in declared.items() if found.get(s) != n}
    assert not undeclared and not stale, (
        'A per-customer net aggregate that is not sales_filters.purchase_net_sql() '
        'must be declared in ALLOWED with its exact count and a reason.\n'
        f'  found but not declared (or count differs): {undeclared}\n'
        f'  declared but not found at that count: {stale}')


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_exemption_carries_a_reason(site):
    assert len(ALLOWED[site][1]) > 40, f'{site}: say WHY it keeps its own definition'


def test_the_purchase_total_surfaces_use_the_helper():
    """Positive control: the census is worthless if it passes while the
    surfaces it exists for quietly stop reading the helper."""
    found = _app_counts(_HELPER)
    assert {s: found.get(s) for s in MUST_USE_HELPER} == MUST_USE_HELPER
    # CONTROL: the helper is found nowhere it was not put, or a sweep that
    # matched every file would still satisfy the line above.
    assert set(found) == set(MUST_USE_HELPER), set(found) - set(MUST_USE_HELPER)


# ── The sweep's own coverage: one rogue source per shape ─────────────────────
#
# A sweep is only worth what its pattern can see, and reading it never shows a
# shape it misses. Every entry is a shape that exists in this app.

def _src(sql, func='report', f_prefix=''):
    return f'def {func}(conn, code):\n    return conn.execute({f_prefix}"""{sql}""", (code,))\n'


AGGREGATE_SHAPES = {
    'bare':             'SELECT SUM(net) FROM sales_transactions WHERE customer_code = ?',
    'aliased':          'SELECT SUM(s.net) FROM sales_transactions s WHERE s.customer_code = ?',
    'coalesced':        'SELECT COALESCE(SUM(net), 0) FROM sales_transactions WHERE customer = ?',
    'rounded':          'SELECT ROUND(SUM(net), 2) FROM sales_transactions WHERE customer = ?',
    'case':             "SELECT SUM(CASE WHEN doc_base LIKE 'SR%' THEN -net ELSE net END) "
                        'FROM sales_transactions WHERE customer_code = ?',
    'case_over_lines':  'SELECT SUM(CASE WHEN vat_type = 2\n THEN net * 1.07\n ELSE net END)\n'
                        '  FROM sales_transactions WHERE customer_code = ?',
    'vat_math_cash':    'SELECT SUM({vat_math.cash_sql()}) FROM sales_transactions '
                        'WHERE customer_code = ?',
    'digit alias':      'SELECT SUM(s2.net) FROM sales_transactions s2 WHERE s2.customer_code = ?',
    'wrapped':          'SELECT SUM(COALESCE(net, 0)) FROM sales_transactions WHERE customer = ?',
}

CUSTOMER_KEY_SHAPES = {
    'one customer by name':  ('SELECT SUM(net) FROM sales_transactions WHERE customer = ?', 'report'),
    'one customer by code':  ('SELECT SUM(net) FROM sales_transactions WHERE customer_code = ?', 'report'),
    'grouped by code':       ('SELECT customer_code, SUM(net) FROM sales_transactions '
                              'GROUP BY customer_code', 'report'),
    'scope from the caller': ('SELECT SUM(net) FROM sales_transactions WHERE {where}',
                              '_customer_totals'),
}

NOT_SITES = {
    'the helper itself':     _src('SELECT SUM({sales_filters.purchase_net_sql()}) '
                                  'FROM sales_transactions WHERE customer_code = ?', f_prefix='f'),
    'not per customer':      _src('SELECT SUM(net) FROM sales_transactions WHERE date_iso >= ?'),
    'another column':        _src('SELECT SUM(qty) FROM sales_transactions WHERE customer = ?'),
    'another table':         _src('SELECT SUM(net) FROM purchase_transactions WHERE supplier = ?'),
    'prose in a docstring':  ('def customer_note():\n'
                              '    """Was SUM(net) FROM sales_transactions WHERE customer = ?"""\n'
                              '    return 1\n'),
}


@pytest.mark.parametrize('shape', sorted(AGGREGATE_SHAPES))
def test_the_sweep_sees_every_aggregate_shape(shape):
    src = _src(AGGREGATE_SHAPES[shape], f_prefix='f' if '{' in AGGREGATE_SHAPES[shape] else '')
    assert _per_function(src, _RAW_NET_AGG) == {'report': 1}, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(CUSTOMER_KEY_SHAPES))
def test_the_sweep_sees_every_customer_key_shape(shape):
    sql, func = CUSTOMER_KEY_SHAPES[shape]
    src = _src(sql, func=func, f_prefix='f' if '{' in sql else '')
    assert _per_function(src, _RAW_NET_AGG) == {func: 1}, f'{shape}: unswept'


@pytest.mark.parametrize('shape', ['implicit concatenation', 'plus concatenation',
                                   'module constant', 'method'])
def test_the_sweep_sees_every_string_shape(shape):
    src = {
        'implicit concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) "\n'
            '        "FROM sales_transactions WHERE customer = ?")\n',
        'plus concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) " +\n'
            '        "FROM sales_transactions WHERE customer = ?")\n',
        'module constant':
            'Q = "SELECT SUM(net) FROM sales_transactions WHERE customer = ?"\n',
        'method':
            'class Repo:\n    def report(self, conn):\n'
            '        return conn.execute("SELECT SUM(net) FROM sales_transactions '
            'WHERE customer = ?")\n',
    }[shape]
    assert sum(_per_function(src, _RAW_NET_AGG).values()) == 1, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_raw_purchase_total(shape):
    assert _per_function(NOT_SITES[shape], _RAW_NET_AGG) == {}, f'{shape}: false positive'
