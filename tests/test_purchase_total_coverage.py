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

from tests import _census

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
    'purchase_history.py::customer_documents': (1,
        'ยอดรวมเอกสาร: one row per DOCUMENT, VAT added on แยก VAT documents, a '
        'credit note negated in Python. A document total, VAT-inclusive by design. '
        'Moved here from models/customers.py in card C P2 (the owner module holds '
        'the document list, so consumers point at it).'),
    'purchase_history.py::products': (2,
        'the per-(product, unit) rows of the customer page and the call card, moved '
        'from _customer_product_cards: qty and money NET of credit notes (#646) as '
        'CASE expressions over price_lookup.returned_lines_filter. Per-product '
        'figures, never the customer\'s total (that is purchase_net_sql, '
        'MUST_USE_HELPER). Split out of history() in card C P3 so the call card can '
        'read the rows alone.'),
    'purchase_history.py::history': (1,
        'returned_net_total, the CUSTOMER-level sum of credit notes in the window '
        'behind the page\'s "returns not shown on a card" footnote: the credit '
        'notes themselves, deliberately not un-netted sales. (The per-product '
        'figures it used to hold with this one moved to products() in card C P3.)'),
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
# The call card is not listed: it reads purchase_history.history() (card C P3, which
# also deleted customers._customer_sales_aggregates, the entry that stood here for
# "header + monthly"; those two now live in _totals and history below).
MUST_USE_HELPER = {
    'purchase_history.py::history': 1,                      # card C: monthly
    'purchase_history.py::_totals': 1,                      # card C: header (history() and totals())
    'purchase_history.py::histories': 1,                    # card C: every customer's ยอดซื้อรวม
}


# A module whose whole job is one customer's history. Its key arrives through a
# `{where}` / `{key}` hole in functions named `history`/`histories`, so neither
# signal below fires and the gate would report {} for the file (P1 review W1:
# a clean result because the check never ran). Every sales_transactions query
# in these files counts as per-customer.
CUSTOMER_MODULES = ('purchase_history.py',)


def _customer_scoped(func, sql):
    return bool(_CUSTOMER_KEY.search(sql) or 'customer' in func.lower())


def _per_function(src, pattern):
    """{function: hits of `pattern` in its per-customer sales_transactions queries}"""
    return _census.per_function(src, pattern, 'sales_transactions', _customer_scoped)


def _app_counts(pattern):
    out = {site: n for site, n in
           _census.app_counts(pattern, 'sales_transactions', _customer_scoped).items()
           if site.split('::')[0] not in CUSTOMER_MODULES}
    for rel in CUSTOMER_MODULES:
        src = _census.read(os.path.join(_census.APP, rel))
        for func, n in _census.per_function(src, pattern, 'sales_transactions').items():
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


# Card C P2: the customer surfaces read their history from ONE module instead of
# holding an aggregate each, so the census above no longer names them. This is the
# positive control that does: each must still CALL purchase_history, and the
# census itself proves it holds no raw aggregate of its own (none is in ALLOWED).
SURFACES_ON_THE_MODULE = {
    ('models/customers.py', 'get_customer_summary_by_code'): 'history',
    ('models/customers.py', 'get_customers'): 'histories',
    ('blueprints/mobile.py', 'customer_detail'): 'totals',
    ('blueprints/mobile.py', 'sales_trip'): 'histories',
    ('call_card.py', 'get_call_list'): 'histories',
    ('call_card.py', 'get_card'): 'history',       # card C P3
}


def _calls_module(src, func, name):
    """True if function `func` in `src` calls purchase_history.<name>(...)."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == name
                       and isinstance(n.func.value, ast.Name)
                       and n.func.value.id == 'purchase_history'
                       for n in ast.walk(node))
    raise AssertionError(f'{func} not found')


@pytest.mark.parametrize('site', sorted(SURFACES_ON_THE_MODULE))
def test_the_customer_surfaces_read_purchase_history(site):
    rel, func = site
    src = _census.read(os.path.join(_census.APP, rel))
    assert _calls_module(src, func, SURFACES_ON_THE_MODULE[site]), \
        f'{rel}::{func} stopped calling purchase_history.{SURFACES_ON_THE_MODULE[site]}'


def test_the_module_call_check_can_fail():
    """CONTROL: a matcher that answered True for anything would pass the above."""
    src = ('import purchase_history\n'
           'def a(conn):\n    return purchase_history.histories(conn)\n'
           'def b(conn):\n    return conn.execute("SELECT 1")\n')
    assert _calls_module(src, 'a', 'histories')
    assert not _calls_module(src, 'b', 'histories')
    assert not _calls_module(src, 'a', 'history')


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
                                   'module constant', 'method', 'format chain'])
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
        # Review of #678 (W1): see test_591's 'format chain'.
        'format chain':
            'def report(conn, w):\n    return conn.execute(\"\"\"SELECT SUM(\n'
            '        net) FROM sales_transactions WHERE customer = {w}\"\"\".format(w=w)\n'
            '        + " GROUP BY customer").fetchone()\n',
        'method':
            'class Repo:\n    def report(self, conn):\n'
            '        return conn.execute("SELECT SUM(net) FROM sales_transactions '
            'WHERE customer = ?")\n',
    }[shape]
    assert sum(_per_function(src, _RAW_NET_AGG).values()) == 1, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_raw_purchase_total(shape):
    assert _per_function(NOT_SITES[shape], _RAW_NET_AGG) == {}, f'{shape}: false positive'
