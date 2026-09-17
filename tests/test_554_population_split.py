"""#554 — PRICE evidence and PURCHASE recency are two populations, pinned per
call site.

`ar_writeoffs` answers FOUR questions in this repo and they do not share one
answer:

    revenue          the FLAG only (`excludes_revenue = 1`)  sales_filters
    collectability   the WHOLE table                         cashflow
    price evidence   the WHOLE table                         price_evidence_filter
    purchase recency the FLAG only                            purchase_population_filter

The last two used to be one function (`evidence_filter`), which meant #554's
price fix also moved ซื้อล่าสุด off every written-off bill. Put ruled against
that on 2026-09-17: the goods moved and the shop engaged, we just never got
paid, so dropping the bill makes the data LESS true — and that date decides who
the sales team sees as เงียบ.

This file holds the two halves of the guard:

1. **The assignment census.** Every reference to either predicate, per
   `file::function`, declared in EXPECTED as 'price' or 'purchase' with a
   reason. A parametrized test asserts each site's choice, so repointing one
   fails NAMING that site rather than moving an anonymous count. A
   file-level "this file is known" list would not catch a swap inside an
   approved file, which is the whole failure mode here.

   It reads the **AST**, not the SQL text: the first version matched
   `(?:pl|price_lookup)\.` inside an f-string hole, and review measured three
   reachable shapes it called clean (a third import alias, concatenation
   outside the f-string, and the predicate assigned to a local first). The
   shapes are now fed back in as `_SHAPES_SEEN` / `_SHAPES_UNSEEN`, so the
   claim "a new undeclared reader cannot appear" is measured — with ONE
   recorded blind spot, a `getattr(pl, '<name>')` string lookup, which
   nothing in the app does.

2. **The behavioural pair.** Both predicates are run against ONE fixture
   holding a row on the far side of each clause: an unflagged write-off (kept
   by purchase, dropped by price) and a flagged one (dropped by both), with an
   ordinary bill as the CONTROL in the same run. Then the same shape through
   the real customer-summary code path, which is where the ruling is visible
   (prod 99ส09: ซื้อล่าสุด must stay 2025-03-31, not fall back to 2024-12-10).
"""
import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(_ROOT, 'inventory_app')
SCRIPTS = os.path.join(_ROOT, 'scripts')

# The census reads the AST, not the SQL text. A text matcher has to hard-code
# the import alias (`pl.` / `price_lookup.`) and only sees the predicate when it
# sits inside an f-string hole — review of the first version measured three
# reachable shapes it called clean: a third alias (`import price_lookup as PLX`),
# concatenation OUTSIDE the f-string (`"… WHERE " + price_lookup.price_…(…)`),
# and the predicate assigned to a local first (`pred = …` then `f"… {pred}"`).
# An AST walk over every reference to either NAME sees all of them, and cannot
# see prose (a docstring or a comment is not an AST reference) — which is what
# the text matcher was really buying. `_SHAPES_SEEN` / `_SHAPES_UNSEEN` below
# feed those exact shapes back in, so this claim is measured rather than argued.
_NAMES = {'price_evidence_filter': 'price',
          'purchase_population_filter': 'purchase'}
_OLD_NAME = 'evidence_filter'   # pre-#554; must read as NEITHER half

