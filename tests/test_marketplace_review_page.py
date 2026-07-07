"""Route + nav tests for /marketplace/review (build-phase 1 of
marketplace-iv-matching). The classifier itself is covered by
test_marketplace_iv_worklist.py; this file exercises the REAL authed request
path (render, badge on settlement, both nav surfaces) — the class of failure
pytest-on-a-fresh-app + a worktree scan cannot catch (see
erp-engineering-discipline.md).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def seeded(tmp_db_conn):
    """One bucket-D order (settled, mapped, no IV linked) — enough to exercise
    the review page's render + the settlement badge without depending on
    whatever bucket rows happen to already exist in the cloned live data."""
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn = 'REVIEWPAGE1'")
    px = c.execute("INSERT INTO products (product_name) VALUES ('Review Page Test Product')").lastrowid
    cur = c.execute(
        """INSERT INTO marketplace_orders
           (platform, order_sn, status, order_date, actual_payout, settled_at, currency)
           VALUES ('shopee','REVIEWPAGE1','สำเร็จแล้ว', '2026-05-10 10:00', 42.0, '2026-05-11', 'THB')""")
    oid = cur.lastrowid
    c.execute(
        """INSERT INTO marketplace_order_items
           (order_id, platform, order_sn, line_key, item_name, qty, internal_product_id)
           VALUES (?, 'shopee', 'REVIEWPAGE1', 'L1', 'สินค้าทดสอบหน้ารีวิว', 1, ?)""",
        (oid, px))
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


def test_review_page_renders_and_shows_bucket_d_row(seeded):
    _conn, _oid = seeded
    resp = _client().get('/marketplace/review?platform=shopee')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ต้องตรวจการจับคู่ใบกำกับ' in html
    assert 'REVIEWPAGE1' in html
    assert 'ไม่มีใบกำกับว่างให้จับคู่' in html


def test_review_page_lazada_platform_switch(seeded):
    resp = _client().get('/marketplace/review?platform=lazada')
    assert resp.status_code == 200


def test_review_page_rejects_bad_platform_param(seeded):
    """An unknown ?platform value falls back to shopee, doesn't 500."""
    resp = _client().get('/marketplace/review?platform=bogus')
    assert resp.status_code == 200


def test_settlement_page_shows_worklist_badge(seeded):
    resp = _client().get('/marketplace/settlement?platform=shopee')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ต้องตรวจการจับคู่ใบกำกับ' in html
    assert '/marketplace/review' in html


def test_review_nav_present_desktop_and_mobile(tmp_db):
    """Regression for the missing left-nav tab (erp-engineering-discipline):
    the review page must resolve to the 'accounting' module (else the whole
    E-commerce/accounting sidebar block disappears on that page), AND both the
    desktop sidebar and mobile drawer must link to it. Needs tmp_db (a real
    cloned DB) — never render a route against the untouched default path."""
    from access_control import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('marketplace.review') == 'accounting'

    html = _client().get('/marketplace/settlement').get_data(as_text=True)
    assert html.count('href="/marketplace/review"') >= 2, \
        "review link missing from a nav (desktop sidebar or mobile drawer)"
