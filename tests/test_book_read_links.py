"""#501 — a link to ONE document or ONE product opens in the book its page was
rendered under, or explains the mismatch.

The active book lives in the session, so it is shared by every tab. Before
#501 the detail routes read whichever book the session held when the link was
CLICKED: a stale document link got a bare `ไม่พบเอกสาร` 404 (no layout, a dead
end in the standalone PWA), and a stale product link got 200 with a DIFFERENT
product, because the two books number product ids from 1 and never name the
same product with one. Writes were already bound (`expected_book`); reads were
not.

Fixture: the same product id exists in BOTH books under two different names,
and each book holds one document shaped the way that book numbers them (main =
Buddhist-Era year prefix, xp5 = Gregorian). Book switches go through the REAL
`POST /book/toggle`, never a session poke, and every link followed is one
parsed out of a rendered page.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

import pytest

import book_registry as br

PID = 990501                      # same id in both books, two different products
MAIN_NAME = 'สินค้าสมุดหลัก ทดสอบ501'
VAT_NAME = 'สินค้าสมุดVAT ทดสอบ501'
MAIN_DOC = 'IV6950101'            # BSN5657 numbers by Buddhist-Era year
VAT_DOC = 'IV2650101'             # xp5 numbers by Gregorian year
CODE = 'TEST501'
CUST = 'ลูกค้าทดสอบ 501'
VAT_BANNER = 'กำลังดูสมุด VAT (xp5) — อ่านอย่างเดียว'


@pytest.fixture
def books(tmp_db):
    """Main book = the tmp_db copy; VAT book = a file with the main book's
    schema at book_db_path('vat'). Every key below is forced, never inherited."""
    main = sqlite3.connect(tmp_db)
    main.execute("DELETE FROM sales_transactions WHERE doc_base IN (?, ?) "
                 "OR customer_code = ? OR product_id = ?",
                 (MAIN_DOC, VAT_DOC, CODE, PID))
    main.execute("DELETE FROM products WHERE id = ?", (PID,))
    main.execute("INSERT INTO products (id, product_name, unit_type, "
                 "base_sell_price, is_active) VALUES (?, ?, 'ตัว', 100, 1)",
                 (PID, MAIN_NAME))
    main.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
                 "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
                 (CODE, CUST))
    main.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, "
                 "product_id, customer, customer_code, qty, unit, unit_price, "
                 "vat_type, total, net) VALUES ('2026-03-10', ?, ?, ?, ?, ?, "
                 "1, 'ตัว', 100, 1, 100, 100)",
                 (MAIN_DOC + '-1', MAIN_DOC, PID, CUST, CODE))
    main.commit()
    objects = main.execute(
        """SELECT sql FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1
                     WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END"""
    ).fetchall()
    main.close()

    vat = sqlite3.connect(br.book_db_path('vat'))
    vat.execute("PRAGMA foreign_keys = OFF")
    for (sql,) in objects:
        vat.execute(sql)
    vat.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    vat.execute("INSERT INTO book_meta VALUES ('built_at', '2026-09-04T16:55:36')")
    vat.execute("INSERT INTO products (id, product_name, unit_type) "
                "VALUES (?, ?, 'ตัว')", (PID, VAT_NAME))
    vat.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, "
                "product_id, customer, qty, unit, unit_price, vat_type, total, "
                "net) VALUES ('2026-03-15', ?, ?, ?, 'ลูกค้าVAT 501', 2, 'ตัว', "
                "61.725, 2, 123.45, 123.45)",
                (VAT_DOC + '-1', VAT_DOC, PID))
    vat.commit()
    vat.close()
    return tmp_db


def _client(book=None, role='admin'):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = role
        if book:
            s['active_book'] = book
    return c


def _session_book(c):
    with c.session_transaction() as s:
        return s.get('active_book', 'novat')


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.hrefs.append(href)


def _hrefs_to(html, path):
    """Every <a href> whose PATH is exactly `path` (the query is the stamp)."""
    p = _Anchors()
    p.feed(html)
    return [h for h in p.hrefs if urlsplit(h).path == path]


def _book_of(url):
    return parse_qs(urlsplit(url).query).get('book')


# ── build side: the stamp ────────────────────────────────────────────────────

def test_customer_page_document_and_product_links_carry_the_main_book(books):
    r = _client().get(f'/customer/code/{quote(CODE)}')
    html = r.get_data(as_text=True)
    # Control: the FOUND branch rendered. The not-found branch is abort(404),
    # which carries none of these links, so the counts below cannot pass on it.
    assert r.status_code == 200
    docs = _hrefs_to(html, f'/sales/doc/{MAIN_DOC}')
    prods = _hrefs_to(html, f'/products/{PID}')
    assert len(docs) >= 1, 'customer page lost its document link'
    assert len(prods) >= 1, 'customer page lost its product link'
    for href in docs + prods:
        assert _book_of(href) == ['novat'], href


def test_vat_sales_page_document_and_product_links_carry_the_vat_book(books):
    r = _client('vat').get('/sales?date_from=2026-03-01&date_to=2026-03-31')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert VAT_BANNER in html, 'page did not render under the VAT book'
    docs = _hrefs_to(html, f'/sales/doc/{VAT_DOC}')
    prods = _hrefs_to(html, f'/products/{PID}')
    assert len(docs) >= 1 and len(prods) >= 1, (docs, prods)
    for href in docs + prods:
        assert _book_of(href) == ['vat'], href


def test_only_entity_urls_on_book_scoped_routes_are_stamped(books):
    """One document / one product on a PARITY route → stamped. A list or
    navigation URL with no entity key, and any non-parity route, keep
    following the session book exactly as before."""
    from flask import session, url_for
    from app import app as flask_app
    with flask_app.test_request_context('/'):
        session['role'] = 'admin'
        stamped = [
            url_for('sales.sales_doc', doc_base='IV1'),
            url_for('sales.purchases_doc', doc_base='HP1'),
            url_for('products.product_detail', product_id=5),
            url_for('products.product_detail', product_id=5, txn_page=2),
            url_for('sales.sales_view', product_id=5),
            url_for('inventory.transaction_history', product_id=5),
        ]
        unstamped = [
            url_for('sales.sales_view'),
            url_for('sales.sales_view', page=2, date_from='2026-01-01'),
            url_for('sales.sales_view', product_id=''),
            url_for('sales.sales_view', doc_no='IV69'),      # a search filter
            url_for('sales.purchases_view'),
            url_for('products.product_list'),
            url_for('inventory.transaction_history', type='IN'),
            url_for('products.product_edit', product_id=5),   # not a parity route
            url_for('partners.customer_detail', customer_code='X'),
        ]
        assert len(stamped) == 6 and len(unstamped) == 9
        for u in stamped:
            assert _book_of(u) == ['novat'], u
        for u in unstamped:
            assert _book_of(u) is None, u

    with flask_app.test_request_context('/'):
        session['role'] = 'admin'
        session['active_book'] = 'vat'
        assert _book_of(url_for('sales.sales_doc', doc_base='IV1')) == ['vat']
        assert _book_of(url_for('products.product_detail', product_id=5)) == ['vat']