# site -> (which, reason). 'price' = the WHOLE ar_writeoffs table is excluded;
# 'purchase' = only the flagged rows are. A site holding BOTH appears twice,
# as '<site> [price]' and '<site> [purchase]', because one function can
# legitimately ask both questions (and one does).
EXPECTED = {
    # ── PRICE: "is this a price someone agreed to?" ──────────────────────────
    'price_lookup.py::latest_evidence': ('price',
        "the customer's own last-paid price — the answer /quote-customer "
        'prints. A ฿1.00 written-off line (prod IV6801241) was admissible '
        'here until #554.'),
    'price_lookup.py::_evidence_rows': ('price',
        "the R4 window's bill population: n_bills, context.lowest, the "
        'promo-stale check. lowest is shown to Put as "ต่ำสุดที่ใครเคยได้".'),
    'price_lookup.py::_customer_context': ('price',
        "R7: this customer's typical discount % and product count over 12 "
        'months — a price statistic, not a purchase count.'),
    'price_lookup.py::resolve_price': ('price',
        'the pre-epoch informational lookup (the 3 bills just before the '
        'price regime changed), rendered as prices.'),
    'peer_pricing.py::product_peer_prices': ('price',
        'what OTHER shops paid for the same product. Its own docstring '
        'requires the SAME population resolve_price uses, or the card and '
        'the resolver disagree about which bills exist.'),
    'models/customers.py::_customer_product_cards': ('price',
        "ซื้อบ่อย's per-card LAST BILL, whose net/qty is rendered to a rep as "
        'a price. Same function also holds a purchase site (below) — the '
        'count and the price answer different questions on purpose.'),
    'scripts/price_lookup_cli.py::_latest_cash_per_piece': ('price',
        "the CLI's latest-B2B-cash-per-piece figure, used by the sibling "
        'price hint /quote-customer prints. Named by this census rather than '
        'guessed: the first draft declared `_family_hint`, the CALLER.'),

    # ── PURCHASE: "did this shop buy from us, and when?" (Put, 2026-09-17) ──
    'models/customers.py::_customer_sales_aggregates': ('purchase',
        'the customer page ซื้อล่าสุด + จำนวนครั้งซื้อ pair. A written-off bill '
        'is still a purchase; moving this date backwards is exactly what Put '
        'ruled against (prod 99ส09, 2025-03-31 -> 2024-12-10).'),
    'models/customers.py::get_customers': ('purchase',
        'the /customers list ซื้อล่าสุด column. Must match the detail page '
        'above or the two disagree one click apart.'),
    'models/customers.py::_customer_product_cards ': ('purchase',
        "ซื้อบ่อย's times_bought / total_qty / total_net per (product, unit) — "
        'ครั้งที่ซื้อ, a purchase count. (Trailing space in the key: this '
        'function is listed twice, see the price entry above.)'),
    'models/customers.py::_cross_sell_suggestions': ('purchase',
        'เสนอเพิ่ม (#498): how many OTHER shops bought a product, and whether '
        'THIS shop ever did. All THREE sites in this function must agree, or '
        'the self-exclusion stops matching the counting and a shop gets its '
        'own product suggested back to it.'),
    'call_card.py::get_call_list': ('purchase',
        "/call's ซื้อล่าสุด column and the เงียบ badge — the worklist that "
        'decides who a rep visits.'),
    'blueprints/mobile.py::sales_trip': ('purchase',
        "the mobile trip row's ล่าสุด, the same question as its desktop twin "
        '(#513).'),
    'winback.py::compute_winback': ('purchase',
        'ครั้งที่ซื้อ >= 3 and the median gap between purchase DATES. A bill '
        'that was never paid is still a purchase this shop made.'),
}

# Sites that legitimately hold more than one call of the same predicate.
EXPECTED_COUNTS = {
    'models/customers.py::_cross_sell_suggestions': 3,   # rank + 2 self-exclusions
    'price_lookup.py::_evidence_rows': 1,
    'price_lookup.py::resolve_price': 1,
}


def _references(src):
    """(function qualname, 'price'|'purchase') for every REFERENCE to either
    predicate name in `src`, whatever shape the call site takes.

    A reference, not strictly a call: `pred = pl.purchase_population_filter`
    followed by `f"… {pred('s')}"` is a reader too, and the Call node there
    names `pred`, not the predicate. Matching the NAME covers both.

    Invisible to this, on purpose: a docstring, a `#` comment and a SQL
    comment are not AST references, so the reasons in EXPECTED above can name
    both predicates in prose without counting as call sites (the #469/#471
    docstring trap). Also invisible, and accepted: `getattr(pl, 'price_…')`
    — a string, not a reference. Nothing in the app does that; the harness in
    `_SHAPES_UNSEEN` records it as a known blind spot rather than pretending.
    """
    out = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope + [child.name])
                continue
            name = None
            if isinstance(child, ast.Attribute):
                name = child.attr
            elif isinstance(child, ast.Name):
                name = child.id
            if name in _NAMES:
                out.append(('.'.join(scope) or '<module>', _NAMES[name]))
            visit(child, scope)

    visit(ast.parse(src), [])
    return out


