"""Tests for marketplace order import (parse_orders.py + models + routes).

Fixtures are synthetic (no real customer PII): real product names/amounts are
fine, buyer fields are fake.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')  # don't init the live DB when importing app

import pandas as pd
import pytest

from parse_orders import (parse_shopee_orders, parse_lazada_orders,
                          _SP, _LZ, _num, _s)


def _shopee_df(rows):
    """Build a Shopee-export-shaped DataFrame from a list of partial dicts,
    filling every column the parser reads so missing-column logic isn't tripped."""
    cols = [_SP.ORDER, _SP.STATUS, _SP.ORDER_DATE, _SP.PAID_TIME, _SP.ITEM_NAME,
            _SP.SKU_REF, _SP.VAR_NAME, _SP.SELL_PRICE, _SP.QTY, _SP.NET_SELL,
            _SP.COMMISSION, _SP.TXN_FEE, _SP.SVC_FEE, _SP.TOTAL, _SP.RECIPIENT,
            _SP.PHONE, _SP.ADDR, _SP.PROVINCE, _SP.DISTRICT, _SP.ZIP]
    full = [{c: r.get(c, '') for c in cols} for r in rows]
    return pd.DataFrame(full, columns=cols)


def test_single_line_order():
    df = _shopee_df([{
        _SP.ORDER: '260530R46WR25G', _SP.STATUS: 'ที่ต้องจัดส่ง',
        _SP.ORDER_DATE: '2026-05-30 15:32', _SP.PAID_TIME: '2026-05-30 16:02',
        _SP.ITEM_NAME: 'มือจับหน้าต่าง มือจับบัว 5 นิ้ว SENDAI', _SP.VAR_NAME: '',
        _SP.SELL_PRICE: '9.00', _SP.QTY: '1', _SP.NET_SELL: '9.00',
        _SP.COMMISSION: '1.00', _SP.TXN_FEE: '0.00', _SP.SVC_FEE: '1.00',
        _SP.TOTAL: '9.00', _SP.RECIPIENT: 'สมชาย', _SP.PHONE: '0800000000',
        _SP.ADDR: '99/9', _SP.DISTRICT: 'อำเภอบ้านธิ', _SP.PROVINCE: 'จังหวัดลำพูน', _SP.ZIP: '51180',
    }])
    orders = parse_shopee_orders(df)
    assert len(orders) == 1
    o = orders[0]
    assert o['platform'] == 'shopee'
    assert o['order_sn'] == '260530R46WR25G'
    assert o['status'] == 'ที่ต้องจัดส่ง'
    assert o['marketplace_fee'] == 2.0          # 1 + 0 + 1
    assert o['item_total'] == 9.0
    assert o['payout'] == 9.0
    assert o['ship_address'] == '99/9 อำเภอบ้านธิ จังหวัดลำพูน 51180'
    assert len(o['items']) == 1
    it = o['items'][0]
    assert it['qty'] == 1.0 and it['unit_price'] == 9.0 and it['item_subtotal'] == 9.0
    assert it['variation_id'] is None


def test_multi_line_order_fees_are_order_level():
    # Order with 2 tape variations; fee/total columns repeat per line.
    common = dict(**{_SP.ORDER: '260531TDW7VWU9', _SP.STATUS: 'ที่ต้องจัดส่ง',
                     _SP.COMMISSION: '125.00', _SP.TXN_FEE: '39.00', _SP.SVC_FEE: '105.00',
                     _SP.TOTAL: '1043.00'})
    df = _shopee_df([
        {**common, _SP.ITEM_NAME: 'เทปกาว INTER TAPE', _SP.VAR_NAME: '1 นิ้ว(1ม้วน)',
         _SP.SELL_PRICE: '16.00', _SP.QTY: '18', _SP.NET_SELL: '288.00'},
        {**common, _SP.ITEM_NAME: 'เทปกาว INTER TAPE', _SP.VAR_NAME: '2 นิ้ว(1ม้วน)',
         _SP.SELL_PRICE: '34.00', _SP.QTY: '9', _SP.NET_SELL: '306.00'},
    ])
    orders = parse_shopee_orders(df)
    assert len(orders) == 1
    o = orders[0]
    assert len(o['items']) == 2
    assert o['marketplace_fee'] == 269.0                 # 125 + 39 + 105, once
    assert o['item_total'] == 594.0                       # 288 + 306 (per-line sum)
    assert o['payout'] == 1043.0
    keys = [it['line_key'] for it in o['items']]
    assert keys == ['เทปกาว INTER TAPE|1 นิ้ว(1ม้วน)', 'เทปกาว INTER TAPE|2 นิ้ว(1ม้วน)']
    assert len(set(keys)) == 2


