"""Every net/qty aggregate over `sales_transactions` is either ยอดขาย net of
returns (`sales_filters.sales_net_sql()` / `sales_qty_sql()`, #627) or
declared below with the question it answers instead.

The bug: SR (credit-note) lines are stored with POSITIVE net/qty. A bare
SUM(net) ADDS a return as a sale. /sales and /products/<id>/trade did exactly
that, while /trade-dashboard dropped SR, so on prod 2026 Jan-Sep the two trade
screens read ฿2,900,927.44 and ฿2,886,748.94 for one period. Put's ruling
2026-09-22: on the trade screens a return is SUBTRACTED, the rule ยอดซื้อ
already follows (#591), and the dashboard also drops the documents invoiced
in error.

The census is per FUNCTION, not per file. test_revenue_filter_coverage.py is
file-level, and models/sales.py holds both /sales (every document, option ก)
and the dashboard (giveaway dropped) — a file-level entry cannot tell them
apart. Every SQL string in inventory_app/ and scripts/ that reads
sales_transactions has its raw net/qty aggregates counted, and so do its
helper calls. A raw aggregate must be in ALLOWED with its exact count and a
reason; the helper must be exactly where MUST_USE_HELPER says. A new
aggregate inside a listed function changes its count, so it cannot hide
behind the old entry.

What it still cannot see: SQL whose FROM and SUM sit in separate variables
(accounting's COGS reads `{base_qty}`, a local name), a figure computed in
Python from fetched rows (sales_doc's per-document total), Jinja or JS
arithmetic, and a table name that arrives at run time. The behavioural half
is test_627_sales_net_of_returns.py.
"""
import ast
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(_ROOT, 'inventory_app')
SCRIPTS = os.path.join(_ROOT, 'scripts')

# Raw net/qty aggregates: the shapes the #494 and #591 censuses sweep, applied
# to both columns, plus the two helpers that sum net or qty without saying so
# (VAT-inclusive cash, base-unit qty).
_RAW_AGG = re.compile(
    r'SUM\(\s*(?:(?:(?:COALESCE|ROUND|IFNULL)\(\s*)?(?:\w+\.)?(?:net|qty)\b'
    r'|CASE\b.{0,200}?\b(?:net|qty)\b'
    r'|\{vat_math\.cash_sql\('
    r'|\{sales_filters\.base_qty_sql\()',
    re.IGNORECASE | re.DOTALL)
_HELPER = re.compile(r'\{sales_filters\.sales_(?:net|qty)_sql\(')

# file::function -> how many helper calls it holds. The trade screens.
MUST_USE_HELPER = {
    'models/sales.py::get_sales_summary': 2,          # /sales cards: qty + net
    'models/sales.py::get_trade_dashboard': 6,        # card (2) + weekly (1) + top products (2) + top customers (1)
    'models/sales.py::get_product_trade_summary': 8,  # card (2) + customers (2) + monthly (2) + docs (1) + doc units (1)
    'call_card.py::_assemble_products': 2,            # ซื้อประจำ ordering + its ซื้อรวม qty
    'models/customers.py::_customer_sales_aggregates': 3,  # จำนวนชิ้น + top_products (the call card's แบรนด์เด่น)
}