def _py_files():
    for base, prefix in ((APP, ''), (SCRIPTS, 'scripts/')):
        for root, _dirs, names in os.walk(base):
            if any(part in root for part in ('__pycache__', 'instance', 'static')):
                continue
            for n in names:
                if n.endswith('.py'):
                    yield (prefix + os.path.relpath(os.path.join(root, n), base)
                           .replace(os.sep, '/'), os.path.join(root, n))


def _census():
    """{site: {'price': n, 'purchase': n}} over the whole app + scripts."""
    found = {}
    for rel, path in _py_files():
        with open(path, encoding='utf-8') as f:
            src = f.read()
        for func, which in _references(src):
            site = found.setdefault(f'{rel}::{func}', {'price': 0, 'purchase': 0})
            site[which] += 1
    return found


def _expected_for(site):
    """The declared choice(s) for a site, collapsing the two-entry form."""
    out = set()
    for key, (which, _why) in EXPECTED.items():
        if key.strip() == site:
            out.add(which)
    return out


# ── 1. the assignment census ────────────────────────────────────────────────

@pytest.mark.parametrize('site', sorted({k.strip() for k in EXPECTED}))
def test_each_call_site_reads_the_predicate_it_is_declared_with(site):
    """Names the site. Repointing a purchase consumer at the price predicate
    (or the reverse) fails HERE, saying which one moved — the census in
    test_last_purchase_population_coverage.py counts either name as "the
    population" and would stay green."""
    found = _census().get(site)
    assert found is not None, f'{site} no longer calls either #554 predicate'
    declared = _expected_for(site)
    got = {which for which in ('price', 'purchase') if found[which]}
    assert got == declared, (
        f'{site} reads {sorted(got) or "neither"}, declared {sorted(declared)}.\n'
        'Either the site changed question (say so in EXPECTED with a reason), '
        'or the two predicates just got collapsed back into one — which is what '
        "#554 split apart: price evidence drops the whole ar_writeoffs table, "
        'purchase recency drops only the flagged rows (Put, 2026-09-17).')


def test_every_site_found_in_the_app_is_declared():
    """The other direction: a NEW call site cannot appear undeclared. A
    file-level allowlist would answer "is this file known"; this answers "is
    this SITE's question declared"."""
    found = _census()
    undeclared = sorted(set(found) - {k.strip() for k in EXPECTED})
    assert not undeclared, (
        'New reader(s) of a #554 population, not declared in EXPECTED: '
        f'{undeclared}. Say which question each one asks, and why.')


@pytest.mark.parametrize('site,n', sorted(EXPECTED_COUNTS.items()))
def test_multi_call_sites_keep_every_call(site, n):
    """_cross_sell_suggestions holds THREE calls that must all be the purchase
    predicate; losing one silently un-filters a sub-select rather than
    changing a name."""
    found = _census()[site]
    assert found['price'] + found['purchase'] == n, (
        f'{site}: {found}, expected {n} call(s) in total')


def test_both_halves_of_the_split_are_actually_used():
    """CONTROL for the two tests above: a matcher that found nothing, or a
    world where one predicate has no readers, would satisfy them just as
    well (every `got` would be empty and every `declared` would have to be
    wrong to notice)."""
    found = _census()
    price = {s for s, n in found.items() if n['price']}
    purchase = {s for s, n in found.items() if n['purchase']}
    assert len(price) >= 5 and len(purchase) >= 5, (price, purchase)
    assert price & purchase == {'models/customers.py::_customer_product_cards'}, (
        'exactly one function is supposed to ask BOTH questions '
        f'(ซื้อบ่อย: a count and a price); found {sorted(price & purchase)}')


