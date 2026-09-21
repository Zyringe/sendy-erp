"""TDD — ต้นทุนขาย is read at ทุน ณ วันขาย from 2026-03-03 (#593 stories 1-7 + 15).

ADR 0015: `/accounting` costed every sales line at `products.cost_price`, the
product's CURRENT weighted average, so a closed month's ต้นทุนขาย moved
whenever any cost was recalculated (measured 2026-09-18: ฿289.46 between two
page loads on one morning, no sale and no purchase involved). From
**2026-03-03** a line is costed at the carrying amount on its own sale date,
read from `product_cost_ledger.wacc_after`. Before that date the ledger cannot
reach — its `INITIAL` rows reset every product on 2026-03-03 — so those lines
keep ทุนเฉลี่ยวันนี้ and the page says so.

Three rules the plain reading does NOT give you, each measured on prod
2026-09-20 and each with its own case below:

  * **Clamp.** 871 products never get an `INITIAL` row (`wacc.py` only writes
    one when `opening_cost > 0`), so a post-cutover sale can resolve straight
    back to a 2024 `PURCHASE` row — the basis ADR 0015 says it discarded.
    51 lines on prod. The lookup is floored at the cutover.
  * **Sentinel.** `wacc_after = 0` is not a cost. When stock goes negative the
    walk freezes the running average at 0 and the write guard deliberately
    leaves `cost_price` alone, so for that class the ledger is the worse
    source (prod pid 714: ledger 0.0 against cost_price 7.0; 182 pre-cutover
    rows carry 0). Put's ruling 2026-09-20: use the recorded cost, because the
    goods that left the warehouse had one. Worth +฿336.00 of the +฿2,212.69.
  * **Fallback, disclosed.** A line the ledger cannot answer keeps
    `cost_price` and is counted, never costed at zero — zeroing it would trade
    a ฿1,876 problem for a ฿3,080 one. ⚠ ADR 0015's "their cost is ฿0
    regardless" is measurably false and is corrected in that file.

Fixture: `empty_db_conn` (full live schema, zero rows) — never `tmp_db`, whose
real cost-ledger and cashbook rows would leak into every assertion.
"""
import sqlite3

import pytest

import models
import sales_filters


CUT = sales_filters.COGS_HISTORICAL_FROM          # '2026-03-03'
AFTER, BEFORE = '2026-05-10', '2026-02-10'


# ── seed helpers ─────────────────────────────────────────────────────────────

def _mk_product(conn, cost_price, unit_type='ตัว', brand_id=None):
    return conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, brand_id) "
        "VALUES ('t', ?, ?, ?)", (unit_type, cost_price, brand_id)).lastrowid


def _mk_ledger(conn, product_id, event_date, wacc_after, event_type='PURCHASE'):
    return conn.execute(
        """INSERT INTO product_cost_ledger
             (product_id, event_type, event_date, qty_change, unit_cost,
              stock_after, wacc_after)
           VALUES (?, ?, ?, 1, ?, 1, ?)""",
        (product_id, event_type, event_date, wacc_after, wacc_after)).lastrowid


def _mk_sale(conn, date_iso, doc_no, product_id, qty, net, unit='ตัว'):
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, doc_base, product_id, qty, unit, unit_price, net, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (date_iso, doc_no, doc_no, product_id, qty, unit, net / qty, net, net))


def _summary(conn, a='2026-05-01', b='2026-05-31'):
    conn.commit()
    return models.get_accounting_summary(a, b)


# ── story 2: the cost is the one that stood on the sale date ─────────────────

def test_post_cutover_line_costs_at_the_ledger_not_at_cost_price(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=3, net=500.0)

    s = _summary(conn)

    assert s['line_count'] == 1                      # control: the row is in scope
    assert s['cogs'] == pytest.approx(30.0)          # 3 x 10, not 3 x 99
    assert s['no_ledger_lines'] == 0


def test_a_later_ledger_row_does_not_reach_back_into_an_earlier_sale(empty_db_conn):
    """The whole point: a purchase AFTER the sale cannot change that sale."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_ledger(conn, pid, '2026-06-01', wacc_after=77.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=3, net=500.0)

    assert _summary(conn)['cogs'] == pytest.approx(30.0)


def test_same_date_rows_take_the_days_closing_cost(empty_db_conn):
    """Ties break on id, which `wacc.py` assigns in walk order, so the last row
    of a date is that day's closing WACC after all its INs. Byte-identical to
    `wacc.get_current_wacc`'s own ORDER BY."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=20.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=1, net=500.0)

    assert _summary(conn)['cogs'] == pytest.approx(20.0)


# ── story 1 / 6: a closed month stops tracking cost_price ────────────────────

