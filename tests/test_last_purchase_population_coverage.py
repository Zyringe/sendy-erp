"""Every per-customer "last purchase" / "purchase count" reads ONE population.

`price_lookup.evidence_filter` is that population: real, paid, non-returned
sales lines. #493 moved the customer page onto it; #513 found the call card,
the /call worklist and the mobile sales-trip list still reading raw rows, so a
credit note read as a recent purchase and the เงียบ badge — the only signal
that surfaces a customer who stopped buying — went out on exactly the customer
it exists to find.

The sweep, per QUERY rather than per file (a file-level allowlist answers "is
this file known", not "is this aggregate declared"): every SQL string in
inventory_app/ that reads sales_transactions and is keyed to a customer (it
names the customer or customer_code column, or it lives in a function whose
name says customer) is measured twice — its DATE-OF-LAST-ROW and
DOCUMENT-COUNT aggregates (MAX(date_iso), MAX(s.date_iso),
COUNT(DISTINCT doc_base)), and its evidence_filter call sites. Each function is
listed in ALLOWED with BOTH numbers and a reason, so adding, removing or
re-pointing an aggregate changes a number and cannot hide behind the old entry.

Why both numbers rather than "declare only the unfiltered ones": one query can
hold both. `models/customers.py::get_customers` renders ซื้อล่าสุด from an
evidence-filtered subquery sitting INSIDE the same SELECT as the raw
MAX(s.date_iso) that feeds ช่วงเวลา. A sweep that skipped any query mentioning
the helper would have called that whole query clean — and would have stayed
green if the ซื้อล่าสุด subquery were later swapped back to a raw MAX.

⚠ What this sweep CANNOT see, so a green run says nothing about them:
  - WHICH aggregate inside a query the helper applies to. `(1, 1)` is the only
    shape that proves it on its own; anything else has to be read. That is the
    sweep's real limit, and why ALLOWED holds prose.
  - a date or count computed in Python from fetched rows, or in Jinja, or in JS
  - `ORDER BY date_iso DESC LIMIT 1` — the same question spelled as a sort
  - COUNT(DISTINCT doc_no) (that is a LINE count, swept by
    test_doc_no_count_coverage.py instead) and COUNT(*) over documents
  - SQL assembled from separate variables, or a table name that arrives at
    run time
  - files outside inventory_app/ (scripts/)
The behavioural half is test_513_call_card_purchase_population.py; this is the
census.
"""
import ast
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'inventory_app')

# "when did this customer last buy" and "how many times did it buy".
_STALE_AGG = re.compile(
    r'MAX\(\s*(?:\w+\.)?date_iso\s*\)'
    r'|COUNT\(\s*DISTINCT\s*\(?\s*(?:\w+\.)?doc_base\s*\)',
    re.IGNORECASE)
# The population, however it is spelled at the call site (`pl` or the full name).
_HELPER = re.compile(r'\{\s*(?:pl|price_lookup)\s*\.\s*evidence_filter\s*\(')
_CUSTOMER_KEY = re.compile(r'\b(?:customer|customer_code)\b', re.IGNORECASE)
# A SQL comment is prose. The comment on the very line this sweep exists to
# protect says "a raw MAX(date_iso) showed a rep a RETURN", and counting that
# is the same mistake as a `grep -c` that counts its own explanation.
_SQL_COMMENT = re.compile(r'--[^\n]*|/\*.*?\*/', re.DOTALL)


def _code_only(sql):
    return _SQL_COMMENT.sub(' ', sql)