# ── the census's own coverage: one rogue source per shape ───────────────────
#
# A sweep is only worth what its matcher can see, and READING it never shows a
# shape it misses (`.claude/rules/verification-discipline.md`, "a cross-cutting
# sweep you wrote yourself is the next thing to distrust"). Every entry below
# is a shape that either exists in this app or was measured green against the
# first, text-matching version of this census. Prior art and the three
# concatenation/alias shapes: test_last_purchase_population_coverage.py's
# HELPER_SHAPES.

_SHAPES_SEEN = {
    'f-string hole, pl alias':
        "def report(c):\n    return c.execute(f\"WHERE {pl.price_evidence_filter('st')}\")\n",
    'f-string hole, full module name':
        "def report(c):\n"
        "    return c.execute(f\"WHERE {price_lookup.price_evidence_filter('s')}\")\n",
    # ⬇ the three the text matcher called CLEAN (measured, review of #562)
    'f-string hole, a THIRD import alias':
        "def report(c):\n    return c.execute(f\"WHERE {PLX.price_evidence_filter('st')}\")\n",
    'plus concatenation OUTSIDE the f-string':
        "def report(c):\n"
        "    return c.execute(\"SELECT 1 WHERE \" + price_lookup.price_evidence_filter('st'))\n",
    'assigned to a local first':
        "def report(c):\n"
        "    pred = price_lookup.price_evidence_filter('st')\n"
        "    return c.execute(f\"SELECT 1 WHERE {pred}\")\n",
    # ...and the same three for the purchase half, plus the shapes in the app
    'bare name (inside price_lookup.py itself)':
        "def report(c):\n    return c.execute(f\"WHERE {purchase_population_filter('')}\")\n",
    'aliased WITHOUT calling, called later':
        "def report(c):\n"
        "    pred = pl.purchase_population_filter\n"
        "    return c.execute(f\"WHERE {pred('s')}\")\n",
    '.format() instead of an f-string':
        "def report(c):\n"
        "    return c.execute('WHERE {}'.format(pl.purchase_population_filter('s2')))\n",
    'module-level constant':
        "Q = f\"SELECT 1 WHERE {pl.purchase_population_filter('')}\"\n",
}

_SHAPES_UNSEEN = {
    # Prose. The #469/#471 trap: EXPECTED's own reasons name both predicates.
    'a docstring naming the predicate':
        "def report(c):\n"
        "    \"\"\"Was pl.price_evidence_filter(\'s\') before #554.\"\"\"\n"
        "    return c.execute('SELECT 1')\n",
    'a python comment':
        "def report(c):\n"
        "    # was pl.purchase_population_filter('s')\n"
        "    return c.execute('SELECT 1')\n",
    'a SQL comment inside the query':
        "def report(c):\n"
        "    return c.execute('SELECT 1 -- pl.price_evidence_filter(x)')\n",
    # The pre-#554 name must read as NEITHER half, or a half-finished rename
    # looks declared.
    'the old evidence_filter name, as a real call':
        "def report(c):\n    return c.execute(f\"WHERE {pl.evidence_filter('s')}\")\n",
    # KNOWN BLIND SPOT, recorded rather than hidden: a string-built lookup.
    # Nothing in the app does this; if something ever does, this census will
    # call it clean and only the behavioural tests would notice.
    'getattr by string (accepted blind spot)':
        "def report(c):\n"
        "    pred = getattr(pl, 'price_evidence_filter')\n"
        "    return c.execute(f\"WHERE {pred('s')}\")\n",
}


@pytest.mark.parametrize('shape', sorted(_SHAPES_SEEN))
def test_the_census_sees_every_call_site_shape(shape):
    refs = _references(_SHAPES_SEEN[shape])
    assert refs, f'{shape}: UNSEEN — a new undeclared reader in this shape would pass'


