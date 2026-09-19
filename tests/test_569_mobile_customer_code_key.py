"""#569: the mobile customer surface is keyed on the customers-master CODE.

A rep taps a /m/sales-trip card and lands on /m/customer. Both were keyed on the
master NAME, which the bill name drifts from (the master carries a legal-entity
prefix such as หจก. that the bill does not), so a real buyer's page listed no
documents. Two master rows can also share one name, and a name key then opens
the wrong shop.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from urllib.parse import quote

import lxml.html
import pytest

REGION = 'ภาคตะวันออก'
ADDRESS = '123 ถ.สุขุมวิท ชลบุรี'

# (code, master name, bill name, doc_base)
MISMATCH = ('C-569M', 'หจก. ร้านทดสอบห้าหกเก้า', 'ร้านทดสอบห้าหกเก้า', 'IV-569M')
TWIN_A = ('C-569A', 'ร้านชื่อซ้ำห้าหกเก้า', 'ร้านชื่อซ้ำห้าหกเก้า', 'IV-569A')
TWIN_B = ('C-569B', 'ร้านชื่อซ้ำห้าหกเก้า', 'ร้านชื่อซ้ำห้าหกเก้า สาขา2', 'IV-569B')
CUSTOMERS = (MISMATCH, TWIN_A, TWIN_B)


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    try:
        for code, master, bill, doc in CUSTOMERS:
            conn.execute("DELETE FROM customers WHERE code = ? OR name = ?", (code, master))
            conn.execute("DELETE FROM sales_transactions WHERE doc_base = ? OR customer = ?",
                         (doc, bill))
        for code, master, bill, doc in CUSTOMERS:
            conn.execute("INSERT INTO customers (code, name, address) VALUES (?, ?, ?)",
                         (code, master, ADDRESS))
            conn.execute(
                """INSERT INTO sales_transactions
                     (date_iso, doc_no, doc_base, customer, customer_code,
                      qty, unit, unit_price, vat_type, total, net)
                   VALUES ('2026-07-01', ?, ?, ?, ?, 1, 'ตัว', 500.0, 1, 500.0, 500.0)""",
                (doc + '-1', doc, bill, code))
        conn.commit()
        dups = conn.execute("SELECT COUNT(*) FROM customers WHERE name = ?",
                            (TWIN_A[1],)).fetchone()[0]
        assert dups == 2, 'fixture lost the shared master name it exists to test'
    finally:
        conn.close()


@pytest.fixture
def client(tmp_db):
    _seed(tmp_db)
    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


def _card_href(client, code):
    html = client.get(f'/m/sales-trip?region={quote(REGION)}').get_data(as_text=True)
    tree = lxml.html.fromstring(html)
    cards = tree.xpath(
        "//a[contains(concat(' ', @class, ' '), ' trip-cust ')]"
        "[.//span[contains(@class, 'font-mono') and normalize-space() = $code]]",
        code=code)
    assert len(cards) == 1, f'expected one trip card for {code}, found {len(cards)}'
    return cards[0].get('href')


def _recent_docs(html):
    tree = lxml.html.fromstring(html)
    return [d.text_content().split()[0] for d in tree.xpath(
        "//div[contains(@class, 'card')]"
        "[div[contains(@class, 'card-header')][contains(., 'ขายล่าสุด')]]"
        "//div[contains(@class, 'font-mono')]")]


def _doc_count(html):
    tree = lxml.html.fromstring(html)
    cells = tree.xpath(
        "//div[div[normalize-space() = 'เอกสารทั้งหมด']]/div[contains(@class, 'fs-5')]")
    return int(cells[0].text_content().strip().replace(',', '')) if cells else None


@pytest.mark.parametrize('customer', CUSTOMERS, ids=lambda c: c[0])
def test_trip_card_opens_the_page_of_its_own_code(client, customer):
    code, _master, _bill, doc = customer
    resp = client.get(_card_href(client, code), follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    docs = _recent_docs(html)
    assert len(docs) == 1, f'{code} page lists {docs}, expected exactly its own document'
    assert docs == [doc]
    assert _doc_count(html) == 1


@pytest.mark.parametrize('customer', CUSTOMERS, ids=lambda c: c[0])
def test_full_details_link_reaches_the_same_customer(client, customer):
    code, _master, _bill, doc = customer
    page = client.get(_card_href(client, code)).get_data(as_text=True)
    links = lxml.html.fromstring(page).xpath(
        "//a[normalize-space() = 'ดูรายละเอียดเต็ม →']/@href")
    assert len(links) == 1
    desktop = client.get(links[0], follow_redirects=True).get_data(as_text=True)
    assert doc in desktop, f'desktop page reached from {code} does not show {doc}'
