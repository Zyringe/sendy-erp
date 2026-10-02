"""#720: a unit_conversions row with ratio 0 resolves like no row at all.

`_resolve_unit` returned the stored 0.0, so an ask in that unit priced the
list at ฿0 and the cost at ฿0, and resolve_price divided by the ฿0 lowest
bill: /customer/code/<code> returned 500. A ratio-0 row is now skipped and
the chain goes on (โหล tier, any tier, the base unit). With nothing else to
answer, a strict ask raises the usual ValueError and a non-strict lookup gets
None, never the non-strict miss value 1.0.

Every product: unit_type ตัว, base_sell_price 100, cost_price 60
(tests/_purchase_history_fixture).
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import price_lookup
from tests._purchase_history_fixture import add_line, mk_product

CODE = 'Z720'
NAME = 'ร้านทดสอบเจ็ดสองศูนย์'
TODAY = '2026-10-02'


@pytest.fixture
def conn(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (CODE, NAME))
    c.commit()
    return c


def _product(conn, rows, tiers=(), base=100.0):
    pid = mk_product(conn, f'สินค้าเจ็ดสองศูนย์ {rows} {tiers} {base}')
    conn.execute("UPDATE products SET base_sell_price = ? WHERE id = ?", (base, pid))
    conn.executemany("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, ?, ?)",
                     [(pid, u, r) for u, r in rows])
    conn.executemany("INSERT INTO product_price_tiers (product_id, qty_label, price) VALUES (?, ?, ?)",
                     [(pid, label, price) for label, price in tiers])
    conn.commit()
    return pid


def test_strict_ask_in_a_ratio_zero_unit_raises_and_does_not_list_it(conn):
    pid = _product(conn, [('กล่อง', 0.0), ('โหล', 12.0)])
    with pytest.raises(ValueError) as e:
        price_lookup._resolve_unit(conn, pid, 'กล่อง', 'ตัว', strict=True)
    resolves = str(e.value).split('units that DO resolve:')[1]
    assert 'โหล' in resolves, 'CONTROL: the message lists the units that do resolve'
    assert 'กล่อง' not in resolves


def test_non_strict_lookup_is_unknown_never_one(conn):
    pid = _product(conn, [('กล่อง', 0.0)])
    assert price_lookup._resolve_unit(conn, pid, 'กล่อง', 'ตัว')[:2] == (None, 'unknown')


def test_control_a_unit_with_no_row_still_reads_one_when_non_strict(conn):
    # The non-strict miss value is 1.0 (pinned in test_unit_conversion_families);
    # a ratio-0 row must not inherit it.
    pid = _product(conn, [])
    assert price_lookup._resolve_unit(conn, pid, 'กล่อง', 'ตัว')[:2] == (1.0, 'none')


def test_dozen_tier_answers_past_a_ratio_zero_row(conn):
    pid = _product(conn, [('โหล', 0.0)], tiers=[('1 โหล', 1000.0)])
    assert price_lookup._resolve_unit(conn, pid, 'โหล', 'ตัว', strict=True)[:2] == (12.0, 'tier-implied')


def test_other_tier_answers_with_ratio_unknown(conn):
    pid = _product(conn, [('กล่อง', 0.0)], tiers=[('1 กล่อง', 900.0)])
    ratio, source, tier = price_lookup._resolve_unit(conn, pid, 'กล่อง', 'ตัว', strict=True)
    assert (ratio, source) == (None, 'unknown')
    assert tier is not None


def test_ratio_zero_row_on_the_base_unit_is_the_base_unit(conn):
    pid = _product(conn, [('ตัว', 0.0)])
    assert price_lookup._resolve_unit(conn, pid, 'ตัว', 'ตัว', strict=True)[:2] == (1.0, 'none')


def test_resolve_price_with_a_customer_raises_value_error_not_zero_division(conn):
    pid = _product(conn, [('กล่อง', 0.0)])
    add_line(conn, doc_base='IV720A', date_iso='2026-08-01', pid=pid, qty=2, net=180,
             customer=NAME, code=CODE, unit='ตัว')
    conn.commit()
    with pytest.raises(ValueError):
        price_lookup.resolve_price(conn, product_id=pid, customer_code=CODE, unit='กล่อง',
                                   today=TODAY)


def test_dozen_only_product_with_a_ratio_zero_dozen_row(conn):
    # base price 0, sold only by the โหล tier: the answer switches to โหล and
    # converts qty by the โหล ratio, which used to be the stored 0.
    pid = _product(conn, [('โหล', 0.0)], tiers=[('1 โหล', 1200.0)], base=0.0)
    out = price_lookup.resolve_price(conn, product_id=pid, unit='ตัว', qty=24, today=TODAY)
    assert out['list']['list_source'] == 'dozen-only', 'CONTROL: the dozen-only branch ran'
    assert out['unit']['ratio'] == 12.0


def test_customer_page_renders(conn):
    pid = _product(conn, [('กล่อง', 0.0)])
    add_line(conn, doc_base='IV720A', date_iso='2026-08-01', pid=pid, qty=2, net=180,
             customer=NAME, code=CODE, unit='ตัว')
    add_line(conn, doc_base='IV720B', date_iso='2026-09-01', pid=pid, qty=1, net=500,
             customer=NAME, code=CODE, unit='กล่อง')
    conn.commit()
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'
    resp = client.get('/customer/code/' + CODE)
    assert resp.status_code == 200
    assert 'สินค้าเจ็ดสองศูนย์' in resp.get_data(as_text=True), 'CONTROL: the product card rendered'