# site -> (raw aggregates, evidence_filter call sites, why that is right).
# A census, not a blocklist: every customer-keyed query is listed, the
# filtered ones included, because the pair is what changes when someone
# adds, removes or re-points an aggregate. `(1, 1)` is the goal shape — one
# figure, read off the purchase population.
ALLOWED = {
    # ── the ซื้อ-labelled surfaces: the purchase population, #493 + #513 ──
    'call_card.py::get_call_list': (1, 1,
        "/call's ซื้อล่าสุด column and the เงียบ badge computed from it. Its one "
        'aggregate IS the filtered one (#513).'),
    'blueprints/mobile.py::sales_trip': (1, 1,
        "the trip row's ล่าสุด — a rep deciding who to visit. Its one aggregate "
        'IS the filtered one (#513).'),
    'models/customers.py::_customer_sales_aggregates': (6, 1,
        'the customer page. THREE questions live here and only one is ซื้อ: the '
        'purchase pair (purchase_doc_count + last_purchase_date) is the single '
        'evidence-filtered query and is what every ซื้อ-labelled surface reads; '
        'doc_count/last_date stay raw because the page labels them จำนวนเอกสาร '
        'and the ช่วงเวลา range end, and a credit note IS a document and IS '
        'activity; monthly and the document list are per-period/per-document.'),
    'models/customers.py::get_customers': (3, 1,
        "the /customers list. Its ซื้อล่าสุด column reads the evidence-filtered "
        'last_purchase_date subquery (#493); the raw COUNT/MAX beside it feed '
        'จำนวนเอกสาร and the ช่วงเวลา end, same split as the detail page.'),
    'models/customers.py::_customer_product_cards': (1, 2,
        'สินค้าที่ซื้อบ่อย: times_bought per (product, unit) over the evidence '
        'population (#493 slice 2). Per-product, and already filtered.'),
    'peer_pricing.py::product_peer_prices': (0, 1,
        'peer prices for one product over the evidence population. No date or '
        'document-count aggregate of its own; listed so the census covers every '
        'reader of the population, not only the ones holding an aggregate.'),
    # ── per PRODUCT, not per customer ──
    'call_card.py::_assemble_products': (2, 0,
        "the call card's ซื้อประจำ table: ล่าสุด and the invoice count for ONE "
        'PRODUCT. A per-product figure that never becomes the card header\'s '
        'ซื้อล่าสุด / จำนวนครั้งซื้อ, which read the summary above.'),
    'models/pricing_ap.py::get_product_pricing': (6, 0,
        "one product's price evidence (list prices, per-customer effective "
        'prices, marketplace rows): บิล counts and last-sale dates PER PRICE '
        'ROW. Pricing wants to know a price was really charged and when, '
        'including on documents the purchase population drops.'),
    'models/sales.py::get_product_trade_summary': (2, 0,
        "/products/<id>/trade: one product's document count and last sale date "
        'across every customer. Keyed by product; the customer column is only '
        'grouped by.'),
    # ── a whole period, or a whole platform, not one customer ──
    'models/sales.py::get_trade_dashboard': (1, 0,
        "the dashboard's เอกสาร count for a DATE RANGE over all customers. Not "
        'a per-customer figure at all; customer appears only in its Top-10 '
        'ranking beside it.'),
    'revenue.py::revenue_summary': (1, 0,
        "/revenue's total_invoices for a period, over revenue_filter(). The "
        'revenue question, which counts documents differently from the purchase '
        'question on purpose (see sales_filters).'),
    'revenue.py::top_customers_by_revenue': (1, 0,
        "/revenue's Top-N invoice_count per customer, over revenue_filter(). "
        'Revenue, not purchases: it leaves returns out rather than subtracting '
        'them, the stance #494 deliberately left alone.'),
    'models/ecommerce_overview.py::get_marketplace_freshness': (1, 0,
        'how fresh a PLATFORM\'s imported sales are, keyed on the หน้าร้าน '
        'pseudo-customers. Data freshness for Shopee/Lazada/TikTok, not a B2B '
        "customer's buying history — and evidence_filter excludes หน้าร้าน "
        'outright, so filtering it would answer NULL forever.'),
    # ── a document, or a search hint ──
    'models/customers.py::_customer_documents': (1, 0,
        'one row per DOCUMENT, and the date shown against it — for a credit '
        'note, the credit note\'s own date. A document list must show returns; '
        'the customer page renders them with a negative total.'),
    'blueprints/mobile.py::customer_detail': (2, 0,
        "/m/customer's เอกสารทั้งหมด and the activity dates behind it: the "
        'mobile mirror of the desktop จำนวนเอกสาร card, labelled documents. '
        'Nothing on that screen says ซื้อล่าสุด. (`last_seen` is computed and '
        'the template never renders it — pre-existing, left alone.)'),
    'price_lookup.py::find_customers': (1, 0,
        'the customer picker\'s search hint. A typeahead aid, not a money or '
        'worklist figure — its own docstring said so before this sweep existed.'),
}

