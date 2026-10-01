"""Card C, P5: every customer-history figure comes from `purchase_history`.

Four census tests (#494 test_purchase_total_coverage, #513
test_last_purchase_population_coverage, #627 test_627_sales_returns_coverage, plus
the ซื้อ half of #554) each carried a list of the customer surfaces and the exact
aggregate each one still held. Card C moved those surfaces onto
`inventory_app/purchase_history.py`; this is the ONE check that replaces their
customer-surface halves. They keep guarding what is not a customer's history
(payments, revenue, the trade screens, price evidence).

What it asserts, per `file::function`:

1. A function outside the module that holds a customer-keyed `sales_transactions`
   query of a HISTORY SHAPE (a last date, a document count, a money or quantity
   sum, a purchase-population call) is listed in DECLARED with those shapes and a
   reason. Nothing else may hold one. The module itself is exempt: it is the owner.
2. Each surface in SURFACES_MUST_CALL calls its `purchase_history` entry point AND
   holds no shape of its own (N4 of the P2 review: calling the module is not
   enough, a surface could call `histories()` and still re-add its own
   `SUM(purchase_net_sql)` with a bill-name filter).
3. The module's own functions still use the shared helpers they are pinned to (presence only).
   The module's internals are otherwise guarded by behaviour tests (test_purchase_history*,
   the differentials) and test_554, not by counts here: the old per-function counts are gone.

A query is "customer-keyed" when it names `customer` / `customer_code` /
`customer_key_sql(...)` or sits in a function whose name says customer. The key
expression is in that list on purpose: a `MAX(date_iso)` keyed only through
`customer_key_sql` was invisible to the old censuses (#699 review W5).

What it cannot see: SQL assembled from separate variables, a figure computed in
Python from fetched rows, and a table name that arrives at run time.
"""
import ast
import os
import re

import pytest

from tests import _census

MODULE = 'purchase_history.py'

_KEYED = re.compile(r'\b(?:customer|customer_code)\b|customer_key_sql', re.IGNORECASE)

# shape -> pattern, applied to the rendered SQL of one query.
SHAPES = {
    'last date':   re.compile(r'M(?:AX|IN)\(\s*(?:CASE\b[^()]{0,200}?\bTHEN\s+)?(?:\w+\.)?date_iso\b',
                          re.IGNORECASE),
    'doc count':   re.compile(r'COUNT\(\s*DISTINCT\s*\(?\s*(?:\w+\.)?doc_base\s*\)'
                              r'|COUNT\(\s*DISTINCT\s+CASE\b[^()]{0,200}?\bdoc_base\s+END\s*\)',
                              re.IGNORECASE),
    'raw money':   re.compile(r'SUM\(\s*(?:(?:(?:COALESCE|ROUND|IFNULL)\(\s*)?(?:\w+\.)?(?:net|qty)\b'
                              r'|CASE\b.{0,200}?\b(?:net|qty)\b)'
                              r'|\{vat_math\.cash_sql\(', re.IGNORECASE | re.DOTALL),
    'purchase net': re.compile(r'purchase_net_sql\('),
    'sales net/qty': re.compile(r'sales_(?:net|qty)_sql\('),
    'population':  re.compile(r'purchase_population_filter\(|returned_lines_filter\('),
}


def shapes_by_function(src, whole_file=False):
    """{function: {shape, ...}} over the customer-keyed sales_transactions queries
    of `src`. `whole_file` counts every query (the module: its key arrives through a
    `{where}` hole, so it never names a customer column). A purchase-population call
    is always a customer-history read, whatever key the query carries."""
    out = {}
    for func, sql in _census.queries(src):
        if 'sales_transactions' not in sql:
            continue
        if not (whole_file or _KEYED.search(sql) or 'customer' in func.lower()
                or SHAPES['population'].search(sql)):
            continue
        hit = {name for name, pat in SHAPES.items() if pat.search(sql)}
        if hit:
            out.setdefault(func, set()).update(hit)
    return out


def tree_shapes():
    """{'file::function': {shape}} across inventory_app/ and scripts/."""
    out = {}
    for rel, path in _census.py_files(include_scripts=True):
        whole = rel == MODULE
        for func, hit in shapes_by_function(_census.read(path), whole_file=whole).items():
            out[f'{rel}::{func}'] = hit
    return out