def test_two_orders_grouped_and_order_preserved():
    df = _shopee_df([
        {_SP.ORDER: 'A', _SP.ITEM_NAME: 'x', _SP.QTY: '1', _SP.NET_SELL: '10', _SP.TOTAL: '10'},
        {_SP.ORDER: 'B', _SP.ITEM_NAME: 'y', _SP.QTY: '2', _SP.NET_SELL: '20', _SP.TOTAL: '20'},
        {_SP.ORDER: 'A', _SP.ITEM_NAME: 'z', _SP.QTY: '3', _SP.NET_SELL: '30', _SP.TOTAL: '10'},
    ])
    orders = parse_shopee_orders(df)
    assert [o['order_sn'] for o in orders] == ['A', 'B']
    assert len(orders[0]['items']) == 2          # A has 2 lines
    assert orders[0]['item_total'] == 40.0       # 10 + 30


def test_line_key_dedups_identical_name_variation():
    df = _shopee_df([
        {_SP.ORDER: 'A', _SP.ITEM_NAME: 'same', _SP.VAR_NAME: 'red', _SP.QTY: '1', _SP.NET_SELL: '5'},
        {_SP.ORDER: 'A', _SP.ITEM_NAME: 'same', _SP.VAR_NAME: 'red', _SP.QTY: '1', _SP.NET_SELL: '5'},
    ])
    o = parse_shopee_orders(df)[0]
    keys = [it['line_key'] for it in o['items']]
    assert keys == ['same|red', 'same|red#2']    # deterministic, unique


def test_missing_required_columns_raises():
    df = pd.DataFrame([{'foo': 'bar'}])
    with pytest.raises(ValueError):
        parse_shopee_orders(df)


def test_number_and_string_helpers():
    assert _num('1,043.00') == 1043.0
    assert _num('') is None
    assert _num('abc') is None
    assert _s('  nan  ') == ''
    assert _s(None) == ''
    assert _s('  ok ') == 'ok'


# --- Lazada ---

def _lazada_df(rows):
    cols = [_LZ.ORDER, _LZ.ITEM_ID, _LZ.SELLER_SKU, _LZ.LAZADA_SKU, _LZ.ITEM_NAME,
            _LZ.VARIATION, _LZ.UNIT_PRICE, _LZ.PAID_PRICE, _LZ.STATUS, _LZ.CREATE,
            _LZ.SHIP_NAME, _LZ.CUST_NAME, _LZ.PHONE, _LZ.CITY, _LZ.POSTCODE,
            _LZ.REGION] + list(_LZ.ADDR)
    full = [{c: r.get(c, '') for c in cols} for r in rows]
    return pd.DataFrame(full, columns=cols)


def test_lazada_units_collapse_into_line_with_qty():
    # 3 units of the same product (3 orderItemIds) -> one line, qty 3.
    base = dict(**{_LZ.ORDER: '63171524', _LZ.SELLER_SKU: 'No.412',
                   _LZ.LAZADA_SKU: '2157778976_TH-7187066237',
                   _LZ.ITEM_NAME: 'บานพับสีทอง', _LZ.VARIATION: 'Item:No.412',
                   _LZ.UNIT_PRICE: '39.00', _LZ.STATUS: 'ready_to_ship',
                   _LZ.CREATE: '30 May 2026 17:18'})
    df = _lazada_df([
        {**base, _LZ.ITEM_ID: 'A1', _LZ.PAID_PRICE: '39.89'},
        {**base, _LZ.ITEM_ID: 'A2', _LZ.PAID_PRICE: '39.89'},
        {**base, _LZ.ITEM_ID: 'A3', _LZ.PAID_PRICE: '39.88'},
    ])
    orders = parse_lazada_orders(df)
    assert len(orders) == 1
    o = orders[0]
    assert o['platform'] == 'lazada'
    assert o['order_date'] == '2026-05-30 17:18'        # normalized to ISO
    assert o['marketplace_fee'] is None and o['payout'] is None
    assert len(o['items']) == 1
    it = o['items'][0]
    assert it['qty'] == 3.0
    assert it['item_subtotal'] == 119.66                # 39.89 + 39.89 + 39.88
    assert it['variation_id'] == '2157778976_TH-7187066237'   # resolves by this
    assert it['line_key'] == '2157778976_TH-7187066237'


