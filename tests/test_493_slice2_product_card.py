"""TDD for #493 Slice 2 — the enriched สินค้าที่ซื้อบ่อย product card.

Round 1 (Put picked B — trip was on its last day, full spec deferred):
  IN  — times bought per (product, unit), single ordering (times bought desc,
        ties by money), last price paid (VAT-inclusive, same unit, with date +
        tappable invoice), today's list-after-promo price.
  OUT — toggle ครั้ง/ยอด, stale-note, stock badge, call-card button, VAT-note
        text / bill-discount / freebie breakdown.

Round 2 (2026-09-11, this file — Put picked A: finish the rest of Slice 2):
  IN  — ครั้ง/ยอด toggle (union of both top-20 orderings, client-side re-sort),
        stale-note (reusing price_lookup.epochs_for_pairs, the call card's own
        mechanism), stock badge (red when stock < the row unit's ratio, no
        badge when the ratio is unknown), the call-card button, and on the
        last-price line: the VAT note (แยก VAT only), the bill-level discount
        (derived from net vs total), and same-product freebies on that doc.
  OUT — Slice 3 (cost block: WACC, last purchase cost, both margins, badges —
        admin/manager only). Not started.

This is additive: a NEW `product_cards` key on get_customer_summary_by_code's
result, alongside the untouched `top_products` (still money-ordered — the
call card's "แบรนด์เด่น" reads top_products[0].name and must not change).

Seam 1 (primary): the customer summary data contract — prior art (insert
helpers, tmp_db_conn/cust fixtures): test_493_slice1_documents.py.
Seam 2: HTTP render, session-injected roles — prior art: test_customer_code_route.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from urllib.parse import quote

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST4932'
TEST_NAME = 'ลูกค้าทดสอบ 493 สอง'

_pid_counter = [493200]


def _mk_product(conn, name='สินค้าทดสอบสอง', unit_type='ตัว', base=100.0, cost=60.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()


def _clear_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                 (code, name))
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, qty, unit_price, net,
          vat_type=1, unit='ตัว', ref_invoice=None, total=None, discount=None,
          customer_code=TEST_CODE, customer_name=TEST_NAME):
    doc_no = f"{doc_base}-{suffix}"
    # total defaults to net (pre-existing behaviour — no bill-level discount)
    # unless a test explicitly wants total != net to exercise that discount.
    if total is None:
        total = net
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice, discount) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, total, net, ref_invoice, discount),
    )
    conn.commit()


@pytest.fixture
def cust(tmp_db_conn):
    _mk_customer(tmp_db_conn)
    _clear_customer(tmp_db_conn)
    pid = _mk_product(tmp_db_conn)
    yield tmp_db_conn, pid
    _clear_customer(tmp_db_conn)


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _card(data, pid, unit='ตัว'):
    return next(c for c in data['product_cards'] if c['product_id'] == pid and c['unit'] == unit)


# ── Seam 1: product_cards data contract ─────────────────────────────────────

def test_times_bought_counts_paid_invoices_only(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49320', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    _line(conn, doc_base='IV49321', suffix=1, pid=pid, date_iso='2026-01-02',
          qty=1, unit_price=100, net=100, vat_type=0)
    # Freebie-only line — must not count.
    _line(conn, doc_base='IV49322', suffix=1, pid=pid, date_iso='2026-01-03',
          qty=1, unit_price=0, net=0, vat_type=0)
    # Credit note — must not count.
    _line(conn, doc_base='SR49323', suffix=1, pid=pid, date_iso='2026-01-04',
          qty=1, unit_price=100, net=100, vat_type=0, ref_invoice='IV49320')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['times_bought'] == 2


def test_last_price_is_vat_inclusive_and_carries_date_and_doc(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49330', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    # Later + แยก VAT → this is the "last" line, and its price must include
    # the 7% the customer actually paid (100 * 1.07 = 107.00), never the
    # bare ex-VAT net.
    _line(conn, doc_base='IV49331', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=100, net=100, vat_type=2)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['last']['doc_base'] == 'IV49331'
    assert card['last']['date_iso'] == '2026-02-01'
    assert card['last']['price_per_unit'] == pytest.approx(107.0)
    # Controls for the round-2 fields: no price_history row → never stale;
    # total == net (the _line default) → no bill-level discount at all.
    assert card['last']['is_stale'] is False
    assert card['last']['bill_discount_pct'] is None
    assert card['last']['freebies'] == []


def test_stale_note_set_when_last_purchase_predates_a_price_change(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49390', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    # Base price changed AFTER this purchase — reuses price_lookup's own
    # epoch source (product_price_history), the same mechanism the call
    # card's stale flag uses (price_lookup.epochs_for_pairs).
    conn.execute(
        "INSERT INTO product_price_history "
        "(product_id, field_name, old_value, new_value, changed_at) "
        "VALUES (?, 'base_sell_price', 100, 120, '2026-02-01')",
        (pid,),
    )
    conn.commit()
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['last']['is_stale'] is True


def test_last_line_reports_vat_note_fields_and_bill_level_discount(cust):
    conn, pid = cust
    # แยก VAT, 10% line discount, and a 2% bill-level discount (total=200,
    # net=196 — the customer's paid share after the doc-level cut).
    _line(conn, doc_base='IV49391', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=2, unit_price=100, net=196, total=200, discount='10%',
          vat_type=2)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['last']['vat_type'] == 2
    assert card['last']['unit_price'] == pytest.approx(100.0)
    assert card['last']['discount'] == '10%'
    assert card['last']['bill_discount_pct'] == pytest.approx(2.0)


def test_last_line_reports_same_product_freebies_on_the_same_document(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49392', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=10, unit_price=100, net=1000, vat_type=0)
    # Free line, same doc, same product, in its OWN unit (ตัว here, could
    # differ from the paid line's unit — the query must not restrict by unit).
    _line(conn, doc_base='IV49392', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=2, unit_price=0, net=0, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['last']['freebies'] == [{'qty': 2.0, 'unit': 'ตัว'}]


def test_product_bought_in_two_units_is_two_rows(cust):
    conn, pid = cust
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, 'โหล', 12))
    conn.commit()
    _line(conn, doc_base='IV49332', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0, unit='ตัว')
    _line(conn, doc_base='IV49333', suffix=1, pid=pid, date_iso='2026-01-02',
          qty=1, unit_price=1000, net=1000, vat_type=0, unit='โหล')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    piece = _card(data, pid, unit='ตัว')
    dozen = _card(data, pid, unit='โหล')
    assert piece['last']['price_per_unit'] == pytest.approx(100.0)
    assert dozen['last']['price_per_unit'] == pytest.approx(1000.0)


def test_today_price_is_list_after_promo_no_promo_case(cust):
    conn, pid = cust
    # cust's product has base_sell_price=100 (see _mk_product default).
    _line(conn, doc_base='IV49334', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=90, net=90, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['today']['has_list_price'] is True
    assert card['today']['price_per_unit'] == pytest.approx(100.0)


def test_no_base_price_shows_no_list_price_not_zero(cust):
    conn, pid = cust
    conn.execute("UPDATE products SET base_sell_price = 0 WHERE id = ?", (pid,))
    conn.commit()
    _line(conn, doc_base='IV49335', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=50, net=50, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid)
    assert card['today']['has_list_price'] is False
    assert card['today']['price_per_unit'] is None


def test_stock_badge_insufficient_when_stock_below_unit_ratio(cust):
    conn, pid = cust
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, 'โหล', 12))
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 5)", (pid,))
    conn.commit()
    _line(conn, doc_base='IV49393', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=1000, net=1000, vat_type=0, unit='โหล')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid, unit='โหล')
    assert card['stock']['base_qty'] == 5
    assert card['stock']['insufficient'] is True


def test_stock_badge_not_insufficient_when_stock_covers_ratio(cust):
    conn, pid = cust
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, 'โหล', 12))
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 50)", (pid,))
    conn.commit()
    _line(conn, doc_base='IV49394', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=1000, net=1000, vat_type=0, unit='โหล')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid, unit='โหล')
    assert card['stock']['base_qty'] == 50
    assert card['stock']['insufficient'] is False


def test_stock_badge_absent_when_ratio_is_unknown(cust):
    """A tier answers the price (dozen-only-style) but no unit_conversions
    row exists for it — resolve_price returns ratio=None, and the stock
    badge cannot judge 'enough for one unit' without a ratio: no figure,
    no badge, per the issue's own decision."""
    conn, pid = cust
    conn.execute(
        "INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?,?,?)",
        (pid, '1 แพ็ค', 500),
    )
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    conn.commit()
    _line(conn, doc_base='IV49395', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=500, net=500, vat_type=0, unit='แพ็ค')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    card = _card(data, pid, unit='แพ็ค')
    assert card['today']['has_list_price'] is True   # tier still answers the price
    assert card['stock'] is None