def test_changing_cost_price_does_not_move_a_closed_month(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=4, net=500.0)
    before = _summary(conn)['cogs']

    conn.execute("UPDATE products SET cost_price = 250.0 WHERE id = ?", (pid,))

    assert _summary(conn)['cogs'] == pytest.approx(before)
    assert before == pytest.approx(40.0)


# ── the cutover, both sides ──────────────────────────────────────────────────

def test_pre_cutover_line_keeps_cost_price_even_when_a_ledger_row_exists(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-01-05', wacc_after=10.0)
    _mk_sale(conn, BEFORE, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn, '2026-02-01', '2026-02-28')
    assert s['cogs'] == pytest.approx(198.0)         # 2 x 99, the ledger is ignored
    assert s['no_ledger_lines'] == 0                 # pre-cutover is not "no ledger"


def test_post_cutover_line_never_reads_a_pre_cutover_row(empty_db_conn):
    """Clamp. ADR 0015 discarded the pre-2026-03-03 basis, so a product with no
    INITIAL row falls back to cost_price and is DISCLOSED, not costed off a
    2024 average."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-01-05', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn)
    assert s['cogs'] == pytest.approx(198.0)
    assert s['no_ledger_lines'] == 1


def test_a_row_dated_exactly_on_the_cutover_is_usable(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn)
    assert s['cogs'] == pytest.approx(20.0)
    assert s['no_ledger_lines'] == 0


# ── Put's ruling 2026-09-20: a frozen zero is a sentinel, not a cost ─────────

def test_zero_wacc_falls_back_to_the_recorded_cost(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=7.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=0.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=3, net=500.0)

    s = _summary(conn)
    assert s['cogs'] == pytest.approx(21.0)          # 3 x 7, not 3 x 0
    assert s['no_ledger_lines'] == 1
    assert s['zero_cost_lines'] == 0


def test_a_zero_row_does_not_hide_an_earlier_real_one(empty_db_conn):
    """Skipping the sentinel must reach past it, not give up at it."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, '2026-03-10', wacc_after=10.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=0.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn)
    assert s['cogs'] == pytest.approx(20.0)
    assert s['no_ledger_lines'] == 0


# ── disclosure counts (stories 4 and 5) ──────────────────────────────────────

def test_zero_cost_lines_follows_the_resolved_cost(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=0.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn)
    assert s['zero_cost_lines'] == 1
    assert s['cogs'] == pytest.approx(0.0)


def test_an_unmapped_line_is_not_counted_as_a_missing_ledger(empty_db_conn):
    """An unmapped line has no product to have a ledger for. It belongs to
    no_cost_lines alone, or the page shows 2 counts for 1 line."""
    conn = empty_db_conn
    _mk_sale(conn, AFTER, 'IV1-1', None, qty=2, net=500.0)

    s = _summary(conn)
    assert s['no_cost_lines'] == 1
    assert s['no_ledger_lines'] == 0


def test_unknown_ratio_disclosure_survives_the_basis_change(empty_db_conn):
    """Story 5: this change must not quietly drop a warning already relied on."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0, unit_type='ตัว')
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0, unit='โหล')

    s = _summary(conn)
    assert s['unknown_ratio_lines'] == 1
    assert s['cogs'] == pytest.approx(20.0)          # ratio fell back to 1


def test_the_bill_unit_is_still_converted_on_the_new_basis(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0, unit_type='ตัว')
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) "
                 "VALUES (?, 'โหล', 12.0)", (pid,))
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=2, net=500.0, unit='โหล')

    assert _summary(conn)['cogs'] == pytest.approx(240.0)   # 2 x 12 x 10


# ── story 15: each month says which basis it used ────────────────────────────

def test_a_wholly_post_cutover_month_reads_historical(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=1, net=500.0)

    s = _summary(conn)
    assert s['cogs_basis'] == 'historical'
    assert [m['ym'] for m in s['cogs_basis_months']] == ['2026-05']
    assert s['cogs_basis_months'][0]['basis'] == 'historical'


def test_a_wholly_pre_cutover_month_reads_current(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0)
    _mk_sale(conn, BEFORE, 'IV1-1', pid, qty=1, net=500.0)

    s = _summary(conn, '2026-02-01', '2026-02-28')
    assert s['cogs_basis'] == 'current'
    assert s['cogs_basis_months'][0]['basis'] == 'current'


def test_march_holds_both_bases_and_splits_its_number(empty_db_conn):
    """2026-03 straddles the cutover on prod (11 lines / ฿6,450.30 of revenue
    on 2026-03-02). Labelling it either way is the silent rewrite ADR 0015
    cites ม.39 against, so it reads `mixed` and the number comes apart."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=100.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, '2026-03-02', 'IV1-1', pid, qty=1, net=500.0)   # pre-cutover
    _mk_sale(conn, '2026-03-10', 'IV2-1', pid, qty=1, net=500.0)   # post-cutover

    s = _summary(conn, '2026-03-01', '2026-03-31')
    m = s['cogs_basis_months'][0]
    assert s['cogs_basis'] == 'mixed'
    assert m['basis'] == 'mixed'
    assert m['cogs_current'] == pytest.approx(100.0)
    assert m['cogs_historical'] == pytest.approx(10.0)
    assert m['cogs'] == pytest.approx(110.0)
    assert s['cogs'] == pytest.approx(110.0)


def test_a_period_spanning_both_kinds_of_month_reads_mixed(empty_db_conn):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=100.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, BEFORE, 'IV1-1', pid, qty=1, net=500.0)
    _mk_sale(conn, AFTER, 'IV2-1', pid, qty=1, net=500.0)

    s = _summary(conn, '2026-02-01', '2026-05-31')
    assert s['cogs_basis'] == 'mixed'
    assert [(m['ym'], m['basis']) for m in s['cogs_basis_months']] == [
        ('2026-02', 'current'), ('2026-05', 'historical')]


