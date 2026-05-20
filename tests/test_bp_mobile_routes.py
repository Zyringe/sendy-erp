"""Route-level integration tests for bp_mobile.

Mobile-first /m/* routes — these are NOT gated by the staff/manager/admin
before_request middleware (any logged-in role can hit them), so admin
session is used here only to match the pattern of the other route-test
files. The same routes would render for staff too.

Uses tmp_db so route + models + templates execute against a live-DB
clone and never touch the real DB. Covers 3 of the 4 mobile endpoints:
stock search page, stock search JSON API, and the region-grouped sales-
trip view. customer_detail (path-arg with Thai name) is left out — the
3-endpoint target is met and the URL-encoding is brittle for a smoke
test.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated. tmp_db
    must be pulled in first so config.DATABASE_PATH is monkeypatched
    before `from app import app` runs."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def test_mobile_stock_search_renders(admin_client):
    """Thumb-friendly product search landing page (results come from
    /m/stock/api as the user types)."""
    resp = admin_client.get('/m/stock')
    assert resp.status_code == 200, resp.data[:500]


def test_mobile_stock_search_api_returns_json(admin_client):
    """Live-search JSON endpoint. Hits products + stock_levels +
    product_barcodes — broadest single-query in the blueprint."""
    resp = admin_client.get('/m/stock/api?q=ใบตัด')
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    body = resp.get_json()
    assert 'items' in body
    assert isinstance(body['items'], list)


def test_mobile_sales_trip_renders(admin_client):
    """Region-grouped customer list for field-trip planning. Exercises
    the customers + salespersons + regions + sales_transactions JOIN."""
    resp = admin_client.get('/m/sales-trip')
    assert resp.status_code == 200, resp.data[:500]
