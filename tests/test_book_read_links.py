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


class _Notice(HTMLParser):
    """Text, forms (+ their inputs) and links inside the FIRST element that
    carries data-book-link="<mode>" — the #501 page's own root element, so
    nothing the layout or a <script> says can satisfy an assertion on it."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
            'link', 'meta', 'source', 'track', 'wbr'}

    def __init__(self, mode):
        super().__init__()
        self.mode, self.found, self.depth = mode, False, 0
        self.text, self.forms, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.found and a.get('data-book-link') == self.mode:
            self.found, self.depth = True, 1
            return
        if not self.depth:
            return
        if tag == 'form':
            self.forms.append({'action': a.get('action'), 'inputs': {}})
        elif tag == 'input' and self.forms:
            self.forms[-1]['inputs'][a.get('name')] = a.get('value')
        elif tag == 'a':
            self.links.append(a)
        if tag not in self.VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth and tag not in self.VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


def _notice(html, mode):
    p = _Notice(mode)
    p.feed(html)
    p.text = ' '.join(' '.join(p.text).split())
    return p


def _customer_page_link(c, path):
    html = c.get(f'/customer/code/{quote(CODE)}').get_data(as_text=True)
    hrefs = _hrefs_to(html, path)
    assert len(hrefs) >= 1, f'no link to {path} on the customer page'
    return hrefs[0]


def _vat_sales_page_link(c, path):
    html = c.get('/sales?date_from=2026-03-01&date_to=2026-03-31').get_data(as_text=True)
    assert VAT_BANNER in html, '/sales did not render under the VAT book'
    hrefs = _hrefs_to(html, path)
    assert len(hrefs) >= 1, f'no link to {path} on the VAT /sales page'
    return hrefs[0]


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


# ── serve side: a stale link is explained, never read ───────────────────────

def test_stale_document_link_shows_the_mismatch_page_not_a_404(books):
    c = _client()
    href = _customer_page_link(c, f'/sales/doc/{MAIN_DOC}')
    control = c.get(href).get_data(as_text=True)
    assert MAIN_NAME in control, 'control: same book, the link opens the document'

    c.post('/book/toggle', data={'book': 'vat'})
    assert _session_book(c) == 'vat'
    r = c.get(href)
    body = r.get_data(as_text=True)

    assert r.status_code == 409
    notice = _notice(body, 'mismatch')
    assert notice.found, 'no mismatch page'
    assert VAT_BANNER in body, 'red VAT banner missing — the layout did not render'
    # it names both books
    assert br.BOOKS['novat']['label'] in notice.text
    assert br.BOOKS['vat']['label'] in notice.text
    # the document's data from NEITHER book
    for leaked in (MAIN_NAME, CUST, VAT_NAME, 'ลูกค้าVAT 501'):
        assert leaked not in body, leaked
    assert _session_book(c) == 'vat', 'the read changed the session'


def test_stale_product_link_main_to_vat_reads_neither_book(books):
    c = _client()
    href = _customer_page_link(c, f'/products/{PID}')
    assert MAIN_NAME in c.get(href).get_data(as_text=True), 'control'

    c.post('/book/toggle', data={'book': 'vat'})
    r = c.get(href)
    body = r.get_data(as_text=True)

    assert r.status_code == 409
    assert _notice(body, 'mismatch').found
    assert MAIN_NAME not in body
    assert VAT_NAME not in body, 'showed the VAT book product that shares the id'


def test_stale_product_link_vat_to_main_reads_neither_book(books):
    """The direction that showed nothing unusual before #501: a VAT-rendered
    product link followed after switching back to the main book opened the
    MAIN product with that id, with no red banner to hint at it."""
    c = _client()
    c.post('/book/toggle', data={'book': 'vat'})
    href = _vat_sales_page_link(c, f'/products/{PID}')
    assert VAT_NAME in c.get(href).get_data(as_text=True), 'control'

    c.post('/book/toggle', data={'book': 'novat'})
    assert _session_book(c) == 'novat'
    r = c.get(href)
    body = r.get_data(as_text=True)

    assert r.status_code == 409
    notice = _notice(body, 'mismatch')
    assert notice.found
    assert br.BOOKS['vat']['label'] in notice.text
    assert MAIN_NAME not in body, 'showed the main book product that shares the id'
    assert VAT_NAME not in body
    assert VAT_BANNER not in body       # the session really is the main book


def test_stale_product_filtered_sales_link_reads_neither_book(books):
    """`/sales?product_id=` from the product page names one product too."""
    c = _client()
    html = c.get(f'/products/{PID}').get_data(as_text=True)
    hrefs = [h for h in _hrefs_to(html, '/sales')
             if parse_qs(urlsplit(h).query).get('product_id') == [str(PID)]]
    assert len(hrefs) == 1, hrefs
    assert _book_of(hrefs[0]) == ['novat']

    c.post('/book/toggle', data={'book': 'vat'})
    r = c.get(hrefs[0])
    body = r.get_data(as_text=True)
    assert r.status_code == 409
    assert _notice(body, 'mismatch').found
    assert MAIN_NAME not in body and VAT_NAME not in body


def test_matching_absent_and_unknown_book_read_as_before(books):
    vat = _client('vat')
    assert VAT_NAME in vat.get(f'/products/{PID}?book=vat').get_data(as_text=True)
    assert VAT_NAME in vat.get(f'/products/{PID}').get_data(as_text=True)
    r = vat.get(f'/sales/doc/{VAT_DOC}?book=vat')
    assert r.status_code == 200 and VAT_NAME in r.get_data(as_text=True)

    main = _client()
    assert MAIN_NAME in main.get(f'/products/{PID}?book=novat').get_data(as_text=True)
    assert MAIN_NAME in main.get(f'/products/{PID}').get_data(as_text=True)
    # an unrecognised value is treated as absent
    assert MAIN_NAME in main.get(f'/products/{PID}?book=hack').get_data(as_text=True)
    r = main.get(f'/sales/doc/{MAIN_DOC}')
    assert r.status_code == 200 and MAIN_NAME in r.get_data(as_text=True)


def test_mismatched_json_read_gets_409_with_both_books(books):
    c = _client('vat')
    r = c.get(f'/products/{PID}?book=novat', headers={'Accept': 'application/json'})
    assert r.status_code == 409
    j = r.get_json()
    assert (j['expected_book'], j['active_book']) == ('novat', 'vat')

    r = c.get(f'/api/products/{PID}/barcodes?book=novat')
    assert r.status_code == 409
    j = r.get_json()
    assert (j['expected_book'], j['active_book']) == ('novat', 'vat')
    assert _session_book(c) == 'vat'


# ── the one-tap switch and its landing target ───────────────────────────────

def _switch_form(body):
    notice = _notice(body, 'mismatch')
    assert notice.found, 'no mismatch page'
    forms = [f for f in notice.forms if urlsplit(f['action']).path == '/book/toggle']
    assert len(forms) == 1, notice.forms
    return forms[0]['inputs']


def test_switch_control_lands_on_the_same_document_in_the_link_book(books):
    c = _client()
    href = _customer_page_link(c, f'/sales/doc/{MAIN_DOC}')
    c.post('/book/toggle', data={'book': 'vat'})
    inputs = _switch_form(c.get(href).get_data(as_text=True))
    assert inputs['book'] == 'novat'

    r = c.post('/book/toggle', data=inputs, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert _session_book(c) == 'novat'
    assert r.status_code == 200
    assert r.request.path == f'/sales/doc/{MAIN_DOC}'
    assert f'เอกสารขาย {MAIN_DOC}' in body          # the doc page's own title
    assert MAIN_NAME in body                         # its line, from the main book
    assert VAT_BANNER not in body


def test_switch_control_lands_on_the_same_product_in_the_vat_book(books):
    c = _client()
    c.post('/book/toggle', data={'book': 'vat'})
    href = _vat_sales_page_link(c, f'/products/{PID}')
    c.post('/book/toggle', data={'book': 'novat'})
    inputs = _switch_form(c.get(href).get_data(as_text=True))
    assert inputs['book'] == 'vat'

    r = c.post('/book/toggle', data=inputs, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert _session_book(c) == 'vat'
    assert r.request.path == f'/products/{PID}'
    assert VAT_NAME in body
    assert MAIN_NAME not in body
    assert VAT_BANNER in body


@pytest.mark.parametrize('target', [
    'https://evil.example/sales',
    '//evil.example/sales',
    '/\\evil.example/sales',
    f'/customer/code/{CODE}',        # main-book-only: cannot be a VAT landing
    '/no/such/page',
])
def test_vat_switch_ignores_an_outside_or_non_parity_landing(books, target):
    c = _client()
    r = c.post('/book/toggle', data={'book': 'vat', 'next': target})
    loc = r.headers['Location']
    assert _session_book(c) == 'vat'                 # the switch itself happened
    assert r.status_code == 302
    assert 'evil' not in loc
    assert urlsplit(loc).path == '/sales' and not urlsplit(loc).query, loc


@pytest.mark.parametrize('target', [
    'https://evil.example/',
    '//evil.example/',
    '/\\evil.example/',
    'javascript:alert(1)',
    '/no/such/page',
])
def test_main_switch_ignores_an_outside_landing(books, target):
    c = _client('vat')
    r = c.post('/book/toggle', data={'book': 'novat', 'next': target})
    loc = r.headers['Location']
    assert _session_book(c) == 'novat'
    assert r.status_code == 302
    assert 'evil' not in loc
    assert urlsplit(loc).path == '/' and not urlsplit(loc).query, loc


def test_switch_honours_an_internal_landing_the_target_book_can_render(books):
    c = _client()
    r = c.post('/book/toggle', data={'book': 'vat', 'next': f'/products/{PID}?book=vat'})
    loc = urlsplit(r.headers['Location'])
    assert (loc.path, parse_qs(loc.query)) == (f'/products/{PID}', {'book': ['vat']})

    r = c.post('/book/toggle', data={'book': 'novat', 'next': f'/customer/code/{CODE}'})
    assert urlsplit(r.headers['Location']).path == f'/customer/code/{CODE}'


# ── a document that is not in this book: 404, but inside the layout ─────────

class _Tags(HTMLParser):
    def __init__(self, tag):
        super().__init__()
        self.tag, self.found = tag, []

    def handle_starttag(self, tag, attrs):
        if tag == self.tag:
            self.found.append(dict(attrs))


def _tags(html, tag):
    p = _Tags(tag)
    p.feed(html)
    return p.found


@pytest.mark.parametrize('path,back', [
    ('/sales/doc/ZZ501NOPE', '/sales'),
    ('/purchases/doc/ZZ501NOPE', '/purchases'),
])
def test_document_not_found_is_a_404_inside_the_layout(books, path, back):
    r = _client().get(path)
    body = r.get_data(as_text=True)
    assert r.status_code == 404
    notice = _notice(body, 'not_found')
    assert notice.found, 'bare not-found response, no page'
    assert 'ZZ501NOPE' in notice.text
    backs = [a for a in notice.links if 'data-book-link-back' in a]
    assert len(backs) == 1 and backs[0]['href'] == back
    # the layout: the sidebar, and the book strip whose form is the switch
    assert [a for a in _tags(body, 'aside') if a.get('id') == 'sidebar']
    toggles = [f for f in _tags(body, 'form')
               if urlsplit(f.get('action', '')).path == '/book/toggle']
    assert len(toggles) == 1, 'the banner (and its switch) did not render'


def test_document_not_found_under_the_vat_book_keeps_the_red_banner(books):
    r = _client('vat').get(f'/sales/doc/{MAIN_DOC}')   # a main-book doc, no stamp
    body = r.get_data(as_text=True)
    assert r.status_code == 404
    assert _notice(body, 'not_found').found
    assert VAT_BANNER in body
    assert MAIN_NAME not in body
