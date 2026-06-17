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
    # /marketplace/reconciliation now redirects into the settlement reconcile tab.
    resp = c.get('/marketplace/reconciliation', follow_redirects=True)
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
    assert r.status_code == 302 and 'tab=reconcile' not in loc   # blocked before the route

    cm = flask_app.test_client()
    with cm.session_transaction() as s:
        s.update(user_id=2, username='mgr', role='manager')
    r = cm.post('/marketplace/order/99999999/review-amount', data={}, follow_redirects=False)
    # review_amount now redirects into the settlement reconcile tab.
    assert r.status_code == 302 and 'tab=reconcile' in (r.headers.get('Location') or '')


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


def test_deposits_tab_wires_drilldown_and_estimate_badge(tmp_db_conn):
    """Deposits tab: each order_sn is a drill-down trigger (data-order-id), and a
    wallet-sourced order (Order+Balance only, no Income) shows the '~ ประมาณ'
    estimate badge. Exercises the real authed render → catches Jinja/query bugs
    that the model-level test can't (e.g. a template that drops the new fields)."""
    import models, marketplace_reconcile
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_wallet_txns WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_payouts WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_order_fees WHERE platform='shopee'")
    c.execute("UPDATE marketplace_orders SET payout_id=NULL WHERE platform='shopee'")
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='DEPWALLET1'")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn, item_total) VALUES ('shopee','DEPWALLET1', 100.0)")
    models.import_wallet_txns(c, [
      {'txn_time':'2026-06-02 10:00','txn_type':'income','order_sn':'DEPWALLET1','amount':82.0,'running_balance':82.0,'description':''},
      {'txn_time':'2026-06-09 01:19','txn_type':'withdrawal','order_sn':None,'amount':-82.0,'running_balance':0.0,'description':'w'}], 'b.xlsx')
    marketplace_reconcile.reconcile_payouts(c, 'shopee')
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='DEPWALLET1'").fetchone()['id']
    pid = models.get_payout_summaries(c, 'shopee')[0]['id']
    cl = _client()
    resp = cl.get('/marketplace/settlement?platform=shopee&tab=deposits&year=all')
    assert resp.status_code == 200
    # Rows are lazy-loaded: the page carries the deposit card, the API the rows
    # (with id + fee_source that the JS turns into the drill-down hook + badge).
    assert f'data-payout-id="{pid}"' in resp.get_data(as_text=True)
    api = cl.get(f'/marketplace/api/payout/{pid}/orders?platform=shopee').get_json()
    o = next(x for x in api['orders'] if x['order_sn'] == 'DEPWALLET1')
    assert o['id'] == oid and o['fee_source'] == 'wallet' and o['net_payout'] == 82.0


def test_api_order_detail_returns_adjustments(tmp_db_conn):
    """The order-detail JSON carries refund/adjustment rows so the modal can show
    'การปรับปรุง / คืนเงิน'."""
    import models
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='APIADJ1'")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','APIADJ1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='APIADJ1'").fetchone()['id']
    models.import_wallet_txns(c, [
      {'txn_time':'2026-04-05 23:47','txn_type':'adjustment','order_sn':'APIADJ1','amount':-500.0,'running_balance':0.0,'description':'คืนเงิน'}], 'b.xlsx')
    resp = _client().get(f'/marketplace/api/order/{oid}')
    assert resp.status_code == 200
    assert resp.get_json()['adjustments'] == [
      {'txn_time':'2026-04-05 23:47','amount':-500.0,'description':'คืนเงิน'}]