def test_cards_ordered_by_times_bought_then_money(cust):
    conn, pid = cust
    pid_a = _mk_product(conn, name='สินค้า A ซื้อบ่อย')
    pid_b = _mk_product(conn, name='สินค้า B ซื้อครั้งเดียวแพง')
    # A: bought 3 times, small amounts.
    for i, d in enumerate(['2026-01-01', '2026-01-02', '2026-01-03']):
        _line(conn, doc_base=f'IV4934{i}', suffix=1, pid=pid_a, date_iso=d,
              qty=1, unit_price=10, net=10, vat_type=0)
    # B: bought once, big amount — must still sort AFTER A (times bought wins).
    _line(conn, doc_base='IV49350', suffix=1, pid=pid_b, date_iso='2026-01-01',
          qty=1, unit_price=5000, net=5000, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    order = [c['product_id'] for c in data['product_cards']]
    assert order.index(pid_a) < order.index(pid_b)


def test_union_includes_a_top20_by_money_row_outside_top20_by_times(cust):
    """The issue's own decision: two top-20 orderings, UNIONED — a one-off
    big-ticket purchase must still be on the page even when 20 OTHER
    products each outrank it on times bought alone."""
    conn, pid = cust
    filler_ids = []
    for n in range(20):
        fpid = _mk_product(conn, name=f'สินค้าเติม {n}')
        filler_ids.append(fpid)
        for i, d in enumerate(['2026-01-01', '2026-01-02', '2026-01-03']):
            _line(conn, doc_base=f'IV494{n:02d}{i}', suffix=1, pid=fpid,
                  date_iso=d, qty=1, unit_price=1, net=1, vat_type=0)
    # times_bought=3 each, 20 of them — completely fills the times-bought
    # top 20, so a 21st product (times_bought=1) cannot get in on that axis.
    pid_money = _mk_product(conn, name='สินค้าแพงครั้งเดียว')
    _line(conn, doc_base='IV49399', suffix=1, pid=pid_money, date_iso='2026-01-01',
          qty=1, unit_price=999999, net=999999, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    order = [c['product_id'] for c in data['product_cards']]
    assert pid_money in order
    assert len(order) == 21


# ── Seam 2: HTTP render ─────────────────────────────────────────────────────

def test_customer_page_product_card_shows_last_price_and_invoice_link(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49340', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    # Two links to the same invoice on this page now: one in the product
    # card's ราคาล่าสุด cell (new), one in รายการเอกสาร (Slice 1, unchanged).
    assert html.count('doc/IV49340"') == 2
    assert '2026-01-01' in html
    assert 'ครั้งที่ซื้อ' in html
    assert 'ราคาวันนี้' in html


def test_customer_page_no_list_price_shows_text_not_zero(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    conn.execute("UPDATE products SET base_sell_price = 0 WHERE id = ?", (pid,))
    _line(conn, doc_base='IV49341', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=50, net=50, vat_type=0)
    conn.commit()
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'ยังไม่มีราคาตั้ง' in html


def test_customer_page_has_sort_toggle_and_row_data_attributes(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49396', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'data-sort-cards="times"' in html
    assert 'data-sort-cards="money"' in html
    assert f'data-times-bought="1"' in html


def test_customer_page_has_call_card_button(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert f'href="/call/{TEST_CODE}"' in html


def test_customer_page_shows_stale_note_only_when_stale(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid_stale = _mk_product(conn, name='สินค้าราคาเปลี่ยน')
    pid_fresh = _mk_product(conn, name='สินค้าราคาไม่เปลี่ยน')
    _line(conn, doc_base='IV49397', suffix=1, pid=pid_stale, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.execute(
        "INSERT INTO product_price_history "
        "(product_id, field_name, old_value, new_value, changed_at) "
        "VALUES (?, 'base_sell_price', 100, 120, '2026-02-01')",
        (pid_stale,),
    )
    _line(conn, doc_base='IV49398', suffix=1, pid=pid_fresh, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.commit()
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert html.count('ก่อนเปลี่ยนราคา') == 1


def test_customer_page_shows_vat_note_only_on_split_vat_line(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid_vat = _mk_product(conn, name='สินค้าแยกVAT')
    pid_novat = _mk_product(conn, name='สินค้าไม่แยกVAT')
    _line(conn, doc_base='IV49400', suffix=1, pid=pid_vat, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=2)
    _line(conn, doc_base='IV49401', suffix=1, pid=pid_novat, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert html.count('+ VAT') == 1


def test_customer_page_low_stock_row_gets_the_warning_class(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, 'โหล', 12))
    conn.execute("INSERT INTO stock_levels (product_id, quantity) VALUES (?, 1)", (pid,))
    _line(conn, doc_base='IV49402', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=1000, net=1000, vat_type=0, unit='โหล')
    conn.commit()
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert 'row-low-stock' in html


def test_call_card_still_reads_top_products_unaffected(tmp_db):
    """/code-review risk: product_cards is ADDITIVE. top_products must stay
    money-ordered and untouched — call/card.html's "แบรนด์เด่น" reads
    top_products[0].name (get_customer_summary, name-keyed, via call_card.py).
    A cheap product bought many times must NOT outrank an expensive one here,
    even though it does in product_cards (times-bought order)."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid_cheap = _mk_product(conn, name='ถูกซื้อบ่อย')
    pid_costly = _mk_product(conn, name='แพงซื้อครั้งเดียว')
    for i, d in enumerate(['2026-01-01', '2026-01-02', '2026-01-03']):
        _line(conn, doc_base=f'IV4935{i}', suffix=1, pid=pid_cheap, date_iso=d,
              qty=1, unit_price=10, net=10, vat_type=0)
    _line(conn, doc_base='IV49360', suffix=1, pid=pid_costly, date_iso='2026-01-01',
          qty=1, unit_price=5000, net=5000, vat_type=0)
    conn.close()

    import models
    data = models.get_customer_summary(TEST_NAME)
    assert data['top_products'][0]['product_id'] == pid_costly
