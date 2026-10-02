"""#673 — the call card and a promo with a minimum quantity.

The card has no quantity, and its `customer_price` was never rendered: it is
gone (plan, /interrogate item 10). What the card still shows is the promo row,
picked by the SAME selector resolve_price uses, and the promo cell prints the
minimum ("ซื้อ ≥ N <unit>") through the promo_summary macro.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import call_card
import price_lookup
from tests._purchase_history_fixture import add_line, mk_product

CODE = 'Z673'
NAME = 'ร้านทดสอบหกเจ็ดสาม'


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
    c.commit()
    return c


def _gated(conn):
    pid = mk_product(conn, 'สินค้าโปรขั้นต่ำ')
    conn.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
                 "is_active, min_qty, min_qty_unit) VALUES (?, 'ลด 5%', 'percent', 5, 1, 20, 'ตัว')",
                 (pid,))
    add_line(conn, doc_base='IV673', date_iso='2026-09-01', pid=pid, qty=1, net=100,
             customer=NAME, code=CODE)
    conn.commit()
    return pid


def test_card_row_carries_no_price_of_its_own_and_the_resolvers_promo(conn):
    pid = _gated(conn)
    rows = [p for p in call_card._assemble_products(conn, CODE, CODE, today='2026-10-02')
            if p['product_id'] == pid]
    assert len(rows) == 1, 'CONTROL: the line must reach the card'
    row = rows[0]
    assert 'customer_price' not in row
    resolved = price_lookup.resolve_price(conn, product_id=pid, qty=None, today='2026-10-02')
    assert row['promo']['id'] == resolved['list']['price_promo']['id']
    assert row['promo']['min_qty'] == 20
    assert resolved['list']['promo_gate'] == 'qty_unknown'


def test_rendered_card_states_the_minimum(conn):
    _gated(conn)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = client.get('/call/' + CODE)
    assert resp.status_code == 200
    cells = re.findall(r'<div class="cc-promo">(.*?)</div>', resp.get_data(as_text=True), re.S)
    assert len(cells) == 1, 'CONTROL: the promo cell rendered'
    assert 'ลด 5%' in cells[0] and 'ซื้อ ≥ 20 ตัว' in cells[0]
