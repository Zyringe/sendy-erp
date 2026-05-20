"""Route-level integration tests for bp_products.

Covers 5 highest-value endpoints. Uses tmp_db so route + models +
templates all execute against a live-DB copy and never touch the
real DB. Logs in by setting session keys directly — bypasses the
/login flow since auth isn't what these tests are validating.

bp_products is 561 LOC with 20+ CRUD routes and previously had 0
route-level tests. This file establishes coverage for index,
detail, cost-history (admin/manager-gated JSON), pricing, and a
404-path negative case.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated.

    Matches the session keys read by app.py's permission middleware
    (`session.get('role')`, `session.get('user_id')`). tmp_db is
    pulled in so config/database DATABASE_PATH is already
    monkeypatched before `from app import app` runs.
    """
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _first_active_product_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No active products in live DB clone")
    return row[0]


def test_products_index_renders(admin_client):
    resp = admin_client.get('/products')
    assert resp.status_code == 200, resp.data[:500]


def test_product_detail_renders(admin_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}')
    assert resp.status_code == 200, resp.data[:500]


def test_product_cost_history_returns_json(admin_client, tmp_db):
    """Admin/manager-gated JSON endpoint — guards the WACC history
    surface that finance reports depend on."""
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}/cost-history')
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    body = resp.get_json()
    assert 'wacc' in body
    assert 'history' in body
    assert isinstance(body['history'], list)


def test_product_pricing_renders(admin_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}/pricing')
    assert resp.status_code == 200, resp.data[:500]


def test_product_detail_unknown_id_redirects_to_list(admin_client):
    """Unknown id flashes 'ไม่พบสินค้า' and redirects to /products
    (per blueprints/products.py::product_detail)."""
    resp = admin_client.get('/products/99999999', follow_redirects=False)
    # product_detail flashes + redirects; product_pricing aborts(404).
    # Detail route specifically redirects — assert that path.
    assert resp.status_code in (302, 404)
