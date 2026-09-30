"""Card C, P3: the call card reads its customer's history from purchase_history.

Put's decisions (decisions/log.md 2026-09-30, "Round-3 cards C/E/F"):
  B1  a product's ล่าสุด is the last PAID purchase (not a credit note, not a freebie)
  C1  a product the customer only returned or got free leaves the ซื้อประจำ table
  D1  a document invoiced in error (a flagged giveaway) leaves the quantities
  E1  ซื้อครั้งแรก is the first PAID purchase
and, not a decision, the header is keyed on the customer key, not on ONE bill name.

Fixture: one customer code with TWO bill names (the old header read `names[0]`
only), a credit note dated BEFORE the first invoice, a credit note dated AFTER
the last invoice, a returned-only product, a freebie-only product, and a
flagged giveaway document. Every expected number is a hand count of the table
below, not a re-run of the code under test.

  doc      date        bill name  lines
  SR-0     2025-12-01  N1         PB  2 ตัว  ฿200            credit note, first document
  IV-1     2026-01-10  N1         PA 10 ตัว ฿1,000
  IV-2     2026-02-15  N2         PA  5 ตัว ฿500  +  PC 3 ตัว ฿0   (freebie-only product)
  GV-3     2026-03-01  N1         PA  7 ตัว ฿700               flagged excludes_revenue=1
  SR-4     2026-03-20  N1         PA  2 ตัว ฿200            credit note, after the last invoice
  IV-1 also carries PD 4 ตัว ฿400 + PD 1 ตัว ฿0 (a free unit inside a paid bill)
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import call_card
import models
from tests._purchase_history_fixture import add_line, mk_product, writeoff

CODE = 'ZCP3A'
N1, N2 = 'ร้านทดสอบพีสาม หนึ่ง', 'ร้านทดสอบพีสาม สอง'


@pytest.fixture
def card_conn(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, N1))
    pa = mk_product(c, 'สินค้าพีสาม เอ')
    pb = mk_product(c, 'สินค้าพีสาม บี คืนอย่างเดียว')
    pc = mk_product(c, 'สินค้าพีสาม ซี แถมอย่างเดียว')
    pd = mk_product(c, 'สินค้าพีสาม ดี')
    kw = dict(code=CODE)
    add_line(c, doc_base='SR-0', date_iso='2025-12-01', pid=pb, qty=2, net=200, customer=N1, **kw)
    add_line(c, doc_base='IV-1', date_iso='2026-01-10', pid=pa, qty=10, net=1000, customer=N1, **kw)
    add_line(c, doc_base='IV-1', date_iso='2026-01-10', pid=pd, qty=4, net=400, customer=N1,
             suffix=2, **kw)
    add_line(c, doc_base='IV-1', date_iso='2026-01-10', pid=pd, qty=1, net=0, customer=N1,
             suffix=3, **kw)
    add_line(c, doc_base='IV-2', date_iso='2026-02-15', pid=pa, qty=5, net=500, customer=N2, **kw)
    add_line(c, doc_base='IV-2', date_iso='2026-02-15', pid=pc, qty=3, net=0, customer=N2,
             suffix=2, **kw)
    add_line(c, doc_base='GV-3', date_iso='2026-03-01', pid=pa, qty=7, net=700, customer=N1, **kw)
    writeoff(c, 'GV-3', 1, CODE)
    add_line(c, doc_base='SR-4', date_iso='2026-03-20', pid=pa, qty=2, net=200, customer=N1, **kw)
    c.commit()
    return c


def _pid(conn, tag):
    name = {'A': 'สินค้าพีสาม เอ', 'B': 'สินค้าพีสาม บี คืนอย่างเดียว',
            'C': 'สินค้าพีสาม ซี แถมอย่างเดียว', 'D': 'สินค้าพีสาม ดี'}[tag]
    return conn.execute("SELECT id FROM products WHERE product_name = ?", (name,)).fetchone()[0]


def _by_pid(card):
    return {p['product_id']: p for p in card['products']}


def test_header_equals_the_desktop_page_for_the_same_key(card_conn):
    """The anti-drift pin. The header used to be keyed on ONE bill name (`names[0]`):
    with two names on this code it showed half the history. Now it is the page's."""
    card = call_card.get_card(card_conn, CODE)
    page = models.get_customer_summary_by_code(CODE)['summary']
    s = card['summary']['summary']

    # 1000 + 400 (PD) + 500 - 200 (SR-0) - 200 (SR-4); GV-3 is invoiced in error and is out.
    assert s['total_net'] == 1500
    assert s['total_net'] == page['total_net'], 'call card header != desktop page (ยอดซื้อรวม)'
    assert s['purchase_doc_count'] == 2 == page['purchase_doc_count']    # IV-1, IV-2
    assert s['last_purchase_date'] == '2026-02-15' == page['last_purchase_date']


def test_first_purchase_skips_a_leading_credit_note(card_conn):
    """E1: SR-0 (2025-12-01) is the first DOCUMENT; the first PAID purchase is IV-1."""
    s = call_card.get_card(card_conn, CODE)['summary']['summary']
    assert s['first_date'] == '2025-12-01', 'CONTROL: the raw first document is still SR-0'
    assert s['first_purchase_date'] == '2026-01-10'


