"""TDD for #498 — เสนอเพิ่ม / ขายดีที่ร้านนี้ยังไม่มี.

The customer page suggests up to 10 in-stock, priced products that many
OTHER B2B shops buy in the trailing 24 months but this shop has never
bought (all-time), one row per `products.sub_category`. See the issue's
Agent Brief (`gh issue view 498 --repo Zyringe/sendy-erp --comments`) for
the full spec; this file exercises the helper directly
(`models.customers._cross_sell_suggestions`), the same way
tests/test_497_winback.py exercises `winback.compute_winback` directly —
independent of how `get_customer_summary_by_code` wires it in (see
test_498_customer_page_suggestions.py for that seam).

Population = `price_lookup.evidence_filter`, reused, never re-derived.
Shop key = the call list's own canonical key
(COALESCE(NULLIF(TRIM(customer_code),''), customer)).

`tmp_db_conn` clones the LIVE dev DB WITH its data (hundreds of real
products, real B2B shop history) — every assertion here must be immune to
that noise, not merely hope it doesn't collide. Two techniques do that:
(1) every fixture PRODUCT is a fresh INSERT, so no pre-existing
sales_transactions row can reference it; (2) `_window_dates()` below picks
`today` far enough past the real DB's own latest date that the
trailing-24-month window can never admit a single real row, so the
ranking/limit/tie-break tests are not swamped by real popular products.

Prior art for fixtures: tests/test_497_winback.py, tests/test_493_slice2_product_card.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import datetime as dt

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST4980'
TEST_NAME = 'ลูกค้าทดสอบ 498 เสนอเพิ่ม'

_pid_counter = [498000]


def _mk_product(conn, name='สินค้าทดสอบ 498', unit_type='ตัว', base=100.0, cost=60.0,
                 sub_category=None, is_active=1):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active, sub_category) VALUES (?,?,?,?,?,?,?)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID, is_active,
         sub_category),
    )
    conn.commit()
    return cur.lastrowid


def _set_stock(conn, pid, qty):
    conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) VALUES (?, ?) "
        "ON CONFLICT(product_id) DO UPDATE SET quantity = excluded.quantity",
        (pid, qty),
    )
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, code, name=None, qty=1,
          unit_price=100, net=100, vat_type=0, unit='ตัว', ref_invoice=None,
          total=None):
    name = name or code
    doc_no = f"{doc_base}-{suffix}"
    if total is None:
        total = net
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, name, code, qty, unit, unit_price,
         vat_type, total, net, ref_invoice),
    )
    conn.commit()


def _writeoff(conn, doc_no, code, excludes_revenue=1):
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, writeoff_date, "
        "excludes_revenue) VALUES (?,?,0,'writeback','2026-01-01',?)",
        (doc_no, code, excludes_revenue),
    )
    conn.commit()


def _clear_customer(conn, code=TEST_CODE):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (code,))
    conn.execute("DELETE FROM ar_writeoffs WHERE customer_code = ?", (code,))
    conn.commit()


def _window_dates(conn):
    """(today_iso, recent_iso, stale_iso) — see module docstring. `today` is
    800 days past the live DB's own latest sale (comfortably more than the
    730-day window past it, so NO real row can ever qualify); `recent` sits
    inside the resulting window; `stale` is the real DB's own latest date —
    comfortably OUTSIDE the 730-day window measured back from `today`."""
    max_date = conn.execute("SELECT MAX(date_iso) FROM sales_transactions").fetchone()[0]
    max_d = dt.date.fromisoformat(max_date or '2020-01-01')
    today_d = max_d + dt.timedelta(days=800)
    recent_d = today_d - dt.timedelta(days=30)
    return today_d.isoformat(), recent_d.isoformat(), max_d.isoformat()


def _other_shops(conn, pid, n, *, prefix, date_iso, unit_price=100, net=100,
                  unit='ตัว', start=0):
    """`n` distinct OTHER shops (no customers-master row needed — the
    helper never joins customers), each with ONE evidenced invoice for
    `pid`. Distinct doc_base per shop so shop_count == doc_count in the
    simple case (tests that need doc_count to diverge build their own
    fixture)."""
    for i in range(start, start + n):
        code = f'{prefix}{i:03d}'
        _line(conn, doc_base=f'IV{prefix}{i:03d}', suffix=1, pid=pid, date_iso=date_iso,
              code=code, unit_price=unit_price, net=net, unit=unit)


@pytest.fixture
def cust(tmp_db_conn):
    conn = tmp_db_conn
    _clear_customer(conn)
    yield conn
    _clear_customer(conn)


def _suggestions(conn, code=TEST_CODE, today=None, limit=10):
    import models.customers as customers
    if today is None:
        today, _r, _s = _window_dates(conn)
    return customers._cross_sell_suggestions(conn, code, today=today, limit=limit)


def _row(out, pid):
    return next((r for r in out if r['product_id'] == pid), None)


# ── Positive case + ranking threshold ───────────────────────────────────────

def test_positive_case_never_bought_by_3_other_shops_appears_with_their_count(cust):
    """SF1 (review round 1): a 4th shop buys on 3 SEPARATE invoices, so
    doc_count for this product is 6 while shop_count is 4 -- every fixture
    used to give each shop exactly one document, so a mutation reading
    doc_count instead of shop_count for the headline "N ร้าน" stayed green
    (measured on prod: 457 of 648 real qualifying products have
    doc_count != shop_count)."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าขายดี 498 หนึ่ง')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='POS', date_iso=recent)
    for i in range(3):
        _line(conn, doc_base=f'IVPOSHEAVY{i}', suffix=1, pid=pid, date_iso=recent,
              code='POSHEAVY', unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    assert len(out) == 1
    assert out[0]['product_id'] == pid
    assert out[0]['shop_count'] == 4
    # N-stock (review round 1): a whole-number stock renders as an int
    # (50), never a trailing-.0 float, matching how ซื้อบ่อย's own stock
    # column reads.
    assert out[0]['stock_qty'] == 50
    assert isinstance(out[0]['stock_qty'], int)


def test_product_bought_by_only_two_other_shops_never_appears(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าคนซื้อน้อย 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 2, prefix='TWO', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


# ── Exclusion clauses: must NOT add to shop count (far-side fixtures) ───────

def test_this_shops_own_padded_code_is_recognized_as_already_bought(cust):
    """Review round 1 (SF5): own-history and self-exclusion are now keyed
    the SAME way (the canonical key, via a sub-select), so a row whose
    `customer_code` carries incidental whitespace is recognized as this
    shop's own purchase -- the product must be excluded entirely
    ("already bought"), not merely have its shop_count corrected.

    Flipped from the original version of this test (which asserted
    `row is not None` / `shop_count == 3`): that version PINNED the bug —
    own-history used an exact `customer_code = ?` match and missed the
    padded row, so the shop's own product was suggested back to it (with
    a shop_count merely "corrected" by the separately-keyed self-exclusion
    clause). Confirmed red under the fix before flipping (erp rule: "a
    guard must survive its own success" — the OLD assertion could not)."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้ารหัสเว้นวรรค 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='PAD', date_iso=recent)
    _line(conn, doc_base='IVPADSELF', suffix=1, pid=pid, date_iso=recent,
          code=f' {TEST_CODE} ', name=TEST_NAME, unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


def test_credit_note_does_not_count_toward_shop_count(cust):
    """3 real invoices (qualifies) + 1 credit note from a 4th shop that
    would tip it to 4 if counted."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าใบลดหนี้ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='CN', date_iso=recent)
    _line(conn, doc_base='SRCN999', suffix=1, pid=pid, date_iso=recent, code='CNCREDIT',
          unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_excludes_revenue_giveaway_does_not_count_toward_shop_count(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าแจกฟรี 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='GV', date_iso=recent)
    _line(conn, doc_base='IVGV999', suffix=1, pid=pid, date_iso=recent, code='GVSHOP',
          unit_price=100, net=100)
    _writeoff(conn, 'IVGV999', 'GVSHOP', excludes_revenue=1)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_marketplace_pseudo_customer_does_not_count_toward_shop_count(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าหน้าร้าน 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='MP', date_iso=recent)
    _line(conn, doc_base='IVMP999', suffix=1, pid=pid, date_iso=recent, code='หน้าร้านS',
          name='หน้าร้านS', unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_zero_net_line_does_not_count_toward_shop_count(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าแถม 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='FREE', date_iso=recent)
    _line(conn, doc_base='IVFREE999', suffix=1, pid=pid, date_iso=recent, code='FREESHOP',
          unit_price=0, net=0)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_line_older_than_24_months_does_not_count_toward_shop_count(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าซื้อนานแล้ว 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='OLD', date_iso=recent)
    _line(conn, doc_base='IVOLD999', suffix=1, pid=pid, date_iso=stale, code='OLDSHOP',
          unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_window_boundary_exactly_730_days_ago_still_counts(cust):
    """SF3 (review round 1): pins the EXACT boundary the code implements
    (`s.date_iso >= today - 730 days`) -- the previous fixtures only ever
    used dates at today-30 ("recent") or the live DB's own historical max
    ("stale", ~800 days back), so window_days could drift to anywhere in
    31..799 and `>=` could flip to `>` without any test noticing."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    boundary_in = (dt.date.fromisoformat(today) - dt.timedelta(days=730)).isoformat()
    pid = _mk_product(conn, name='สินค้าขอบเขต 730 วัน 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 2, prefix='BOUNDIN', date_iso=recent)
    _line(conn, doc_base='IVBOUNDIN', suffix=1, pid=pid, date_iso=boundary_in,
          code='BOUNDIN', unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_window_boundary_731_days_ago_does_not_count(cust):
    """The mirror of the test above: one day further back than the window
    must NOT count toward shop_count."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    boundary_out = (dt.date.fromisoformat(today) - dt.timedelta(days=731)).isoformat()
    pid = _mk_product(conn, name='สินค้าขอบเขต 731 วัน 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='BOUNDOUT', date_iso=recent)
    _line(conn, doc_base='IVBOUNDOUT', suffix=1, pid=pid, date_iso=boundary_out,
          code='BOUNDOUT', unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['shop_count'] == 3


def test_a_product_bought_once_long_ago_by_everyone_never_appears(cust):
    """The acceptance criteria's own phrasing: every buyer is outside the
    24-month window -> shop_count for the window is 0, well under 3.

    Uses a large `limit` deliberately, not the real default (10): removing
    the date-window clause floods the candidate pool with the live dev
    DB's entire all-time history, which would rank this 5-shop fixture
    below the top 10 REGARDLESS of whether the window clause is present —
    "not in the top 10" would then hold for the wrong reason and this
    break-it-once would go undetected (confirmed: at limit=10 this stayed
    green with the window clause deleted). At limit=10000 the only way the
    product can be absent is the window/HAVING clause actually excluding
    it, which is the property under test."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าเก่าทั้งหมด 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 5, prefix='ALLOLD', date_iso=stale)

    out = _suggestions(conn, today=today, limit=10000)
    assert _row(out, pid) is None


# ── Exclusion clauses: product must NEVER appear ────────────────────────────

def test_out_of_stock_product_never_appears(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าหมดสต็อก 498')
    _set_stock(conn, pid, 0)
    _other_shops(conn, pid, 4, prefix='OOS', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


def test_product_with_no_stock_levels_row_never_appears(cust):
    """Control for the LEFT JOIN: a product that has never been touched by
    the stock ledger has NO stock_levels row at all (not merely 0) — must
    read the same as out-of-stock, not as unknown-therefore-shown."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าไม่เคยมีสต็อก 498')
    _other_shops(conn, pid, 4, prefix='NOROW', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


def test_inactive_product_never_appears(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าปิดการขาย 498', is_active=0)
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='INACT', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


def test_unpriced_product_never_appears(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้ายังไม่ตั้งราคา 498', base=0)
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='NOPRICE', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


def test_already_bought_product_never_appears(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าที่ร้านนี้เคยซื้อ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 5, prefix='OWN', date_iso=recent)
    # This shop bought it once, long ago -- still excludes it (all-time,
    # no date bound on the "ever bought" population).
    _line(conn, doc_base='IVOWN1', suffix=1, pid=pid, date_iso=stale, code=TEST_CODE,
          name=TEST_NAME, unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is None


# ── Grouping: at most one row per sub_category ──────────────────────────────

def test_subcategory_already_bought_in_another_variant_is_excluded(cust):
    """Control: this shop's own variant is a real evidenced purchase (would
    show on its product card) -- proving the exclusion is the sub_category
    rule, not an accident of the fixture."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_mine = _mk_product(conn, name='พุกไซส์ที่ร้านนี้ซื้อ 498', sub_category='พุก498ตัวเดียว')
    pid_other = _mk_product(conn, name='พุกไซส์อื่น 498', sub_category='พุก498ตัวเดียว')
    _set_stock(conn, pid_mine, 50)
    _set_stock(conn, pid_other, 50)
    _line(conn, doc_base='IVMINE1', suffix=1, pid=pid_mine, date_iso=recent, code=TEST_CODE,
          name=TEST_NAME, unit_price=100, net=100)
    _other_shops(conn, pid_other, 5, prefix='SUBCAT', date_iso=recent)

    import models.customers as customers
    own_where, own_params = customers._customer_sales_scope('customer_code', TEST_CODE, None, None)
    cards = customers._customer_product_cards(conn, own_where, own_params)
    assert any(c['product_id'] == pid_mine for c in cards), "control: own variant must be on the card"

    out = _suggestions(conn, today=today)
    assert _row(out, pid_other) is None


def test_at_most_one_row_per_subcategory_takes_the_highest_ranked(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_a = _mk_product(conn, name='พุก A 498', sub_category='พุกกลุ่ม498')
    pid_b = _mk_product(conn, name='พุก B 498', sub_category='พุกกลุ่ม498')
    _set_stock(conn, pid_a, 50)
    _set_stock(conn, pid_b, 50)
    _other_shops(conn, pid_a, 5, prefix='GRPA', date_iso=recent)   # higher popularity
    _other_shops(conn, pid_b, 4, prefix='GRPB', date_iso=recent)   # lower, same sub_category

    out = _suggestions(conn, today=today)
    ours = [r for r in out if r['product_id'] in (pid_a, pid_b)]
    assert len(ours) == 1
    assert ours[0]['product_id'] == pid_a


def test_unpriced_top_of_subcategory_does_not_hide_a_priced_lower_ranked_one(cust):
    """SF4 (review round 1): the spec orders "exclude (incl. unpriced)"
    BEFORE "one row per sub_category, taking its highest-ranked product" --
    the sub-category claim must happen AFTER the price check, so an
    unpriced top-ranked candidate never silently blocks a priced,
    lower-ranked one sharing its sub-category. Reproduced on real prod
    data: sub-category ลูกกลิ้งขนแกะ+ด้าม, unpriced pid 791 (higher
    popularity) hid priced pid 864 (lower popularity) before this fix."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_unpriced_top = _mk_product(conn, name='ลูกกลิ้งไม่มีราคา 498', base=0,
                                    sub_category='กลุ่มลูกกลิ้ง498')
    pid_priced_second = _mk_product(conn, name='ลูกกลิ้งมีราคา 498',
                                     sub_category='กลุ่มลูกกลิ้ง498')
    _set_stock(conn, pid_unpriced_top, 50)
    _set_stock(conn, pid_priced_second, 50)
    _other_shops(conn, pid_unpriced_top, 5, prefix='UNPTOP', date_iso=recent)    # ranks higher
    _other_shops(conn, pid_priced_second, 4, prefix='PRICED2', date_iso=recent)  # ranks lower

    out = _suggestions(conn, today=today)
    ours = [r for r in out if r['product_id'] in (pid_unpriced_top, pid_priced_second)]
    assert len(ours) == 1
    assert ours[0]['product_id'] == pid_priced_second


def test_null_subcategory_products_each_stand_on_their_own(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_a = _mk_product(conn, name='สินค้าไม่มีหมวดย่อย A 498', sub_category=None)
    pid_b = _mk_product(conn, name='สินค้าไม่มีหมวดย่อย B 498', sub_category=None)
    _set_stock(conn, pid_a, 50)
    _set_stock(conn, pid_b, 50)
    _other_shops(conn, pid_a, 5, prefix='NULLA', date_iso=recent)
    _other_shops(conn, pid_b, 4, prefix='NULLB', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid_a) is not None
    assert _row(out, pid_b) is not None


# ── Ranking / tie-breaks ─────────────────────────────────────────────────────

def test_ranked_by_shop_count_descending(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_hi = _mk_product(conn, name='ยอดนิยมมาก 498')
    pid_lo = _mk_product(conn, name='ยอดนิยมน้อย 498')
    _set_stock(conn, pid_hi, 50)
    _set_stock(conn, pid_lo, 50)
    _other_shops(conn, pid_hi, 6, prefix='HI', date_iso=recent)
    _other_shops(conn, pid_lo, 3, prefix='LO', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert [r['product_id'] for r in out] == [pid_hi, pid_lo]


def test_tie_break_by_doc_count_then_by_product_id(cust):
    """SF2 (review round 1): `pid_higher_id` is created SECOND (so it holds
    the higher product id) and is the one given the extra document -- the
    ORIGINAL version of this test created the doc-count winner FIRST, so
    the product-id tie-break alone produced the same order and the test
    could never distinguish "ranked by doc_count" from "ranked by id"
    (confirmed: dropping the doc_count ORDER BY term stayed green). Here,
    doc_count and product-id ASC point in OPPOSITE directions, so only a
    real doc_count tie-break can put the higher-id product first."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_lower_id = _mk_product(conn, name='สินค้าไทเบรค id น้อย 498')
    pid_higher_id = _mk_product(conn, name='สินค้าไทเบรค id มาก 498')
    _set_stock(conn, pid_lower_id, 50)
    _set_stock(conn, pid_higher_id, 50)
    # Same shop_count (3) for both. pid_higher_id gets an EXTRA doc from one
    # of its 3 shops (higher doc_count, same shop set); pid_lower_id gets
    # exactly 1 doc per shop.
    _other_shops(conn, pid_lower_id, 3, prefix='DOC2', date_iso=recent)
    _other_shops(conn, pid_higher_id, 3, prefix='DOC', date_iso=recent)
    _line(conn, doc_base='IVDOCEXTRA', suffix=1, pid=pid_higher_id, date_iso=recent,
          code='DOC000', unit_price=100, net=100)

    out = _suggestions(conn, today=today)
    ours = [r['product_id'] for r in out if r['product_id'] in (pid_lower_id, pid_higher_id)]
    assert ours == [pid_higher_id, pid_lower_id]


def test_tie_break_by_product_id_when_shop_and_doc_counts_are_equal(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid_lower = _mk_product(conn, name='สินค้า id น้อยกว่า 498')
    pid_higher = _mk_product(conn, name='สินค้า id มากกว่า 498')
    if pid_lower > pid_higher:
        pid_lower, pid_higher = pid_higher, pid_lower
    _set_stock(conn, pid_lower, 50)
    _set_stock(conn, pid_higher, 50)
    _other_shops(conn, pid_higher, 3, prefix='IDHI', date_iso=recent)
    _other_shops(conn, pid_lower, 3, prefix='IDLO', date_iso=recent)

    out = _suggestions(conn, today=today)
    ours = [r['product_id'] for r in out if r['product_id'] in (pid_lower, pid_higher)]
    assert ours == [pid_lower, pid_higher]


def test_limit_caps_at_ten_even_with_more_qualifying_products(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pids = []
    for n in range(12):
        pid = _mk_product(conn, name=f'สินค้าเกิน 10 รายการ 498 {n}')
        _set_stock(conn, pid, 50)
        # Descending popularity so ordering is deterministic: n=0 highest.
        _other_shops(conn, pid, 20 - n, prefix=f'LIM{n:02d}', date_iso=recent)
        pids.append(pid)

    out = _suggestions(conn, today=today)
    assert len(out) == 10
    assert [r['product_id'] for r in out] == pids[:10]


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_shop_with_no_qualifying_purchases_gets_plain_top10(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าลูกค้าใหม่ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='NEWCUST', date_iso=recent)

    out = _suggestions(conn, today=today)
    assert _row(out, pid) is not None


def test_empty_when_nothing_qualifies(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    out = _suggestions(conn, today=today)
    assert out == []


def test_code_with_no_master_row_still_computes(cust):
    """A code with no `customers` master row at all -- the helper never
    joins customers, so it must not error."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    ghost_code = 'GHOST4980'
    pid = _mk_product(conn, name='สินค้าโค้ดไม่มีมาสเตอร์ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='GHOST', date_iso=recent)

    import models.customers as customers
    out = customers._cross_sell_suggestions(conn, ghost_code, today=today, limit=10)
    assert _row(out, pid) is not None


# ── Shape: no cost/margin, price + promo shown ──────────────────────────────

def test_suggestion_dict_carries_no_cost_or_margin_keys(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าตรวจคีย์ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='KEYS', date_iso=recent)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    forbidden = {'cost', 'cost_price', 'cost_per_unit', 'wacc', 'margin',
                 'margin_at_answer_pct', 'internal', 'customer'}
    assert forbidden.isdisjoint(row.keys())


def test_price_reflects_active_percent_promo(cust):
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าโปรโมชั่น 498', base=60.0)
    _set_stock(conn, pid, 50)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        " date_start, is_active) VALUES (?, 'ลด 10%', 'percent', 10, '2024-01-01', 1)",
        (pid,))
    conn.commit()
    _other_shops(conn, pid, 3, prefix='PROMO', date_iso=recent)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['list_for_unit'] == pytest.approx(60.0)
    assert row['price_per_unit'] == pytest.approx(54.0)
    assert row['promo_affects_price'] is True
    assert row['promo']['promo_type'] == 'percent'


def test_stock_qty_is_none_when_ratio_is_not_derivable(cust):
    """Dozen-only product (base_sell_price=0, only a pack tier) -- the
    resolver answers the price at the tier's own unit with ratio=None
    (ratio_source='unknown'); stock cannot be expressed in that unit."""
    conn = cust
    today, recent, stale = _window_dates(conn)
    pid = _mk_product(conn, name='สินค้าขายยกแพ็คเท่านั้น 498', base=0)
    conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
        (pid, '1 แพ็ค', 500),
    )
    conn.commit()
    _set_stock(conn, pid, 12)
    _other_shops(conn, pid, 3, prefix='PACK', date_iso=recent)

    out = _suggestions(conn, today=today)
    row = _row(out, pid)
    assert row is not None
    assert row['stock_qty'] is None
    assert row['unit'] == 'แพ็ค'


# ── Independent of the page's date filter ───────────────────────────────────

def test_helper_takes_no_date_from_date_to_params():
    """The helper's own signature has no date_from/date_to at all -- the
    caller (get_customer_summary_by_code) can never accidentally thread
    the page's date filter through. See test_498_customer_page_suggestions.py
    for the end-to-end version of this same guarantee."""
    import inspect
    import models.customers as customers
    sig = inspect.signature(customers._cross_sell_suggestions)
    assert 'date_from' not in sig.parameters
    assert 'date_to' not in sig.parameters
