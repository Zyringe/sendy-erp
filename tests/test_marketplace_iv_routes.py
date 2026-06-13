"""Route tests for the settlement IV column, order-detail JSON, and link-iv confirm.

These exercise the REAL authed request path (the settlement page render, the two
JSON APIs, and the manual-link POST) against a tmp clone of the live DB — the
class of failure pytest-on-a-fresh-app + a worktree scan cannot catch.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def seeded(tmp_db_conn):
    """One settled shopee order (with a line item + a matching Zหน้าร้าน IV) on a
    clean settlement slate. Returns (conn, order_id)."""
    c = tmp_db_conn
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL, settlement_source=NULL")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='IVROUTE1'")
    c.execute("DELETE FROM sales_transactions WHERE doc_base='IV9100001'")
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, item_total, marketplace_fee, actual_payout, order_date, settled_at, buyer_name, currency)
           VALUES ('shopee','IVROUTE1','สำเร็จแล้ว', 150.0, 18.0, 132.0, '2026-06-08 10:00', '2026-06-10', 'N******x', 'THB')""")
    oid = cur.lastrowid
    c.execute(
        """INSERT INTO marketplace_order_items
           (order_id, platform, order_sn, line_key, seller_sku, item_name, qty, unit_price, item_subtotal)
           VALUES (?, 'shopee','IVROUTE1','L1','SKU-1','สินค้าทดสอบ', 2, 75.0, 150.0)""", (oid,))
    # an Express IV booked at the net payout (132) under Zหน้าร้าน, day after the order
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price, vat_type, total, net, created_at, synced_to_stock)
           VALUES ('2026-06-09','IV9100001-1','IV9100001','หน้าร้านS','Zหน้าร้าน', 1, 132.0, 1, 132.0, 132.0, '2026-06-09 00:00:00', 1)""")
    c.commit()
    return c, oid


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def test_settlement_page_renders_iv_column(seeded):
    c = _client()
    resp = c.get('/marketplace/settlement')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ใบกำกับ (IV)' in html        # the new column header
    assert 'เลขออเดอร์' in html           # renamed from "Order ID"
    assert 'Order ID' not in html         # old label gone


def test_api_order_detail_json(seeded):
    _conn, oid = seeded
    c = _client()
    resp = c.get(f'/marketplace/api/order/{oid}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['order']['order_sn'] == 'IVROUTE1'
    assert data['order']['actual_payout'] == 132.0
    assert len(data['items']) == 1
    assert data['items'][0]['item_name'] == 'สินค้าทดสอบ'


def test_api_order_detail_404(seeded):
    c = _client()
    assert c.get('/marketplace/api/order/99999999').status_code == 404


def test_api_iv_candidates_finds_match(seeded):
    _conn, oid = seeded
    c = _client()
    resp = c.get(f'/marketplace/api/order/{oid}/iv-candidates')
    assert resp.status_code == 200
    data = resp.get_json()
    docs = [x['doc_base'] for x in data['candidates']]
    assert 'IV9100001' in docs


def test_link_iv_persists_manual(seeded):
    conn, oid = seeded
    c = _client()
    resp = c.post(f'/marketplace/order/{oid}/link-iv', data={'doc_base': 'IV9100001'})
    assert resp.status_code == 302
    row = conn.execute(
        "SELECT doc_base, match_method, confirmed_by FROM marketplace_order_invoice WHERE order_sn='IVROUTE1'"
    ).fetchone()
    assert row['doc_base'] == 'IV9100001'
    assert row['match_method'] == 'manual'
    assert row['confirmed_by'] == 'staffer'


def test_link_iv_manual_text_overrides(seeded):
    conn, oid = seeded
    c = _client()
    # both fields present: the free-text override wins
    resp = c.post(f'/marketplace/order/{oid}/link-iv',
                  data={'doc_base': 'IV9100001', 'doc_base_manual': 'IV6900999'})
    assert resp.status_code == 302
    row = conn.execute(
        "SELECT doc_base FROM marketplace_order_invoice WHERE order_sn='IVROUTE1'"
    ).fetchone()
    assert row['doc_base'] == 'IV6900999'


def _link_and_pay(conn):
    """Link IVROUTE1→IV9100001 and record รับชำระ 132 for that IV."""
    conn.execute(
        """INSERT INTO marketplace_order_invoice
           (platform, order_sn, doc_base, customer_code, match_method, confidence)
           VALUES ('shopee','IVROUTE1','IV9100001','Zหน้าร้าน','manual','manual')""")
    cur = conn.execute(
        """INSERT INTO received_payments (re_no, date_iso, customer, cancelled, total)
           VALUES ('RE9100001','2026-06-12','หน้าร้านS', 0, 132.0)""")
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)",
        (cur.lastrowid, 'IV9100001', 'IV', 132.0))
    conn.commit()


def test_reconciliation_three_amounts_line_up(seeded):
    """payout (132) == matched IV billed (132) == รับชำระ collected (132) → ok=True."""
    conn, _oid = seeded
    _link_and_pay(conn)
    import models
    rep = models.get_marketplace_reconciliation(conn, 'shopee')
    mine = next(o for m in rep['months'] for o in m['orders'] if o['order_sn'] == 'IVROUTE1')
    assert mine['payout'] == 132.0
    assert mine['billed'] == 132.0
    assert mine['collected'] == 132.0
    assert mine['ok'] is True


def test_reconciliation_page_renders(seeded):
    conn, _oid = seeded
    _link_and_pay(conn)
    c = _client()
    resp = c.get('/marketplace/reconciliation')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'IVROUTE1' in html
    assert 'รับชำระ' in html


def test_amount_review_accept_and_clear(tmp_db_conn):
    """A manager acknowledgement flips a billed≠payout row to reviewed (and back)."""
    import marketplace_match as mm
    import models
    c = tmp_db_conn
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM marketplace_amount_review")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='REV1'")
    c.execute("DELETE FROM sales_transactions WHERE doc_base='IV9200001'")
    cur = c.execute(
        """INSERT INTO marketplace_orders (platform, order_sn, status, actual_payout, order_date, settled_at, currency)
           VALUES ('shopee','REV1','สำเร็จแล้ว', 100.0, '2026-06-07 10:00', '2026-06-09', 'THB')""")
    oid = cur.lastrowid
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price, vat_type, total, net, created_at, synced_to_stock)
           VALUES ('2026-06-08','IV9200001-1','IV9200001','หน้าร้านS','Zหน้าร้าน', 1, 110.0, 1, 110.0, 110.0, '2026-06-08 00:00:00', 1)""")
    c.commit()
    mm.link_manual(c, 'shopee', 'REV1', 'IV9200001')   # billed 110 vs payout 100 → +10 mismatch

    def _mine():
        rep = models.get_marketplace_reconciliation(c, 'shopee')
        return next(o for m in rep['months'] for o in m['orders'] if o['order_sn'] == 'REV1'), rep['summary']

    mine, summ = _mine()
    assert mine['amount_mismatch'] is True and mine['reviewed'] is False
    assert summ['amount_mismatch_open'] >= 1

    models.set_amount_review(c, oid, True, reviewed_by='boss')
    mine, summ = _mine()
    assert mine['reviewed'] is True and mine['reviewed_by'] == 'boss'

    models.set_amount_review(c, oid, False)
    mine, _ = _mine()
    assert mine['reviewed'] is False