# file::function -> (raw aggregates it holds, why a return is not subtracted there).
ALLOWED = {
    # ── the trade screens' own leftovers ──
    'models/sales.py::get_product_trade_summary': (1,
        'free_qty: counts a ฿0 line of an INVOICE as แถม and never looks at a '
        'credit note (doc_base NOT LIKE SR). A freebie count, not ยอดขาย.'),
    # ── revenue: returns LEFT OUT, not subtracted (GL 41-01), unchanged by #627 ──
    'models/accounting.py::get_accounting_summary': (4,
        '/accounting: sales_net and COGS over GL 41-01 — SR dropped by WHERE, '
        'giveaway dropped from revenue only. Put parked "returns in the P&L" '
        '(option C) until the accountant is asked; must NOT move.'),
    'revenue.py::revenue_summary': (1, '/revenue KPIs over revenue_filter(): SR dropped, not subtracted.'),
    'revenue.py::top_customers_by_revenue': (1,
        '/revenue top customers over revenue_filter(): the revenue question, SR dropped.'),
    'revenue.py::top_brands_by_revenue': (1,
        '/revenue brand split over revenue_filter(): the revenue question, SR dropped.'),
    'revenue.py::unmapped_revenue_drilldown': (2,
        'revenue grouped by BSN code / brand over revenue_filter() (base_filter): SR dropped.'),
    'cashflow.py::revenue_by_month': (1,
        'the revenue series on /revenue and /cashflow over revenue_filter(): SR dropped.'),
    'models/financial_health.py::get_trailing_months': (1,
        'financial-health trend over revenue_filter() (Put 2026-07-30 option ข joined '
        'it to /revenue): SR dropped, must agree with /accounting.'),
    'models/financial_health.py::get_current_month_pace': (1,
        'financial-health month pace over revenue_filter(): SR dropped.'),
    'models/financial_health.py::_trailing_margin': (2,
        'financial-health margin: revenue and base-unit COGS over revenue_filter(), SR dropped.'),
    # ── AR / settlement: a credit note is handled as a credit on purpose ──
    'blueprints/mobile.py::sales_trip': (1,
        '/m/sales-trip outstanding: unpaid invoices per customer, VAT-inclusive cash. AR.'),
    'models/payments.py::get_payment_status': (2,
        'the /ar รายบิล document ledger: one row per bill, billed vs paid. AR.'),
    'models/payments.py::get_payment_summary': (4,
        'the /ar รายบิล summary over IV bills (SR and HS excluded: HS is paid '
        'on the spot, #514). AR, not ยอดขาย.'),
    'models/payments.py::get_ar_reconciliation': (1,
        'the "Ledger (Sendy)" unpaid column held against the Express snapshot. AR.'),
    'models/payments.py::find_payment_candidates': (1,
        'matches an incoming transfer to unpaid bills (VAT-inclusive cash owed). AR.'),
    'payments_alloc.py::_settlement_rows': (3,
        'per-invoice billed vs collected: SR summed ON PURPOSE as the credit it '
        'is (authoritative amounts from credit_note_amounts). AR allocation.'),
    'payments_alloc.py::cash_in_rows': (1,
        'cash received per invoice for /cashflow, over non-SR non-HS bills. Cash, not ยอดขาย.'),
    # ── one document, one product's price, one order, one platform ──
    'models/customers.py::_customer_documents': (1,
        'one row per DOCUMENT, a credit note already negated in Python. A document list.'),
    'models/customers.py::_customer_product_cards': (2,
        'per (product, unit) over purchase_population_filter, which excludes SR '
        'outright: a credit note is never in this population.'),
    'commission.py::get_invoices_for_salesperson': (1,
        'the commission tab\'s per-INVOICE rows (IV/HS only); payouts are '
        'receipt-driven and never read this sum.'),
    'models/pricing_ap.py::get_product_pricing': (9,
        'realised selling PRICE of one product (price evidence, #554\'s '
        'question), not ยอดขาย. Its averages also take in SR lines (qty > 0 at '
        'their price): a pricing call for Put, raised on the #627 PR.'),
    'models/pricing_ap.py::get_product_pricing_summary': (3,
        'same page, same price-evidence question as get_product_pricing.'),
    'models/marketplace.py::set_amount_review': (1,
        'one marketplace order\'s own billed lines vs its payout, for IV '
        'matching. Per order, never a total.'),
    'models/ecommerce_overview.py::_sold_since_by_pid': (1,
        'units sold per platform since a stock snapshot, SR excluded by WHERE '
        '(returned goods are not a deduction). Stock quantity, not ยอดขาย.'),
    # ── scripts: one-offs and diagnostics, no screen reads them ──
    'scripts/backfill_nonstock_2026_08.py::main': (1,
        'a one-off August 2026 backfill check; no app surface.'),
    'scripts/express_dbf_smoke_check.py::main': (1,
        'a DBF import smoke check comparing raw sums to the source file; no app surface.'),
    'scripts/verify_commission_reassign.py::_base_by_rep': (1,
        'a hand-run live-data check of the commission reassign rules (mig 143), '
        'not a screen; commission is receipt-driven.'),
}

# The only function that drops the documents invoiced in error (Put,
# 2026-09-22), and the one that must keep them (option ก, 2026-07-30).
_GIVEAWAY_OUT = re.compile(r'\{sales_filters\.not_a_sale_clause\(')
_ANY_REVENUE_GUARD = re.compile(r'not_a_sale_clause|revenue_filter|excludes_revenue')