@pytest.mark.parametrize('shape', sorted(_SHAPES_UNSEEN))
def test_the_census_ignores_prose_and_the_old_name(shape):
    """CONTROL for the battery above: a matcher that fired on everything would
    satisfy every `_SHAPES_SEEN` case just as well."""
    src = _SHAPES_UNSEEN[shape]
    assert not _references(src), f'{shape}: counted as a call site'
    # ...and prove the snippet is real code the walker did parse, so a
    # SyntaxError cannot masquerade as "correctly ignored".
    assert ast.parse(src).body, shape


def test_the_old_name_is_gone_from_the_app_entirely():
    """#554 deleted `evidence_filter` rather than aliasing it. A leftover
    reference would mean a call site nobody classified — and this census
    deliberately cannot see the old name, so nothing else would catch it."""
    leftover = []
    for rel, path in _py_files():
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            name = (node.attr if isinstance(node, ast.Attribute) else
                    node.id if isinstance(node, ast.Name) else None)
            if name == _OLD_NAME:
                leftover.append(f'{rel}:{node.lineno}')
    assert not leftover, (
        f'`{_OLD_NAME}` still referenced in app code: {leftover}. It was split '
        'into price_evidence_filter / purchase_population_filter (#554).')


# ── 2. the behavioural pair ─────────────────────────────────────────────────

CODE = 'TEST554'
NAME = 'ลูกค้าทดสอบ 554'
_pid = [554000]


def _mk_product(conn):
    _pid[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, "
        "cost_price, brand_id, is_active) VALUES (?, 'ตัว', 1000.0, 600.0, 3, 1)",
        (f'สินค้าทดสอบ 554 #{_pid[0]}',))
    conn.commit()
    return cur.lastrowid


def _bill(conn, *, pid, doc_base, date_iso, net, suffix=1):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,1,'ตัว',?,1,?,?)",
        (date_iso, f'{doc_base}-{suffix}', doc_base, pid, NAME, CODE, net, net, net))
    conn.commit()


def _writeoff(conn, doc_base, excludes_revenue):
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, "
        "writeoff_date, excludes_revenue) VALUES (?,?,1,'writeback','2026-01-01',?)",
        (doc_base, CODE, excludes_revenue))
    conn.commit()


@pytest.fixture
def three_bills(tmp_db_conn):
    """ONE fixture, three bills, each on the far side of a different clause.
    Forced state, never inherited: tmp_db_conn clones the live dev DB WITH its
    rows (verification-discipline.md), so this clears the key first.

    Shape copied from prod 99ส09, the customer #554's first cut moved: its
    most recent bill IS a written-off-but-unflagged one.
    """
    c = tmp_db_conn
    c.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
              (CODE, NAME))
    c.execute("DELETE FROM ar_writeoffs WHERE customer_code = ?", (CODE,))
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
              "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (CODE, NAME))
    c.commit()
    pid = _mk_product(c)
    # newest: written off, NOT flagged -> a purchase, but not price evidence
    _bill(c, pid=pid, doc_base='IV5540001', date_iso='2025-03-31', net=1.0)
    _writeoff(c, 'IV5540001', excludes_revenue=0)
    # older: an ordinary bill -> the CONTROL, in both populations
    _bill(c, pid=pid, doc_base='IV5540002', date_iso='2024-12-10', net=900.0)
    # newest of all: flagged "invoiced in error" -> in NEITHER population
    _bill(c, pid=pid, doc_base='IV5540003', date_iso='2025-06-30', net=500.0)
    _writeoff(c, 'IV5540003', excludes_revenue=1)
    yield c, pid
    c.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (CODE,))
    c.execute("DELETE FROM ar_writeoffs WHERE customer_code = ?", (CODE,))
    c.commit()


def _docs(conn, pid, predicate):
    rows = conn.execute(
        f"SELECT st.doc_base FROM sales_transactions st "
        f"WHERE st.product_id = ? AND {predicate('st')}", (pid,)).fetchall()
    return sorted(r['doc_base'] for r in rows)