def test_last_buy_skips_a_credit_note_dated_after_the_last_invoice(card_conn):
    """B1: SR-4 (2026-03-20) and the flagged GV-3 (2026-03-01) are not purchases."""
    p = _by_pid(call_card.get_card(card_conn, CODE))[_pid(card_conn, 'A')]
    assert p['last_buy'] == '2026-02-15'


def test_products_only_returned_or_given_free_are_absent(card_conn):
    card = call_card.get_card(card_conn, CODE)
    by = _by_pid(card)
    assert list(by) == [_pid(card_conn, 'A'), _pid(card_conn, 'D')], \
        'CONTROL: the two bought products, ranked by money (1,300 then 400)'
    assert _pid(card_conn, 'B') not in by, 'returned-only product still listed (C1)'
    assert _pid(card_conn, 'C') not in by, 'freebie-only product still listed (C1)'


def test_giveaway_document_does_not_count_in_the_quantity(card_conn):
    """D1: 10 + 5 - 2 = 13. The flagged GV-3's 7 ตัว are not ซื้อรวม."""
    p = _by_pid(call_card.get_card(card_conn, CODE))[_pid(card_conn, 'A')]
    assert p['total_qty'] == 13
    assert p['total_net'] == 1300


def test_free_units_inside_a_paid_bill_still_count_in_the_quantity(card_conn):
    """Not a decision, the old behaviour kept: ซื้อรวม counts EVERY counted line, so the
    แถม unit on IV-1 is in it (4 paid + 1 free = 5). The customer page's stricter
    figure would say 4; only D1's flagged giveaway is taken out."""
    p = _by_pid(call_card.get_card(card_conn, CODE))[_pid(card_conn, 'D')]
    assert p['total_qty'] == 5
    assert p['total_net'] == 400


def test_doc_count_is_times_bought(card_conn):
    """B/M8: per-product doc_count is the purchase population's IV-1 + IV-2, not the raw
    five documents (SR-4 and GV-3 are on the raw count)."""
    p = _by_pid(call_card.get_card(card_conn, CODE))[_pid(card_conn, 'A')]
    assert p['doc_count'] == 2


def test_orders_modal_reads_the_customer_key_not_one_name(card_conn):
    """The ประวัติซื้อ modal was `customer IN (names)`; it is the key now, and it still
    lists every line of the product (credit note and giveaway included: a history)."""
    p = _by_pid(call_card.get_card(card_conn, CODE))[_pid(card_conn, 'A')]
    assert sorted(o['doc_no'] for o in p['orders']) == \
        ['GV-3-1', 'IV-1-1', 'IV-2-1', 'SR-4-1']


def test_rendered_last_buy_cell_shows_the_invoice_month(card_conn):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = client.get('/call/' + CODE)
    assert resp.status_code == 200, 'CONTROL: get_card returned None and the route redirected'
    html = resp.get_data(as_text=True)
    m = re.search(r'สินค้าพีสาม เอ</span>.*?</td>\s*'          # name cell
                  r'<td[^>]*>\s*(.*?)\s*</td>\s*'               # ซื้อรวม
                  r'<td[^>]*>\s*(.*?)\s*</td>', html, re.S)     # ล่าสุด
    assert m, 'CONTROL: the product row did not render'
    assert m.group(1) == '13 ตัว'
    assert m.group(2) == '2026-02', 'ล่าสุด shows the credit-note month (2026-03)'


class _Seam:
    """A connection proxy that runs `hook()` once, right after the first per-product
    read has been fetched (before any second read): an import committing a removal
    on another connection between two statements."""

    def __init__(self, conn, hook):
        self._c, self._hook, self._done = conn, hook, False

    def execute(self, sql, params=()):
        cur = self._c.execute(sql, params)
        if self._done or 'times_bought' not in sql:
            return cur
        outer = self

        class _Cur:
            def fetchall(_self):
                rows = cur.fetchall()
                outer._done = True
                outer._hook()
                return rows

        return _Cur()


def test_products_counted_survives_an_import_removing_lines_mid_read(card_conn, empty_db):
    """W2 (#699 review): the counted totals used to be a SECOND statement, indexed by the
    first one's (product, unit); lines removed between the two raised KeyError and 500'd
    the call card. One statement has no seam."""
    import sqlite3
    import purchase_history

    def remove_pa_lines():
        w = sqlite3.connect(empty_db)
        w.execute("DELETE FROM sales_transactions WHERE product_id = ?",
                  (_pid(card_conn, 'A'),))
        w.commit()
        w.close()

    rows = purchase_history.products(_Seam(card_conn, remove_pa_lines), CODE, counted=True)
    assert {r['product_id'] for r in rows} == {_pid(card_conn, 'A'), _pid(card_conn, 'D')}, \
        'CONTROL: the read saw both products before the removal'
    assert card_conn.execute("SELECT COUNT(*) FROM sales_transactions WHERE product_id = ?",
                             (_pid(card_conn, 'A'),)).fetchone()[0] == 0, \
        'CONTROL: the removal really was committed mid-read'