# The four surfaces #493 and #513 put on the purchase population. A positive
# control whose failure NAMES the surface that regressed, where the census
# above would only report a changed tuple.
MUST_USE_HELPER = (
    'call_card.py::get_call_list',
    'blueprints/mobile.py::sales_trip',
    'models/customers.py::_customer_sales_aggregates',
    'models/customers.py::get_customers',
)


# ── reading queries out of Python (same reader as test_purchase_total_coverage) ──

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
    """{function: hits of `pattern` in its per-customer sales_transactions
    queries}, SQL comments stripped first."""
    counts = {}
    for func, sql in _queries(src):
        if 'sales_transactions' not in sql:
            continue
        if not (_CUSTOMER_KEY.search(sql) or 'customer' in func.lower()):
            continue
        n = len(pattern.findall(_code_only(sql)))
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


def _census():
    """{site: (raw aggregates, evidence_filter call sites)} over every
    customer-keyed sales_transactions query in the app."""
    aggs, helpers = _app_counts(_STALE_AGG), _app_counts(_HELPER)
    return {site: (aggs.get(site, 0), helpers.get(site, 0))
            for site in set(aggs) | set(helpers)}


# ── The census ───────────────────────────────────────────────────────────────

def test_every_per_customer_last_buy_or_doc_count_is_declared():
    found = _census()
    declared = {site: (a, h) for site, (a, h, _why) in ALLOWED.items()}
    undeclared = {s: n for s, n in found.items() if declared.get(s) != n}
    stale = {s: n for s, n in declared.items() if found.get(s) != n}
    assert not undeclared and not stale, (
        'A per-customer last-buy date or purchase count changed. Declare it in '
        'ALLOWED as (raw aggregates, evidence_filter call sites, reason) — and '
        'if the surface says ซื้อ, it must read price_lookup.evidence_filter.\n'
        f'  found but not declared (or counts differ): {undeclared}\n'
        f'  declared but not found at those counts: {stale}')


@pytest.mark.parametrize('site', sorted(ALLOWED))
def test_every_entry_carries_a_reason(site):
    assert len(ALLOWED[site][2]) > 40, f'{site}: say WHAT question it answers'


@pytest.mark.parametrize('site', MUST_USE_HELPER)
def test_the_surfaces_that_say_bought_read_the_purchase_population(site):
    """Positive control. The census above goes red on any change; this one
    names the surface, so the failure says which screen went back to raw
    rows rather than just that a tuple moved."""
    assert _app_counts(_HELPER).get(site, 0) >= 1, \
        f'{site} stopped reading price_lookup.evidence_filter'


def test_the_population_is_not_found_everywhere():
    """CONTROL for the one above: a matcher that fired on every query would
    satisfy it just as well."""
    found = set(_app_counts(_HELPER))
    assert found == {s for s, (_a, h, _w) in ALLOWED.items() if h}, found


# ── The sweep's own coverage: one rogue source per shape ─────────────────────
#
# A sweep is only worth what its pattern can see, and reading it never shows a
# shape it misses. Every entry is a shape that exists in this app.

def _src(sql, func='report', f_prefix=''):
    return f'def {func}(conn, code):\n    return conn.execute({f_prefix}"""{sql}""", (code,))\n'


AGGREGATE_SHAPES = {
    'bare max':      'SELECT MAX(date_iso) FROM sales_transactions WHERE customer_code = ?',
    'aliased max':   'SELECT MAX(s.date_iso) FROM sales_transactions s WHERE s.customer_code = ?',
    'digit alias':   'SELECT MAX(s2.date_iso) FROM sales_transactions s2 WHERE s2.customer = ?',
    'spaced max':    'SELECT MAX( date_iso ) FROM sales_transactions WHERE customer = ?',
    'doc count':     'SELECT COUNT(DISTINCT doc_base) FROM sales_transactions WHERE customer = ?',
    'doc count alias': 'SELECT COUNT(DISTINCT s.doc_base) FROM sales_transactions s '
                       'WHERE s.customer_code = ?',
    'correlated subquery': 'SELECT c.code, (SELECT MAX(date_iso) FROM sales_transactions s '
                           'WHERE s.customer = c.name) FROM customers c',
    'grouped by code': 'SELECT customer_code, MAX(date_iso) FROM sales_transactions '
                       'GROUP BY customer_code',
    'even when filtered': 'SELECT MAX(date_iso) FROM sales_transactions '
                          "WHERE customer = ? AND {pl.evidence_filter('')}",
}