def test_lazada_multi_product_order_separate_lines():
    df = _lazada_df([
        {_LZ.ORDER: 'X', _LZ.ITEM_ID: '1', _LZ.LAZADA_SKU: 'LZ-A', _LZ.ITEM_NAME: 'a',
         _LZ.UNIT_PRICE: '10', _LZ.PAID_PRICE: '10'},
        {_LZ.ORDER: 'X', _LZ.ITEM_ID: '2', _LZ.LAZADA_SKU: 'LZ-B', _LZ.ITEM_NAME: 'b',
         _LZ.UNIT_PRICE: '20', _LZ.PAID_PRICE: '20'},
        {_LZ.ORDER: 'X', _LZ.ITEM_ID: '3', _LZ.LAZADA_SKU: 'LZ-B', _LZ.ITEM_NAME: 'b',
         _LZ.UNIT_PRICE: '20', _LZ.PAID_PRICE: '20'},
    ])
    o = parse_lazada_orders(df)[0]
    assert [it['line_key'] for it in o['items']] == ['LZ-A', 'LZ-B']
    qty = {it['line_key']: it['qty'] for it in o['items']}
    assert qty == {'LZ-A': 1.0, 'LZ-B': 2.0}
    assert o['item_total'] == 50.0                      # 10 + 20 + 20


def test_lazada_line_key_falls_back_when_no_lazada_sku():
    df = _lazada_df([
        {_LZ.ORDER: 'X', _LZ.ITEM_ID: '1', _LZ.LAZADA_SKU: '', _LZ.SELLER_SKU: 'S1',
         _LZ.ITEM_NAME: 'a', _LZ.UNIT_PRICE: '10', _LZ.PAID_PRICE: '10'},
    ])
    o = parse_lazada_orders(df)[0]
    assert o['items'][0]['line_key'] == 'S1'            # falls back to seller_sku


def test_lazada_missing_required_columns_raises():
    with pytest.raises(ValueError):
        parse_lazada_orders(pd.DataFrame([{'foo': 'bar'}]))


# --- Importer (resolution + idempotent upsert) on a real DB copy ---

import database
import models


def _apply_mig_093(conn):
    path = os.path.join(database.MIGRATIONS_DIR, '093_marketplace_orders.sql')
    with open(path, encoding='utf-8') as f:
        conn.executescript(f.read())


def _order(items, order_sn='TEST-ORD-1'):
    return {
        'platform': 'lazada', 'order_sn': order_sn, 'status': 'ready_to_ship',
        'order_date': '2026-06-01 00:00', 'paid_date': None,
        'buyer_name': 'fake', 'buyer_phone': '0000000000', 'ship_address': 'addr',
        'item_total': sum(i['item_subtotal'] for i in items),
        'marketplace_fee': None, 'payout': None, 'currency': 'THB', 'items': items,
    }


def test_importer_resolves_and_flags_unmapped(tmp_db_conn):
    conn = tmp_db_conn
    _apply_mig_093(conn)
    row = conn.execute(
        "SELECT variation_id, internal_product_id FROM platform_skus "
        "WHERE platform='lazada' AND internal_product_id IS NOT NULL "
        "AND variation_id IS NOT NULL LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no mapped lazada platform_skus row in DB copy")
    real_vid, real_pid = row[0], row[1]

    order = _order([
        {'line_key': real_vid, 'seller_sku': None, 'variation_id': real_vid,
         'item_name': 'mapped', 'variation_name': None, 'qty': 2.0,
         'unit_price': 10.0, 'item_subtotal': 20.0},
        {'line_key': 'BOGUS', 'seller_sku': None, 'variation_id': 'NOPE-999-XYZ',
         'item_name': 'zzz-nonexistent-name', 'variation_name': None, 'qty': 1.0,
         'unit_price': 5.0, 'item_subtotal': 5.0},
    ])
    stats = models.import_marketplace_orders(conn, [order], 'test.xlsx')
    assert stats == {'orders': 1, 'items': 2, 'unmapped': 1, 'lines_resolved': 1}

    # header landed
    h = conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='TEST-ORD-1'").fetchone()
    assert h is not None
    # mapped line -> real pid; bogus line -> NULL
    mapped = conn.execute(
        "SELECT internal_product_id FROM marketplace_order_items "
        "WHERE order_sn='TEST-ORD-1' AND line_key=?", (real_vid,)).fetchone()[0]
    assert mapped == real_pid
    bogus = conn.execute(
        "SELECT internal_product_id FROM marketplace_order_items "
        "WHERE order_sn='TEST-ORD-1' AND line_key='BOGUS'").fetchone()[0]
    assert bogus is None