def test_review_amount_manager_only():
    """Staff is bounced to the dashboard; manager reaches the route."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True

    cs = flask_app.test_client()
    with cs.session_transaction() as s:
        s.update(user_id=4, username='st', role='staff')
    r = cs.post('/marketplace/order/1/review-amount', data={}, follow_redirects=False)
    loc = r.headers.get('Location') or ''
    assert r.status_code == 302 and 'reconciliation' not in loc   # blocked before the route

    cm = flask_app.test_client()
    with cm.session_transaction() as s:
        s.update(user_id=2, username='mgr', role='manager')
    r = cm.post('/marketplace/order/99999999/review-amount', data={}, follow_redirects=False)
    assert r.status_code == 302 and 'reconciliation' in (r.headers.get('Location') or '')


def test_settlement_import_triggers_automatch(tmp_db_conn, tmp_path):
    """End-to-end: importing an Income file settles the order AND auto-links its IV.

    Uses an unusual amount (7,777.77) so the auto-match is unambiguous against the
    cloned live data."""
    import pandas as pd
    c = tmp_db_conn
    c.execute("UPDATE marketplace_orders SET actual_payout=NULL, settled_at=NULL")
    c.execute("DELETE FROM marketplace_order_invoice")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='IVE2E1'")
    c.execute("DELETE FROM sales_transactions WHERE doc_base='IV9100777'")
    c.execute(
        """INSERT INTO marketplace_orders (platform, order_sn, status, item_total, order_date, currency)
           VALUES ('shopee','IVE2E1','สำเร็จแล้ว', 9000.0, '2026-06-07 10:00', 'THB')""")  # unsettled
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code, qty, unit_price, vat_type, total, net, created_at, synced_to_stock)
           VALUES ('2026-06-08','IV9100777-1','IV9100777','หน้าร้านS','Zหน้าร้าน', 1, 7777.77, 1, 7777.77, 7777.77, '2026-06-08 00:00:00', 1)""")
    c.commit()

    # Minimal Income xlsx (only the 3 parsed columns matter), banner above header.
    header = ['หมายเลขคำสั่งซื้อ', 'วันที่โอนชำระเงินสำเร็จ', 'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)']
    rows = [['รายงานรายรับ', None, None]] + [[None, None, None]] * 4 + [header] + [['IVE2E1', '2026-06-08', 7777.77]]
    xlsx = tmp_path / 'Income.test.xlsx'
    pd.DataFrame(rows).to_excel(str(xlsx), sheet_name='Income', header=False, index=False)

    client = _client()
    with open(xlsx, 'rb') as fh:
        resp = client.post('/marketplace/settlement-import',
                           data={'settlement_file': (fh, 'Income.test.xlsx')},
                           content_type='multipart/form-data')
    assert resp.status_code == 302

    settled = c.execute(
        "SELECT settled_at, actual_payout FROM marketplace_orders WHERE order_sn='IVE2E1'").fetchone()
    assert settled['settled_at'] == '2026-06-08'
    assert settled['actual_payout'] == 7777.77
    link = c.execute(
        "SELECT doc_base, match_method FROM marketplace_order_invoice WHERE order_sn='IVE2E1'").fetchone()
    assert link is not None and link['doc_base'] == 'IV9100777' and link['match_method'] == 'auto'
