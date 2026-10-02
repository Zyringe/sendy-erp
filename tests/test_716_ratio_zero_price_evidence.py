"""#716: a unit_conversions row with ratio 0 is a miss for price evidence.

`unit_conversion.word_ratio` hands back the stored 0.0 (pinned in
tests/test_unit_conversion_families.py). Every price-evidence consumer used to
check only `is None` and then divide by it, so one such row raised
ZeroDivisionError and `/call/<code>` returned 500 for that customer. A 0 now
counts as "no ratio", the way the call card already treats it (#668).

Every product: unit_type ตัว, base_sell_price 100, cost_price 60
(tests/_purchase_history_fixture).
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import price_lookup
from models import customers
from tests._purchase_history_fixture import add_line, mk_product

CODE = 'Z716'
NAME = 'ร้านทดสอบเจ็ดหนึ่งหก'
TODAY = '2026-10-02'


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
    c.commit()
    return c


def _product(conn, ratio):
    """A product bought once in กล่อง (ratio `ratio`) and once earlier in ตัว."""
    pid = mk_product(conn, f'สินค้าเจ็ดหนึ่งหก {ratio}')
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'กล่อง', ?)",
                 (pid, ratio))
    add_line(conn, doc_base='IV716A', date_iso='2026-08-01', pid=pid, qty=2, net=180,
             customer=NAME, code=CODE, unit='ตัว')
    add_line(conn, doc_base='IV716B', date_iso='2026-09-01', pid=pid, qty=1, net=500,
             customer=NAME, code=CODE, unit='กล่อง')
    conn.commit()
    return pid


def test_latest_evidence_skips_a_ratio_zero_bill(conn):
    pid = _product(conn, 0.0)
    ev = price_lookup.latest_evidence(conn, pid, CODE, '', today=TODAY)
    assert ev is not None, 'CONTROL: the earlier ตัว bill is still evidence'
    assert (ev['doc_no'], ev['cash_per_unit']) == ('IV716A-1', 90.0)


def test_latest_evidence_control_a_real_ratio_still_converts(conn):
    pid = _product(conn, 10.0)
    ev = price_lookup.latest_evidence(conn, pid, CODE, '', today=TODAY)
    assert (ev['doc_no'], ev['cash_per_unit']) == ('IV716B-1', 50.0)


def test_resolve_price_counts_a_ratio_zero_bill_as_unratioed(conn):
    pid = _product(conn, 0.0)
    out = price_lookup.resolve_price(conn, product_id=pid, customer_code=CODE, today=TODAY)
    assert out['window']['n_unratioed'] == 1
    assert out['context']['lowest']['doc_no'] == 'IV716A-1'


def test_customer_context_skips_a_ratio_zero_bill(conn):
    _product(conn, 0.0)
    ctx = price_lookup._customer_context(conn, CODE, TODAY)
    assert ctx['n_products_12m'] == 1, 'CONTROL: the product is in the 12-month window'


def test_card_cost_reads_a_ratio_zero_unit_as_unknown(conn):
    # Not a divide: ratio 0 printed ทุน 0.00 and a ~100% margin instead.
    pid = _product(conn, 0.0)
    last = {'net': 500.0, 'qty': 1}
    out = customers._card_cost(conn, pid, 'กล่อง', last, [], None)
    assert out['has_cost'] is True, 'CONTROL: the product has a cost'
    assert out['wacc_per_unit'] is None
    assert out['margin_last'] is None


def test_card_cost_ratio_zero_freebie_gives_no_margin(conn):
    pid = _product(conn, 0.0)
    last = {'net': 180.0, 'qty': 2}
    out = customers._card_cost(conn, pid, 'ตัว', last, [{'unit': 'กล่อง', 'qty': 1}], None)
    assert out['wacc_per_unit'] == 60.0, 'CONTROL: the paid ตัว row converts'
    assert out['margin_last'] is None


def test_call_card_with_a_code_renders(conn):
    _product(conn, 0.0)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = client.get('/call/' + CODE)
    assert resp.status_code == 200
    assert 'ไม่มีอัตราแปลง' in resp.get_data(as_text=True), 'CONTROL: the กล่อง row reached the card'