def _render(node):
    """Source text of a string expression, f-string holes kept as {expr}. A
    `+` chain renders its non-string operands as holes too, so a query built
    as `"... SUM(" + sales_filters.base_qty_sql() + ") ..."` is read whole."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant)
                       else '{' + ast.unparse(v.value) + '}' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return ((left if left is not None else '{' + ast.unparse(node.left) + '}')
                + (right if right is not None else '{' + ast.unparse(node.right) + '}'))
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


def _sales_queries(src):
    return [(f, sql) for f, sql in _queries(src) if 'sales_transactions' in sql]


def _per_function(src, pattern):
    counts = {}
    for func, sql in _sales_queries(src):
        n = len(pattern.findall(sql))
        if n:
            counts[func] = counts.get(func, 0) + n
    return counts


def _py_files():
    for base, prefix in ((APP, ''), (SCRIPTS, 'scripts/')):
        for root, _dirs, names in os.walk(base):
            if any(part in root for part in ('__pycache__', 'instance', 'static')):
                continue
            for n in names:
                if n.endswith('.py'):
                    path = os.path.join(root, n)
                    yield prefix + os.path.relpath(path, base).replace(os.sep, '/'), path


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _app_counts(pattern):
    out = {}
    for rel, path in _py_files():
        for func, n in _per_function(_read(path), pattern).items():
            out[f'{rel}::{func}'] = n
    return out


def _function_queries(site):
    rel, func = site.split('::')
    base = SCRIPTS if rel.startswith('scripts/') else APP
    path = os.path.join(base, rel[len('scripts/'):] if rel.startswith('scripts/') else rel)
    return [sql for f, sql in _sales_queries(_read(path)) if f == func]


# ── The census ───────────────────────────────────────────────────────────────

def test_every_raw_aggregate_is_declared():
    found = _app_counts(_RAW_AGG)
    declared = {site: n for site, (n, _why) in ALLOWED.items()}
    undeclared = {s: n for s, n in found.items() if declared.get(s) != n}
    stale = {s: n for s, n in declared.items() if found.get(s) != n}
    assert not undeclared and not stale, (
        'A net/qty aggregate over sales_transactions that is not '
        'sales_filters.sales_net_sql()/sales_qty_sql() must be declared in '
        'ALLOWED with its exact count and the question it answers.\n'
        f'  found but not declared (or count differs): {undeclared}\n'
        f'  declared but not found at that count: {stale}')


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_exemption_carries_a_reason(site):
    assert len(ALLOWED[site][1]) > 40, f'{site}: say WHY a return is not subtracted there'


def test_the_trade_screens_use_the_helper():
    found = _app_counts(_HELPER)
    assert found == MUST_USE_HELPER, f'expected {MUST_USE_HELPER}, found {found}'


# ── WHICH helper feeds WHICH column (the #626 lesson) ────────────────────────
#
# A per-function COUNT cannot see sales_qty_sql() swapped in for
# sales_net_sql() (the count stays the same while a money column becomes a
# unit count). So each call is pinned to the alias it feeds.
_HELPER_CALL = re.compile(
    r"\{sales_filters\.sales_(net|qty)_sql\([^)]*\)\}[\s\d,\)]*\bAS\s+(\w+)")


def _helper_alias_pairs(sql):
    return _HELPER_CALL.findall(sql)


def test_each_helper_call_feeds_a_column_of_its_own_kind():
    for site, n in MUST_USE_HELPER.items():
        pairs = [p for sql in _function_queries(site) for p in _helper_alias_pairs(sql)]
        assert len(pairs) == n, f'{site}: {n} helper calls, {len(pairs)} feed a named column'
        wrong = [(k, a) for k, a in pairs if k not in a.lower()]
        assert not wrong, f'{site}: a helper feeds a column of the other kind: {wrong}'


def test_the_alias_check_can_fire():
    """CONTROL: the check above must be able to see a swapped helper."""
    sql = ("SELECT SUM({sales_filters.sales_qty_sql()}) AS total_net "
           "FROM sales_transactions")
    assert _helper_alias_pairs(sql) == [('qty', 'total_net')]


# ── ruling 2: WHICH screen drops the giveaway ────────────────────────────────

def test_every_dashboard_sales_query_drops_the_documents_invoiced_in_error():
    queries = [q for q in _function_queries('models/sales.py::get_trade_dashboard')
               if _HELPER.search(q)]
    assert len(queries) == 4, 'CONTROL: card, weekly, top products, top customers'
    for q in queries:
        assert len(_GIVEAWAY_OUT.findall(q)) == 1, q


def test_the_sales_page_keeps_every_document():
    """/sales mirrors Express and shows every document, including ones
    invoiced in error (Put, 2026-07-30, option ก)."""
    queries = _function_queries('models/sales.py::get_sales_summary')
    assert len(queries) == 1 and _HELPER.search(queries[0]), 'CONTROL: the /sales query was read'
    assert not _ANY_REVENUE_GUARD.search(queries[0])


# ── The sweep's own coverage: one rogue source per shape ─────────────────────

def _src(sql, func='report', f_prefix=''):
    return f'def {func}(conn, x):\n    return conn.execute({f_prefix}"""{sql}""", (x,))\n'


AGGREGATE_SHAPES = {
    'bare_net':     'SELECT SUM(net) FROM sales_transactions WHERE date_iso >= ?',
    'bare_qty':     'SELECT SUM(qty) FROM sales_transactions WHERE date_iso >= ?',
    'aliased':      'SELECT SUM(st.net) FROM sales_transactions st WHERE st.date_iso >= ?',
    'digit_alias':  'SELECT SUM(s2.qty) FROM sales_transactions s2 WHERE s2.date_iso >= ?',
    'coalesced':    'SELECT COALESCE(SUM(net), 0) FROM sales_transactions WHERE date_iso >= ?',
    'rounded':      'SELECT ROUND(SUM(net), 2) FROM sales_transactions WHERE date_iso >= ?',
    'wrapped':      'SELECT SUM(COALESCE(qty, 0)) FROM sales_transactions WHERE date_iso >= ?',
    'case':         "SELECT SUM(CASE WHEN doc_base LIKE 'SR%' THEN -net ELSE net END) "
                    'FROM sales_transactions WHERE date_iso >= ?',
    'vat_cash':     'SELECT SUM({vat_math.cash_sql()}) FROM sales_transactions WHERE date_iso >= ?',
    'base_qty':     'SELECT SUM({sales_filters.base_qty_sql()} * 2) FROM sales_transactions '
                    'WHERE date_iso >= ?',
}

NOT_SITES = {
    'the helper itself': _src('SELECT SUM({sales_filters.sales_net_sql()}) '
                              'FROM sales_transactions WHERE date_iso >= ?', f_prefix='f'),
    'another table':     _src('SELECT SUM(net) FROM purchase_transactions WHERE date_iso >= ?'),
    'no money column':   _src('SELECT COUNT(*), MAX(date_iso) FROM sales_transactions '
                              'WHERE date_iso >= ?'),
    'prose':             ('def note():\n'
                          '    """Was SUM(net) FROM sales_transactions WHERE date_iso >= ?"""\n'
                          '    return 1\n'),
}


@pytest.mark.parametrize('shape', sorted(AGGREGATE_SHAPES))
def test_the_sweep_sees_every_aggregate_shape(shape):
    sql = AGGREGATE_SHAPES[shape]
    src = _src(sql, f_prefix='f' if '{' in sql else '')
    assert _per_function(src, _RAW_AGG) == {'report': 1}, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_raw_aggregate(shape):
    assert _per_function(NOT_SITES[shape], _RAW_AGG) == {}, f'{shape}: false positive'


@pytest.mark.parametrize('shape', ['implicit concatenation', 'plus concatenation',
                                   'plus a call', 'method'])
def test_the_sweep_sees_every_string_shape(shape):
    src = {
        'implicit concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) "\n'
            '        "FROM sales_transactions WHERE date_iso >= ?")\n',
        'plus concatenation':
            'def report(conn):\n    return conn.execute("SELECT SUM(net) " +\n'
            '        "FROM sales_transactions WHERE date_iso >= ?")\n',
        # accounting.py's brand breakdown: the FROM and the SUM on either side
        # of a helper call. Invisible to a renderer that gives up on the call.
        'plus a call':
            'def report(conn):\n    return conn.execute("SELECT SUM(st.net), " +\n'
            '        sales_filters.not_a_sale_clause() + " FROM sales_transactions st")\n',
        'method':
            'class Repo:\n    def report(self, conn):\n'
            '        return conn.execute("SELECT SUM(qty) FROM sales_transactions '
            'WHERE date_iso >= ?")\n',
    }[shape]
    assert sum(_per_function(src, _RAW_AGG).values()) == 1, f'{shape}: unswept'
