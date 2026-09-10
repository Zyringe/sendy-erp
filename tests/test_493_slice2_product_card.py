"""TDD for #493 Slice 2 (trimmed) — the enriched สินค้าที่ซื้อบ่อย product card.

Scope (Put picked B — trip is on its last day, full spec deferred):
  IN  — times bought per (product, unit), single ordering (times bought desc,
        ties by money), last price paid (VAT-inclusive, same unit, with date +
        tappable invoice), today's list-after-promo price.
  OUT — toggle ครั้ง/ยอด, stale-note, stock badge, call-card button, VAT-note
        text / bill-discount / freebie breakdown, and all of Slice 3 (cost).

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
          vat_type=1, unit='ตัว', ref_invoice=None,
          customer_code=TEST_CODE, customer_name=TEST_NAME):
    doc_no = f"{doc_base}-{suffix}"
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, net, net, ref_invoice),
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