# site -> (shapes it holds, why it is allowed to). Sites OUTSIDE the module only.
DECLARED = {
    # ── another question about a customer, deliberately not ยอดซื้อรวม / ซื้อล่าสุด ──
    'winback.py::compute_winback': ({'population'},
        'win-back: purchase DATES of one scope the CALLER builds, so the module hands '
        'it the all-time scope (history()["winback"]). Reads the purchase population '
        'itself; it is the module\'s collaborator, not a competing definition.'),
    'models/customers.py::_cross_sell_suggestions': ({'doc count', 'population'},
        'เสนอเพิ่ม (#498): how many OTHER shops bought a product and whether THIS shop '
        'ever did. Per product across customers, with a self-exclusion keyed on the '
        'shop; not one customer\'s history.'),
    'price_lookup.py::find_customers': ({'last date'},
        'the customer picker\'s search hint, a typeahead aid and not a worklist or '
        'money figure (its own docstring said so before the sweeps existed).'),
    # ── money owed (AR), not money spent ──
    'blueprints/mobile.py::sales_trip': ({'raw money'},
        'the sales-trip list\'s outstanding: unpaid invoices per customer, '
        'VAT-inclusive cash. Money the customer owes, not what it bought; its ซื้อล่าสุด '
        'comes from purchase_history.histories().'),
    'models/payments.py::get_payment_status': ({'last date', 'raw money'},
        'the /ar รายบิล document ledger: one row per bill, billed against paid. An AR '
        'balance, not a purchase total.'),
    'models/payments.py::get_ar_reconciliation': ({'raw money'},
        'the "Ledger (Sendy)" unpaid column of /ar?tab=กระทบยอด, deliberately '
        'unfiltered so it can be held against the Express snapshot.'),
    'models/payments.py::find_payment_candidates': ({'last date', 'raw money'},
        'matches an incoming transfer to a customer\'s unpaid bills (VAT-inclusive '
        'cash owed). An AR search, not a purchase total.'),
    'payments_alloc.py::_settlement_rows': ({'last date', 'raw money'},
        'per-invoice billed vs collected for AR allocation (VAT-inclusive cash, '
        'credit notes handled by the allocation itself).'),
    # ── one document, one product, one period, or the price domain ──
    'commission.py::<module>': ({'last date'},
        'a module-level query reading a document\'s own issue date (MIN(date_iso) per '
        'doc_base) for the commission tab. A date on one invoice, not a customer\'s history.'),
    'marketplace_match.py::_ivs_for': ({'last date'},
        'marketplace IV matching: the candidate invoice\'s own date (MIN(date_iso) per '
        'document). Per document, never a customer\'s last purchase.'),
    'marketplace_match.py::_ivs_and_srs_for': ({'last date'},
        'marketplace IV/SR matching: each candidate document\'s own date (MIN(date_iso) '
        'per document). Per document, not a customer\'s history.'),
    'marketplace_match.py::plan_manual_pick': ({'last date'},
        'the manual IV pick: the picked document\'s own date (MIN(date_iso)). Per '
        'document, not a customer\'s history.'),
    'commission.py::get_invoices_for_salesperson': ({'last date', 'raw money'},
        'the sales-rep commission tab: one row per INVOICE with its customer. A '
        'document total, never summed per customer.'),
    'models/pricing_ap.py::get_product_pricing': ({'doc count', 'last date', 'raw money'},
        'one product\'s price evidence per customer and per price row: realised price, '
        'bill counts and last-sale dates PER PRICE. Price evidence (#554), not a '
        'customer\'s purchase history.'),
    'models/sales.py::get_product_trade_summary': ({'doc count', 'last date', 'sales net/qty'},
        '/products/<id>/trade: one product across every customer. Keyed by product; '
        'the customer column is only grouped by (the trade screens, #627).'),
    'models/sales.py::get_trade_dashboard': ({'doc count', 'sales net/qty'},
        'the dashboard\'s figures for a DATE RANGE over all customers, plus a Top-10 '
        'ranking. Not one customer\'s history (the trade screens, #627).'),
    'models/ecommerce_overview.py::get_marketplace_freshness': ({'last date'},
        'how fresh a PLATFORM\'s imported sales are, keyed on the หน้าร้าน '
        'pseudo-customers. Data freshness, not a B2B customer\'s buying history.'),
    'revenue.py::revenue_summary': ({'doc count', 'raw money'},
        '/revenue KPIs over revenue_filter(): the revenue question, which counts '
        'documents differently from the purchase question on purpose.'),
    'revenue.py::top_customers_by_revenue': ({'doc count', 'raw money'},
        '/revenue Top-N per customer over revenue_filter(): revenue leaves returns '
        'out rather than subtracting them, the stance #494 left alone (HS is #514).'),
    'revenue.py::unmapped_revenue_drilldown': ({'raw money'},
        'revenue grouped by BSN code / brand over revenue_filter(); customer is only '
        'counted. Not per customer.'),
    'scripts/audit_product_naming.py::evidence_for_product': ({'last date'},
        'the naming audit\'s last-sale date for ONE PRODUCT (its customer clause only '
        'drops marketplace rows). An offline audit judging names, not a worklist.'),
}

