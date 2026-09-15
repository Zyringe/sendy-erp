"""#545 — a manual IV pick is checked on the server before anything is written.

Seam: the real authed POST /marketplace/order/<id>/link-iv (and the confirm POST
it renders), observed through the response (confirm page / redirect / flash) and
the `marketplace_order_invoice` + `audit_log` rows it leaves behind.

Every test seeds its own orders and IVs on the tmp clone of the live DB and
deletes its keys first: the clone carries real rows, so no state is inherited.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

TEST_DOCS = ('IV9500001', 'IV9500002', 'IV9500003', 'HS9500004', 'SR9500005',
             'IV9500006', 'IV9500007', 'IV9500008')
TEST_ORDERS = ('PICKS1', 'PICKS2', 'PICKS3', 'PICKL1')


def _order(c, platform, order_sn, payout, order_date='2026-06-08'):
    return c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, actual_payout, settled_at, order_date, currency)
           VALUES (?,?, 'สำเร็จแล้ว', ?, ?, ?, 'THB')""",
        (platform, order_sn, payout, order_date, order_date + ' 10:00')).lastrowid


def _doc(c, doc_base, net, customer_code, date_iso='2026-06-09'):
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price,
            vat_type, total, net, created_at, synced_to_stock)
           VALUES (?,?,?,?,?, 1, ?, 1, ?, ?, '2026-06-09 00:00:00', 1)""",
        (date_iso, f'{doc_base}-1', doc_base, 'ทดสอบ', customer_code, net, net, net))


def _link(c, platform, order_sn, doc_base, method='auto', confirmed_by=None):
    c.execute(
        """INSERT INTO marketplace_order_invoice
           (platform, order_sn, doc_base, customer_code, match_method, confidence, confirmed_by)
           VALUES (?,?,?,?,?,?,?)""",
        (platform, order_sn, doc_base, 'Zหน้าร้าน', method,
         'manual' if method == 'manual' else 'confident', confirmed_by))


@pytest.fixture
def pick(tmp_db_conn):
    """Shopee orders PICKS1..3 + Lazada PICKL1, and one Express doc per case."""
    c = tmp_db_conn
    marks = ','.join('?' * len(TEST_DOCS))
    c.execute(f"DELETE FROM sales_transactions WHERE doc_base IN ({marks})", TEST_DOCS)
    c.execute(f"DELETE FROM marketplace_order_invoice WHERE doc_base IN ({marks})", TEST_DOCS)
    omarks = ','.join('?' * len(TEST_ORDERS))
    c.execute(f"DELETE FROM marketplace_order_invoice WHERE order_sn IN ({omarks})", TEST_ORDERS)
    c.execute(f"DELETE FROM marketplace_orders WHERE order_sn IN ({omarks})", TEST_ORDERS)
    ids = {
        'S1': _order(c, 'shopee', 'PICKS1', 132.0),
        'S2': _order(c, 'shopee', 'PICKS2', 140.0),
        'S3': _order(c, 'shopee', 'PICKS3', 150.0),
        'L1': _order(c, 'lazada', 'PICKL1', 200.0),
    }
    _doc(c, 'IV9500001', 132.0, 'Zหน้าร้าน')   # free, Shopee's own code
    _doc(c, 'IV9500002', 140.0, 'Zหน้าร้าน')   # held by PICKS2 (auto)
    _doc(c, 'IV9500003', 200.0, 'Lหน้าร้าน')   # free, Lazada's own code
    _doc(c, 'HS9500004', 132.0, 'Zหน้าร้าน')   # a cash bill, not an invoice
    _doc(c, 'SR9500005', -50.0, 'Zหน้าร้าน')   # a credit note
    _doc(c, 'IV9500006', 132.0, 'Gหน้าร้าน')   # walk-in counter, not a marketplace
    _doc(c, 'IV9500007', 132.0, 'PICKB2B')     # a B2B customer
    _doc(c, 'IV9500008', 132.0, 'Bหน้าร้าน')   # old Shopee shop B
    _link(c, 'shopee', 'PICKS2', 'IV9500002')
    c.commit()
    return c, ids


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    cl = flask_app.test_client()
    with cl.session_transaction() as s:
        s.update(user_id=4, username='staffer', role='staff')
    return cl


def _flashes(cl):
    with cl.session_transaction() as s:
        return [m for _cat, m in s.get('_flashes', [])]


def _links(c, doc_base):
    return [dict(r) for r in c.execute(
        "SELECT platform, order_sn, match_method FROM marketplace_order_invoice WHERE doc_base=?",
        (doc_base,))]


def _order_link(c, order_sn):
    r = c.execute("SELECT doc_base FROM marketplace_order_invoice WHERE order_sn=?",
                  (order_sn,)).fetchone()
    return r['doc_base'] if r else None


def test_typed_number_not_in_sendy_is_refused(pick):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': 'IV9599999'})
    assert resp.status_code == 302
    assert _flashes(cl) == ['ไม่พบ IV9599999 ในระบบ ตรวจเลขอีกครั้ง (ถ้าเพิ่งคีย์ใน Express รอข้อมูลรอบถัดไป)']
    assert _order_link(c, 'PICKS1') is None


@pytest.mark.parametrize('doc, message', [
    ('HS9500004', 'HS9500004 เป็นบิลเงินสด ไม่ใช่ใบกำกับ'),
    ('SR9500005', 'SR9500005 เป็นใบลดหนี้ ไม่ใช่ใบกำกับ'),
])
def test_a_doc_that_is_not_an_iv_is_refused_naming_its_kind(pick, doc, message):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': doc})
    assert resp.status_code == 302
    assert _flashes(cl) == [message]
    assert _order_link(c, 'PICKS1') is None


@pytest.mark.parametrize('doc, code', [
    ('IV9500006', 'Gหน้าร้าน'),   # walk-in counter: its name starts หน้าร้าน but it is not online
    ('IV9500007', 'PICKB2B'),
])
def test_an_iv_billed_to_a_non_marketplace_customer_is_refused(pick, doc, code):
    c, ids = pick
    cl = _client()
    resp = cl.post(f"/marketplace/order/{ids['S1']}/link-iv", data={'doc_base_manual': doc})
    assert resp.status_code == 302
    assert _flashes(cl) == [f'{doc} เป็นบิลของ {code} ไม่ใช่บิลขายออนไลน์ ผูกกับออเดอร์ไม่ได้']
    assert _order_link(c, 'PICKS1') is None