def test_a_window_with_no_sales_has_no_basis_rather_than_a_vacuous_one(empty_db_conn):
    """`all()` over an empty set is true for BOTH states, so the branch order
    would otherwise decide this silently."""
    s = _summary(empty_db_conn, '2026-05-01', '2026-05-31')
    assert s['cogs_basis'] is None
    assert s['cogs_basis_months'] == []


# ── the brand panel reads the same basis as the headline ─────────────────────

def test_brand_breakdown_uses_the_same_basis(empty_db_conn):
    conn = empty_db_conn
    bid = conn.execute(
        "INSERT INTO brands (code, name, name_th) VALUES ('X','X','X')").lastrowid
    pid = _mk_product(conn, cost_price=99.0, brand_id=bid)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=3, net=500.0)

    s = _summary(conn)
    assert len(s['brand_breakdown']) == 1             # control
    assert s['brand_breakdown'][0]['cogs_approx'] == pytest.approx(30.0)


def test_a_sale_dated_exactly_on_the_cutover_is_on_the_new_basis(empty_db_conn):
    """The cutover is inclusive on the SALE side too. Nothing pinned this until
    a mutation of the boundary came back green."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=99.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, CUT, 'IV1-1', pid, qty=2, net=500.0)

    s = _summary(conn, '2026-03-01', '2026-03-31')
    assert s['cogs'] == pytest.approx(20.0)
    assert s['cogs_basis'] == 'historical'


# ── the page says it, not just the reader ────────────────────────────────────

@pytest.fixture
def admin_client_empty(empty_db):
    import app as app_module
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    c = app_module.app.test_client()
    with c.session_transaction() as sess:
        sess['role'] = 'admin'
        sess['username'] = 'admin'
        sess['user_id'] = 1
    return c


def _element(html, elem_id):
    """The open tag with that id through its matching close, by depth.

    A bare substring check over the whole page is satisfied by text elsewhere,
    and Thai substrings nest — `ทุน ณ วันขาย` sits inside the assumptions
    footer on every render.
    """
    import re
    m = re.search(r'<(\w+)[^>]*id="%s"' % re.escape(elem_id), html)
    if not m:
        return None
    tag, i, depth = m.group(1), m.start(), 0
    for t in re.finditer(r'<(/?)%s\b[^>]*?(/?)>' % tag, html[i:]):
        if t.group(2) == '/':
            continue
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[i:i + t.end()]
    return html[i:]


def _render(conn, client, a, b):
    conn.commit()
    r = client.get('/accounting?date_from=%s&date_to=%s' % (a, b))
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_a_mixed_period_renders_its_per_month_split(empty_db_conn, admin_client_empty):
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=100.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, '2026-03-02', 'IV1-1', pid, qty=1, net=500.0)
    _mk_sale(conn, '2026-03-10', 'IV2-1', pid, qty=1, net=500.0)

    block = _element(_render(conn, admin_client_empty, '2026-03-01', '2026-03-31'),
                     'cogs-basis-by-month')

    assert block is not None, 'the mixed-basis block did not render'
    assert '2026-03' in block
    assert 'ทุน ณ วันขาย' in block and 'ทุนเฉลี่ยวันนี้' in block


def test_a_single_basis_period_does_not_render_the_split(empty_db_conn, admin_client_empty):
    """Paired negative: the block must be ABSENT here, and the control above
    proves the assertion is capable of finding it when it is present."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=100.0)
    _mk_ledger(conn, pid, '2026-04-01', wacc_after=10.0)
    _mk_sale(conn, AFTER, 'IV1-1', pid, qty=1, net=500.0)

    html = _render(conn, admin_client_empty, '2026-05-01', '2026-05-31')

    assert _element(html, 'cogs-basis-by-month') is None
    assert '10.00' in html            # control: the page really did render COGS