# surface -> the purchase_history entry point it must call. Each holds nothing of
# its own except what DECLARED lists for it.
SURFACES_MUST_CALL = {
    'models/customers.py::get_customer_summary_by_code': 'history',
    'models/customers.py::get_customers': 'histories',
    'blueprints/mobile.py::customer_detail': 'totals',
    'blueprints/mobile.py::sales_trip': 'histories',
    'call_card.py::get_card': 'history',
    'call_card.py::get_call_list': 'histories',
}

# module function -> shapes it must still use (the shared helpers, not a copy).
MODULE_PINS = {
    'histories': {'purchase net', 'population'},
    '_totals': {'purchase net', 'sales net/qty', 'population'},
    'history': {'purchase net', 'sales net/qty', 'population'},
    'products': {'sales net/qty', 'population'},
}


def calls_module(src, func, name):
    """True if function `func` in `src` calls purchase_history.<name>(...)."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == name
                       and isinstance(n.func.value, ast.Name)
                       and n.func.value.id == 'purchase_history'
                       for n in ast.walk(node))
    raise AssertionError(f'{func} not found')


# ── The census ───────────────────────────────────────────────────────────────

def test_every_history_shape_outside_the_module_is_declared():
    found = {s: h for s, h in tree_shapes().items() if s.split('::')[0] != MODULE}
    declared = {s: set(sh) for s, (sh, _why) in DECLARED.items()}
    undeclared = {s: sorted(h) for s, h in found.items() if declared.get(s) != h}
    stale = {s: sorted(h) for s, h in declared.items() if found.get(s) != h}
    assert not undeclared and not stale, (
        'A customer-keyed sales_transactions query outside purchase_history.py holds '
        'a history shape (last date, document count, money/quantity sum, population). '
        'Read it from purchase_history, or declare it in DECLARED with those shapes '
        'and a reason.\n'
        f'  found but not declared (or shapes differ): {undeclared}\n'
        f'  declared but not found with those shapes: {stale}')


@pytest.mark.parametrize('site', sorted(DECLARED))
def test_every_exemption_carries_a_reason(site):
    assert len(DECLARED[site][1]) > 40, f'{site}: say WHAT question it answers instead'


@pytest.mark.parametrize('site', sorted(SURFACES_MUST_CALL))
def test_the_customer_surfaces_call_the_module_and_hold_nothing_of_their_own(site):
    rel, func = site.split('::')
    src = _census.read(os.path.join(_census.APP, rel))
    entry = SURFACES_MUST_CALL[site]
    assert calls_module(src, func, entry), f'{site} stopped calling purchase_history.{entry}'
    own = shapes_by_function(src).get(func, set())
    allowed = set(DECLARED.get(site, ((), ''))[0])
    assert own == allowed, (
        f'{site} holds history shapes of its own: {sorted(own - allowed)} '
        f'(declared and missing: {sorted(allowed - own)}). Read them from purchase_history.')


def test_the_module_still_uses_the_shared_helpers():
    found = shapes_by_function(_census.read(os.path.join(_census.APP, MODULE)), whole_file=True)
    # NB: a function that holds no keyed query would not appear: a clean {} here
    # means the walker saw nothing, so require every pinned function to be present.
    missing = {f for f in MODULE_PINS if f not in found}
    assert not missing, f'the walker saw no keyed query in {missing}'
    short = {f: sorted(need - found[f]) for f, need in MODULE_PINS.items() if need - found[f]}
    assert not short, f'purchase_history functions stopped using their helpers: {short}'


# ── The sweep's own coverage: it must be able to fail ────────────────────────

def _src(sql, func='report', f_prefix='f'):
    return f'def {func}(conn, key):\n    return conn.execute({f_prefix}"""{sql}""", (key,))\n'


PLANTED = {
    'a last date keyed by customer_code':
        ('SELECT MAX(date_iso) FROM sales_transactions WHERE customer_code = ?', 'last date'),
    # #699 review W5: the key expression is the key, and the old censuses never saw it
    'a last date keyed ONLY through customer_key_sql':
        ("SELECT MAX(s.date_iso) FROM sales_transactions s "
         "WHERE {purchase_history.customer_key_sql('s')} = ?", 'last date'),
    'a raw sum keyed only through customer_key_sql':
        ("SELECT SUM(s.net) FROM sales_transactions s "
         "WHERE {purchase_history.customer_key_sql('s')} = ?", 'raw money'),
    # P2 review N4: calling the module does not stop a second, private total
    'a private ยอดซื้อรวม beside a module call':
        ("SELECT SUM({sales_filters.purchase_net_sql('s')}) FROM sales_transactions s "
         "WHERE s.customer = ?", 'purchase net'),
    'a last date through a CASE':
        ("SELECT customer_code, MAX(CASE WHEN doc_base LIKE 'IV%' THEN date_iso END) "
         "FROM sales_transactions GROUP BY customer_code", 'last date'),
    'a first date':
        ('SELECT MIN(date_iso) FROM sales_transactions WHERE customer_code = ?', 'last date'),
    'a private document count':
        ('SELECT COUNT(DISTINCT doc_base) FROM sales_transactions WHERE customer = ?',
         'doc count'),
    'a private qty':
        ("SELECT SUM({sales_filters.sales_qty_sql('s')}) FROM sales_transactions s "
         "WHERE s.customer_code = ?", 'sales net/qty'),
    'a private population read':
        ("SELECT 1 FROM sales_transactions s WHERE s.customer = ? AND "
         "{price_lookup.purchase_population_filter('s')}", 'population'),
}

NOT_SITES = {
    'not customer-keyed': 'SELECT MAX(date_iso) FROM sales_transactions WHERE date_iso >= ?',
    'another table': 'SELECT MAX(date_iso) FROM purchase_transactions WHERE supplier = ?',
    'a bare row list': "SELECT date_iso, net FROM sales_transactions WHERE customer = ?",
}


@pytest.mark.parametrize('shape', sorted(PLANTED))
def test_the_sweep_sees_every_planted_shape(shape):
    sql, expected = PLANTED[shape]
    assert shapes_by_function(_src(sql)) == {'report': {expected}}, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_history_figure(shape):
    assert shapes_by_function(_src(NOT_SITES[shape], f_prefix='')) == {}, f'{shape}: false positive'


def test_the_sweep_sees_a_scope_passed_in_by_the_caller():
    """The WHERE arrives as {where}, so the query names no customer column: the
    function's own name is the only signal."""
    src = _src('SELECT MAX(date_iso) FROM sales_transactions WHERE {where}',
               func='_customer_last_buy')
    assert shapes_by_function(src) == {'_customer_last_buy': {'last date'}}


def test_a_surface_with_a_private_total_is_caught():
    """CONTROL for the surface check: the call is there, the private sum is too."""
    src = ('import purchase_history\n'
           'def surface(conn, key):\n'
           '    h = purchase_history.histories(conn)\n'
           '    return conn.execute(f"SELECT SUM({sales_filters.purchase_net_sql()}) '
           'FROM sales_transactions WHERE customer = ?", (key,))\n')
    assert calls_module(src, 'surface', 'histories')
    assert shapes_by_function(src) == {'surface': {'purchase net'}}


def test_the_module_call_check_can_fail():
    src = ('import purchase_history\n'
           'def a(conn):\n    return purchase_history.histories(conn)\n'
           'def b(conn):\n    return conn.execute("SELECT 1")\n')
    assert calls_module(src, 'a', 'histories')
    assert not calls_module(src, 'b', 'histories')
    assert not calls_module(src, 'a', 'history')
