"""#569: /m/sales-trip reads a customer's bills by customers.code.

The rep reads ล่าสุด (last purchase) and the ฿ figure (what the shop owes) off
this card before walking into the shop. Both were joined on the master NAME,
which the bill name drifts from, so 195 of 272 buyers on prod rendered as
"never bought, owes nothing". The marketplace pseudo-customers match by code, so
the same fix would turn them into shops to visit unless they are excluded.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from urllib.parse import quote

import lxml.html
import pytest

REGION = 'ภาคตะวันออก'
ADDRESS = '123 ถ.สุขุมวิท ชลบุรี'

MISMATCH = ('C-569T', 'หจก. ร้านทริปห้าหกเก้า', 'ร้านทริปห้าหกเก้า')
MATCH = ('C-569K', 'ร้านทริปตรงชื่อห้าหกเก้า', 'ร้านทริปตรงชื่อห้าหกเก้า')
MARKETPLACE = ('Zหน้าร้าน', 'Bหน้าร้าน', 'Lหน้าร้าน', 'Tหน้าร้าน')

# (customer, doc_base, date, net, excludes_revenue or None when not written off)
BILLS = (
    (MISMATCH, 'IV-569T1', '2026-05-01', 1000.0, None),
    (MISMATCH, 'IV-569T2', '2026-06-01', 2000.0, 0),
    (MISMATCH, 'IV-569T3', '2026-07-01', 3000.0, 1),
    (MATCH, 'IV-569K1', '2026-04-01', 700.0, None),
)


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    try:
        for code, master, bill in (MISMATCH, MATCH):
            conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (code, master))
            conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                         (code, bill))
            conn.execute("INSERT INTO customers (code, name, address) VALUES (?, ?, ?)",
                         (code, master, ADDRESS))
        for (code, master, bill), doc, day, net, flag in BILLS:
            conn.execute("DELETE FROM ar_writeoffs WHERE doc_no = ?", (doc,))
            conn.execute("DELETE FROM paid_invoices WHERE doc_no = ?", (doc,))
            conn.execute(
                """INSERT INTO sales_transactions
                     (date_iso, doc_no, doc_base, customer, customer_code,
                      qty, unit, unit_price, vat_type, total, net)
                   VALUES (?, ?, ?, ?, ?, 1, 'ตัว', ?, 1, ?, ?)""",
                (day, doc + '-1', doc, bill, code, net, net, net))
            if flag is not None:
                conn.execute(
                    """INSERT INTO ar_writeoffs
                         (doc_no, customer_code, customer_name, amount, type,
                          writeoff_date, reason, excludes_revenue)
                       VALUES (?, ?, ?, ?, 'expense', '2026-08-01', '#569 fixture', ?)""",
                    (doc, code, bill, net, flag))
        # The pseudo-customers are real rows in the cloned DB. Give each an
        # address inside REGION so a card for it would land on this page.
        for code in MARKETPLACE:
            conn.execute("UPDATE customers SET address = ? WHERE code = ?", (ADDRESS, code))
        conn.commit()
        placed = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE address = ? AND code IN (?, ?, ?, ?)",
            (ADDRESS,) + MARKETPLACE).fetchone()[0]
        assert placed == len(MARKETPLACE), 'a pseudo-customer master row is missing from the clone'
    finally:
        conn.close()


@pytest.fixture
def trip_html(tmp_db):
    _seed(tmp_db)
    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c.get(f'/m/sales-trip?region={quote(REGION)}').get_data(as_text=True)


def _cards(html, code):
    return lxml.html.fromstring(html).xpath(
        "//a[contains(concat(' ', @class, ' '), ' trip-cust ')]"
        "[.//span[contains(@class, 'font-mono') and normalize-space() = $code]]",
        code=code)


def _card(html, code):
    cards = _cards(html, code)
    assert len(cards) == 1, f'expected one trip card for {code}, found {len(cards)}'
    return cards[0]


def _last_sale(card):
    spans = card.xpath(".//span[i[contains(@class, 'bi-clock')]]")
    return spans[0].text_content().strip() if spans else None


def _due(card):
    spans = card.xpath(".//span[contains(@class, 'trip-cust-due')]")
    return float(spans[0].text_content().strip().lstrip('฿').replace(',', '')) if spans else None


def test_every_fixture_buyer_shows_its_last_purchase(trip_html):
    shown = [_last_sale(_card(trip_html, code)) for code, _, _ in (MISMATCH, MATCH)]
    assert len(shown) == 2
    assert shown.count(None) == 0, f'a buyer rendered with a blank ล่าสุด: {shown}'


def test_last_purchase_counts_an_unflagged_writeoff_but_not_a_flagged_one(trip_html):
    assert _last_sale(_card(trip_html, MATCH[0])) == '2026-04-01'
    assert _last_sale(_card(trip_html, MISMATCH[0])) == '2026-06-01'


def test_outstanding_is_read_by_code_and_drops_every_writeoff(trip_html):
    assert _due(_card(trip_html, MATCH[0])) == 700.0
    assert _due(_card(trip_html, MISMATCH[0])) == 1000.0


def test_marketplace_pseudo_customers_are_not_trip_destinations(trip_html):
    assert _card(trip_html, MATCH[0]) is not None
    for code in MARKETPLACE:
        assert _cards(trip_html, code) == [], f'{code} rendered as a shop to visit'
