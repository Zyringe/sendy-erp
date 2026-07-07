"""/products show_inactive toggle (product-naming-audit Phase 5).

NOTE: the checkbox LABEL 'แสดงที่ปิดใช้งาน' contains the badge text
'ปิดใช้งาน', so `'ปิดใช้งาน' in html` would false-pass on the default view
(same class of trap as the PR #215 sidebar test). Assertions therefore anchor
on a real inactive product's name appearing/disappearing, not on the badge
string.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _first_page_inactive(tmp_db):
    """An inactive product that falls inside page 1 (id <= 50, ORDER BY id)."""
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id, product_name FROM products WHERE is_active=0 AND id<=50"
        " ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip('no inactive product within page 1 in this DB copy')
    return row


def test_get_products_excludes_inactive_by_default(tmp_db):
    import models
    rows, total = models.get_products(page=1, per_page=10_000)
    assert all(r['is_active'] == 1 for r in rows)
    rows_all, total_all = models.get_products(page=1, per_page=10_000,
                                              include_inactive=True)
    assert total_all > total
    assert any(r['is_active'] == 0 for r in rows_all)


def test_route_toggle_shows_and_hides_inactive(admin_client, tmp_db):
    pid, name = _first_page_inactive(tmp_db)
    off = admin_client.get('/products').get_data(as_text=True)
    on = admin_client.get('/products?show_inactive=1').get_data(as_text=True)
    assert name not in off
    assert name in on
    # badge attached to the row (title attr is unique to the row badge, not the label)
    assert 'สินค้าถูกปิดใช้งาน' in on and 'สินค้าถูกปิดใช้งาน' not in off
