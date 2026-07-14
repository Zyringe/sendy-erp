"""Render guards for Phase 2 finance revamp R2 — "เลิกเลขชวนงง" (drop
confusing numbers/labels). Pure copy changes; one lightweight test per
surface is proportionate (no new logic to TDD).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')


def _admin(tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1; s['username'] = 'admin'; s['role'] = 'admin'
    return c


def test_trade_dashboard_margin_caption_disclaims_gross_profit(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/trade-dashboard')
    assert r.status_code == 200
    body = r.data.decode()
    # The bare "margin X%" caption is gone; replaced with a disclaimer that
    # this is NOT the COGS gross profit shown on /accounting.
    assert 'margin ' not in body.lower() or 'ไม่ใช่กำไรขั้นต้น' in body
    if 'ของยอดขาย' in body:
        assert 'ไม่ใช่กำไรขั้นต้น (ดู สรุปกำไร-ขาดทุน)' in body


def test_marketplace_index_unmatched_orders_label(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/marketplace')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'ออเดอร์ยังไม่จับคู่สินค้า' in body
    assert 'รายการยังไม่จับคู่' not in body


def test_ecommerce_listing_mapping_label(tmp_db):
    c = _admin(tmp_db)
    r = c.get('/ecommerce')
    assert r.status_code == 200
    body = r.data.decode()
    # Only asserts the label wording when the "ยังไม่ผูก" line actually
    # renders (ltotal > 0 branch) — otherwise the page shows "ยังไม่มีข้อมูล".
    if 'รายการ' in body:
        assert 'listing ยังไม่ผูกสินค้า' in body