HELPER_SHAPES = {
    'short alias': "SELECT MAX(date_iso) FROM sales_transactions "
                   "WHERE customer = ? AND {pl.evidence_filter('')}",
    'full name':   "SELECT MAX(date_iso) FROM sales_transactions "
                   "WHERE customer = ? AND {price_lookup.evidence_filter('s')}",
}

NOT_SITES = {
    'not per customer':     _src('SELECT MAX(date_iso) FROM sales_transactions '
                                 'WHERE date_iso >= ?'),
    'another date column':  _src('SELECT MAX(created_at) FROM sales_transactions '
                                 'WHERE customer = ?'),
    'another table':        _src('SELECT MAX(date_iso) FROM purchase_transactions '
                                 'WHERE supplier = ?'),
    'a line count':         _src('SELECT COUNT(DISTINCT doc_no) FROM sales_transactions '
                                 'WHERE customer = ?'),
    'a SQL line comment':   _src('SELECT 1 FROM sales_transactions WHERE customer = ?\n'
                                 '-- was MAX(date_iso), see #513'),
    'a SQL block comment':  _src('SELECT 1 FROM sales_transactions WHERE customer = ?\n'
                                 '/* was COUNT(DISTINCT doc_base) */'),
    'prose in a docstring': ('def customer_note():\n'
                             '    """Was MAX(date_iso) FROM sales_transactions '
                             'WHERE customer = ?"""\n'
                             '    return 1\n'),
}


@pytest.mark.parametrize('shape', sorted(AGGREGATE_SHAPES))
def test_the_sweep_sees_every_aggregate_shape(shape):
    sql = AGGREGATE_SHAPES[shape]
    src = _src(sql, f_prefix='f' if '{' in sql else '')
    assert _per_function(src, _STALE_AGG) == {'report': 1}, f'{shape}: unswept'


def test_the_sweep_sees_a_scope_passed_in_by_the_caller():
    """The WHERE arrives as {where} from the caller, so the query never names
    a customer column — the function's own name is the only signal."""
    src = _src('SELECT MAX(date_iso) FROM sales_transactions WHERE {where}',
               func='_customer_last_buy', f_prefix='f')
    assert _per_function(src, _STALE_AGG) == {'_customer_last_buy': 1}


@pytest.mark.parametrize('shape', sorted(HELPER_SHAPES))
def test_the_sweep_sees_the_population_however_it_is_spelled(shape):
    src = _src(HELPER_SHAPES[shape], f_prefix='f')
    assert _per_function(src, _HELPER) == {'report': 1}, f'{shape}: unseen'


@pytest.mark.parametrize('shape', ['implicit concatenation', 'plus concatenation',
                                   'module constant', 'method'])
def test_the_sweep_sees_every_string_shape(shape):
    src = {
        'implicit concatenation':
            'def report(conn):\n    return conn.execute("SELECT MAX(date_iso) "\n'
            '        "FROM sales_transactions WHERE customer = ?")\n',
        'plus concatenation':
            'def report(conn):\n    return conn.execute("SELECT MAX(date_iso) " +\n'
            '        "FROM sales_transactions WHERE customer = ?")\n',
        'module constant':
            'Q = "SELECT MAX(date_iso) FROM sales_transactions WHERE customer = ?"\n',
        'method':
            'class Repo:\n    def report(self, conn):\n'
            '        return conn.execute("SELECT MAX(date_iso) FROM sales_transactions '
            'WHERE customer = ?")\n',
    }[shape]
    assert sum(_per_function(src, _STALE_AGG).values()) == 1, f'{shape}: unswept'


@pytest.mark.parametrize('shape', sorted(NOT_SITES))
def test_the_sweep_ignores_what_is_not_a_raw_customer_last_buy(shape):
    assert _per_function(NOT_SITES[shape], _STALE_AGG) == {}, f'{shape}: false positive'


def test_stripping_comments_does_not_eat_the_code_beside_them():
    """CONTROL for the two comment cases above: a stripper that returned '' —
    or one that swallowed the rest of the string from the first `--` — would
    pass them while blinding the whole sweep."""
    src = _src('SELECT MAX(date_iso)   -- not COUNT(DISTINCT doc_base)\n'
               '  FROM sales_transactions WHERE customer = ?')
    assert _per_function(src, _STALE_AGG) == {'report': 1}
