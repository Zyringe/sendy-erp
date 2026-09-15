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

Prior art for fixtures: tests/test_497_winback.py, tests/test_493_slice2_product_card.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import datetime as dt

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST4980'
TEST_NAME = 'ลูกค้าทดสอบ 498 เสนอเพิ่ม'

TODAY = '2026-09-15'
RECENT = '2026-06-01'          # inside the trailing-24-month window
STALE = '2023-01-01'           # older than 24 months before TODAY

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


def _other_shops(conn, pid, n, *, prefix='OTH', date_iso=RECENT, unit_price=100, net=100,
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


def _suggestions(conn, code=TEST_CODE, today=TODAY, limit=10):
    import models.customers as customers
    return customers._cross_sell_suggestions(conn, code, today=today, limit=limit)


# ── Positive case + ranking threshold ───────────────────────────────────────

def test_positive_case_never_bought_by_3_other_shops_appears_with_their_count(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าขายดี 498 หนึ่ง')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='POS')

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['product_id'] == pid
    assert out[0]['shop_count'] == 4


def test_product_bought_by_only_two_other_shops_never_appears(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าคนซื้อน้อย 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 2, prefix='TWO')

    out = _suggestions(conn)
    assert out == []


# ── Exclusion clauses: must NOT add to shop count (far-side fixtures) ───────

def test_credit_note_does_not_count_toward_shop_count(cust):
    """3 real invoices (qualifies) + 1 credit note from a 4th shop that
    would tip it to 4 if counted."""
    conn = cust
    pid = _mk_product(conn, name='สินค้าใบลดหนี้ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='CN')
    _line(conn, doc_base='SRCN999', suffix=1, pid=pid, date_iso=RECENT, code='CNCREDIT',
          unit_price=100, net=100)

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['shop_count'] == 3


def test_excludes_revenue_giveaway_does_not_count_toward_shop_count(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าแจกฟรี 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='GV')
    _line(conn, doc_base='IVGV999', suffix=1, pid=pid, date_iso=RECENT, code='GVSHOP',
          unit_price=100, net=100)
    _writeoff(conn, 'IVGV999', 'GVSHOP', excludes_revenue=1)

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['shop_count'] == 3


def test_marketplace_pseudo_customer_does_not_count_toward_shop_count(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าหน้าร้าน 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='MP')
    _line(conn, doc_base='IVMP999', suffix=1, pid=pid, date_iso=RECENT, code='หน้าร้านS',
          name='หน้าร้านS', unit_price=100, net=100)

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['shop_count'] == 3


def test_zero_net_line_does_not_count_toward_shop_count(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าแถม 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='FREE')
    _line(conn, doc_base='IVFREE999', suffix=1, pid=pid, date_iso=RECENT, code='FREESHOP',
          unit_price=0, net=0)

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['shop_count'] == 3


def test_line_older_than_24_months_does_not_count_toward_shop_count(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าซื้อนานแล้ว 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='OLD')
    _line(conn, doc_base='IVOLD999', suffix=1, pid=pid, date_iso=STALE, code='OLDSHOP',
          unit_price=100, net=100)

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['shop_count'] == 3


def test_a_product_bought_once_long_ago_by_everyone_never_appears(cust):
    """The acceptance criteria's own phrasing: every buyer is outside the
    24-month window -> shop_count for the window is 0, well under 3."""
    conn = cust
    pid = _mk_product(conn, name='สินค้าเก่าทั้งหมด 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 5, prefix='ALLOLD', date_iso=STALE)

    out = _suggestions(conn)
    assert out == []


# ── Exclusion clauses: product must NEVER appear ────────────────────────────

def test_out_of_stock_product_never_appears(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าหมดสต็อก 498')
    _set_stock(conn, pid, 0)
    _other_shops(conn, pid, 4, prefix='OOS')

    out = _suggestions(conn)
    assert out == []


def test_product_with_no_stock_levels_row_never_appears(cust):
    """Control for the LEFT JOIN: a product that has never been touched by
    the stock ledger has NO stock_levels row at all (not merely 0) — must
    read the same as out-of-stock, not as unknown-therefore-shown."""
    conn = cust
    pid = _mk_product(conn, name='สินค้าไม่เคยมีสต็อก 498')
    _other_shops(conn, pid, 4, prefix='NOROW')

    out = _suggestions(conn)
    assert out == []


def test_inactive_product_never_appears(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าปิดการขาย 498', is_active=0)
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='INACT')

    out = _suggestions(conn)
    assert out == []


def test_unpriced_product_never_appears(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้ายังไม่ตั้งราคา 498', base=0)
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='NOPRICE')

    out = _suggestions(conn)
    assert out == []


def test_already_bought_product_never_appears(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าที่ร้านนี้เคยซื้อ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 5, prefix='OWN')
    # This shop bought it once, long ago -- still excludes it (all-time).
    _line(conn, doc_base='IVOWN1', suffix=1, pid=pid, date_iso=STALE, code=TEST_CODE,
          name=TEST_NAME, unit_price=100, net=100)

    out = _suggestions(conn)
    assert out == []


# ── Grouping: at most one row per sub_category ──────────────────────────────

def test_subcategory_already_bought_in_another_variant_is_excluded(cust):
    """Control: this shop's own variant is a real evidenced purchase (would
    show on its product card) -- proving the exclusion is the sub_category
    rule, not an accident of the fixture."""
    conn = cust
    pid_mine = _mk_product(conn, name='พุกไซส์ที่ร้านนี้ซื้อ 498', sub_category='พุก')
    pid_other = _mk_product(conn, name='พุกไซส์อื่น 498', sub_category='พุก')
    _set_stock(conn, pid_mine, 50)
    _set_stock(conn, pid_other, 50)
    _line(conn, doc_base='IVMINE1', suffix=1, pid=pid_mine, date_iso=RECENT, code=TEST_CODE,
          name=TEST_NAME, unit_price=100, net=100)
    _other_shops(conn, pid_other, 5, prefix='SUBCAT')

    import models.customers as customers
    own_where, own_params = customers._customer_sales_scope('customer_code', TEST_CODE, None, None)
    cards = customers._customer_product_cards(conn, own_where, own_params)
    assert any(c['product_id'] == pid_mine for c in cards), "control: own variant must be on the card"

    out = _suggestions(conn)
    assert out == []


def test_at_most_one_row_per_subcategory_takes_the_highest_ranked(cust):
    conn = cust
    pid_a = _mk_product(conn, name='พุก A 498', sub_category='พุกกลุ่ม498')
    pid_b = _mk_product(conn, name='พุก B 498', sub_category='พุกกลุ่ม498')
    _set_stock(conn, pid_a, 50)
    _set_stock(conn, pid_b, 50)
    _other_shops(conn, pid_a, 5, prefix='GRPA')   # higher popularity
    _other_shops(conn, pid_b, 4, prefix='GRPB')   # lower popularity, same sub_category

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['product_id'] == pid_a


def test_null_subcategory_products_each_stand_on_their_own(cust):
    conn = cust
    pid_a = _mk_product(conn, name='สินค้าไม่มีหมวดย่อย A 498', sub_category=None)
    pid_b = _mk_product(conn, name='สินค้าไม่มีหมวดย่อย B 498', sub_category=None)
    _set_stock(conn, pid_a, 50)
    _set_stock(conn, pid_b, 50)
    _other_shops(conn, pid_a, 5, prefix='NULLA')
    _other_shops(conn, pid_b, 4, prefix='NULLB')

    out = _suggestions(conn)
    ids = {r['product_id'] for r in out}
    assert ids == {pid_a, pid_b}


# ── Ranking / tie-breaks ─────────────────────────────────────────────────────

def test_ranked_by_shop_count_descending(cust):
    conn = cust
    pid_hi = _mk_product(conn, name='ยอดนิยมมาก 498')
    pid_lo = _mk_product(conn, name='ยอดนิยมน้อย 498')
    _set_stock(conn, pid_hi, 50)
    _set_stock(conn, pid_lo, 50)
    _other_shops(conn, pid_hi, 6, prefix='HI')
    _other_shops(conn, pid_lo, 3, prefix='LO')

    out = _suggestions(conn)
    assert [r['product_id'] for r in out] == [pid_hi, pid_lo]


def test_tie_break_by_doc_count_then_by_product_id(cust):
    conn = cust
    pid_first = _mk_product(conn, name='สินค้าไทเบรค A 498')
    pid_second = _mk_product(conn, name='สินค้าไทเบรค B 498')
    _set_stock(conn, pid_first, 50)
    _set_stock(conn, pid_second, 50)
    # Same shop_count (3) for both. pid_first gets 2 docs from one of its 3
    # shops (higher doc_count); pid_second gets exactly 1 doc per shop.
    _other_shops(conn, pid_first, 3, prefix='DOC')
    _line(conn, doc_base='IVDOCEXTRA', suffix=1, pid=pid_first, date_iso=RECENT,
          code='DOC000', unit_price=100, net=100)
    _other_shops(conn, pid_second, 3, prefix='DOC2')

    out = _suggestions(conn)
    assert [r['product_id'] for r in out] == [pid_first, pid_second]


def test_tie_break_by_product_id_when_shop_and_doc_counts_are_equal(cust):
    conn = cust
    pid_lower = _mk_product(conn, name='สินค้า id น้อยกว่า 498')
    pid_higher = _mk_product(conn, name='สินค้า id มากกว่า 498')
    if pid_lower > pid_higher:
        pid_lower, pid_higher = pid_higher, pid_lower
    _set_stock(conn, pid_lower, 50)
    _set_stock(conn, pid_higher, 50)
    _other_shops(conn, pid_higher, 3, prefix='IDHI')
    _other_shops(conn, pid_lower, 3, prefix='IDLO')

    out = _suggestions(conn)
    assert [r['product_id'] for r in out] == [pid_lower, pid_higher]


def test_limit_caps_at_ten_even_with_more_qualifying_products(cust):
    conn = cust
    pids = []
    for n in range(12):
        pid = _mk_product(conn, name=f'สินค้าเกิน 10 รายการ 498 {n}')
        _set_stock(conn, pid, 50)
        # Descending popularity so ordering is deterministic: n=0 highest.
        _other_shops(conn, pid, 20 - n, prefix=f'LIM{n:02d}')
        pids.append(pid)

    out = _suggestions(conn)
    assert len(out) == 10
    assert [r['product_id'] for r in out] == pids[:10]


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_shop_with_no_qualifying_purchases_gets_plain_top10(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าลูกค้าใหม่ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='NEWCUST')

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['product_id'] == pid


def test_empty_when_nothing_qualifies(cust):
    conn = cust
    out = _suggestions(conn)
    assert out == []


def test_code_with_no_master_row_still_computes(cust):
    """A code with no `customers` master row at all -- the helper never
    joins customers, so it must not error."""
    conn = cust
    ghost_code = 'GHOST4980'
    pid = _mk_product(conn, name='สินค้าโค้ดไม่มีมาสเตอร์ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='GHOST')

    import models.customers as customers
    out = customers._cross_sell_suggestions(conn, ghost_code, today=TODAY, limit=10)
    assert len(out) == 1


# ── Shape: no cost/margin, price + promo shown ──────────────────────────────

def test_suggestion_dict_carries_no_cost_or_margin_keys(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าตรวจคีย์ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 3, prefix='KEYS')

    out = _suggestions(conn)
    assert len(out) == 1
    row = out[0]
    forbidden = {'cost', 'cost_price', 'cost_per_unit', 'wacc', 'margin',
                 'margin_at_answer_pct', 'internal', 'customer'}
    assert forbidden.isdisjoint(row.keys())


def test_price_reflects_active_percent_promo(cust):
    conn = cust
    pid = _mk_product(conn, name='สินค้าโปรโมชั่น 498', base=60.0)
    _set_stock(conn, pid, 50)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        " date_start, is_active) VALUES (?, 'ลด 10%', 'percent', 10, '2024-01-01', 1)",
        (pid,))
    conn.commit()
    _other_shops(conn, pid, 3, prefix='PROMO')

    out = _suggestions(conn)
    assert len(out) == 1
    row = out[0]
    assert row['list_for_unit'] == pytest.approx(60.0)
    assert row['price_per_unit'] == pytest.approx(54.0)
    assert row['promo_affects_price'] is True
    assert row['promo']['promo_type'] == 'percent'


def test_stock_qty_is_none_when_ratio_is_not_derivable(cust):
    """Dozen-only product (base_sell_price=0, only a pack tier) -- the
    resolver answers the price at the tier's own unit with ratio=None
    (ratio_source='unknown'); stock cannot be expressed in that unit."""
    conn = cust
    pid = _mk_product(conn, name='สินค้าขายยกแพ็คเท่านั้น 498', base=0)
    conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
        (pid, '1 แพ็ค', 500),
    )
    conn.commit()
    _set_stock(conn, pid, 12)
    _other_shops(conn, pid, 3, prefix='PACK')

    out = _suggestions(conn)
    assert len(out) == 1
    assert out[0]['stock_qty'] is None
    assert out[0]['unit'] == 'แพ็ค'


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