def test_importer_is_idempotent(tmp_db_conn):
    conn = tmp_db_conn
    _apply_mig_093(conn)
    order = _order([
        {'line_key': 'L1', 'seller_sku': None, 'variation_id': 'NOPE-1',
         'item_name': 'x', 'variation_name': None, 'qty': 1.0,
         'unit_price': 9.0, 'item_subtotal': 9.0},
    ])
    models.import_marketplace_orders(conn, [order], 'f1.xlsx')
    models.import_marketplace_orders(conn, [order], 'f1.xlsx')   # re-import
    n_orders = conn.execute(
        "SELECT COUNT(*) FROM marketplace_orders WHERE order_sn='TEST-ORD-1'").fetchone()[0]
    n_items = conn.execute(
        "SELECT COUNT(*) FROM marketplace_order_items WHERE order_sn='TEST-ORD-1'").fetchone()[0]
    assert n_orders == 1 and n_items == 1     # no duplication on re-import


# --- Route render tests (template + base.html nav BuildError + queries) ---

@pytest.fixture
def mp_client(tmp_db):
    """Admin test client on a live-DB clone with migration 093 applied."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    with open(os.path.join(database.MIGRATIONS_DIR, '093_marketplace_orders.sql'),
              encoding='utf-8') as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def test_marketplace_dashboard_renders(mp_client):
    resp = mp_client.get('/marketplace')
    assert resp.status_code == 200, resp.data[:600]
    assert 'Marketplace'.encode() in resp.data


def test_marketplace_unmapped_renders(mp_client):
    resp = mp_client.get('/marketplace/unmapped')
    assert resp.status_code == 200, resp.data[:600]


def test_payout_column_shows_actual_then_estimate(tmp_db, mp_client):
    """ยอดรับ column = real net (actual_payout) when settled, else item_total−fee
    with a ~ประมาณ badge. It must NEVER show the raw order-export `payout`
    (จำนวนเงินทั้งหมด, which includes buyer-paid shipping → over-states)."""
    import re
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    # Far-future dates so both rows land in the newest-500 window on the DB clone.
    conn.execute(
        """INSERT INTO marketplace_orders
             (platform, order_sn, status, order_date, item_total,
              marketplace_fee, payout, actual_payout)
           VALUES ('shopee','SETTLED1','สำเร็จแล้ว','2099-01-01 10:00',135,27,164,108)""")
    conn.execute(
        """INSERT INTO marketplace_orders
             (platform, order_sn, status, order_date, item_total,
              marketplace_fee, payout, actual_payout)
           VALUES ('shopee','PENDING1','ที่ต้องจัดส่ง','2099-01-02 10:00',55,14,999,NULL)""")
    conn.commit()
    conn.close()

    html = mp_client.get('/marketplace').get_data(as_text=True)
    rows = re.split(r'<tr', html)
    settled = next(r for r in rows if 'SETTLED1' in r)
    pending = next(r for r in rows if 'PENDING1' in r)
    # settled → shows real net 108.00, NOT the 164.00 buyer-total
    assert '108.00' in settled and '164.00' not in settled
    # unsettled → estimate 55−14 = 41.00 + badge, NOT the raw payout 999.00
    assert '41.00' in pending and '~ประมาณ' in pending and '999.00' not in pending


def test_staff_allowed_to_import_orders(tmp_db):
    """Staff may import marketplace orders (Put enabled it 2026-06-03).

    POST with no file flashes 'choose a file' and redirects to the marketplace
    dashboard (/marketplace). If the permission gate still blocked staff, the
    before_request middleware would instead redirect to the main dashboard (/).
    """
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = c.post('/marketplace/import', data={}, follow_redirects=False)
    loc = resp.headers.get('Location') or ''
    assert resp.status_code == 302
    assert loc.endswith('/marketplace'), (
        f"staff should reach the import route (→ /marketplace), got {loc!r}"
    )