def test_the_two_predicates_disagree_on_exactly_the_unflagged_writeoff(three_bills):
    """The whole split, in one assertion pair on one fixture.

    Both lists are asserted by identity (not "is X absent"), so an empty
    result — the fixture never arriving — fails instead of reading as a
    working guard, and the ordinary bill is the CONTROL present in both.
    """
    conn, pid = three_bills
    import price_lookup as pl

    assert _docs(conn, pid, pl.purchase_population_filter) == ['IV5540001', 'IV5540002'], \
        'the purchase population must KEEP an unflagged write-off (Put, 2026-09-17)'
    assert _docs(conn, pid, pl.price_evidence_filter) == ['IV5540002'], \
        'the price population must DROP every written-off document (#554)'


def test_the_flagged_writeoff_is_in_neither_population(three_bills):
    """`excludes_revenue = 1` means no sale ever happened — it is out of both,
    and it is the NEWEST row here, so a predicate that admitted it would show
    2025-06-30 as the last purchase."""
    conn, pid = three_bills
    import price_lookup as pl
    for predicate in (pl.purchase_population_filter, pl.price_evidence_filter):
        assert 'IV5540003' not in _docs(conn, pid, predicate), predicate.__name__


def test_revenue_filter_is_untouched_by_the_split(three_bills):
    """#554 must not have redefined revenue. The unflagged write-off is still
    revenue (bad debt WAS a sale); the flagged one is not."""
    conn, _pid = three_bills
    import sales_filters
    kept = sorted(r['doc_base'] for r in conn.execute(
        f"SELECT DISTINCT st.doc_base FROM sales_transactions st "
        f"WHERE st.customer_code = ? AND {sales_filters.revenue_filter('st')}",
        (CODE,)).fetchall())
    assert kept == ['IV5540001', 'IV5540002']


def test_ar_writeoffs_doc_no_is_not_nullable(tmp_db_conn):
    """LOAD-BEARING for the price half (sales_filters.py, migration 095): one
    NULL doc_no makes `NOT IN (SELECT doc_no FROM ar_writeoffs)` evaluate to
    NULL for every row, silently re-admitting the entire table.

    Read off the tmp clone of the real dev DB, not a bare get_connection():
    the first draft did the latter and got a DB with no ar_writeoffs table at
    all, which a `.get('doc_no')` would have read as "not nullable, fine".
    """
    cols = {r['name']: r['notnull']
            for r in tmp_db_conn.execute("PRAGMA table_info(ar_writeoffs)")}
    assert cols, 'no ar_writeoffs table in the clone — this check never ran'
    assert cols['doc_no'] == 1


def test_customer_summary_keeps_the_writeoff_as_the_last_purchase(three_bills):
    """The ruling, through the real code path — `models.get_customer_summary_
    by_code`, what the customer page and the /customers list render.

    This is the prod 99ส09 shape: its newest bill is a written-off-unflagged
    one, and #554's first cut moved ซื้อล่าสุด from 2025-03-31 back to
    2024-12-10. It must stay on the write-off.
    """
    conn, _pid = three_bills
    import models

    data = models.get_customer_summary_by_code(CODE)
    summary = data['summary']
    assert summary['last_purchase_date'] == '2025-03-31', (
        'ซื้อล่าสุด fell off the written-off bill — the purchase population '
        'must keep it (Put, 2026-09-17)')
    # CONTROL, same run: the count agrees with that date (both come from ONE
    # query on purpose) and the flagged document is in NEITHER figure.
    assert summary['purchase_doc_count'] == 2
    # and the price side, on the same rows, answers the EARLIER bill — the two
    # questions really do land on different documents here.
    import price_lookup as pl
    last = pl.latest_evidence(conn, _pid, CODE, '', today='2026-09-17')
    assert last is not None and last['doc_no'].startswith('IV5540002'), last
