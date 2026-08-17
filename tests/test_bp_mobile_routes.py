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


def test_sales_trip_outstanding_ignores_cancelled_receipts(tmp_db):
    """/m/sales-trip shows a customer-facing amount owed. Its old
    `LEFT JOIN paid_invoices ... IS NULL` had no received_payments join at all,
    so a cancelled receipt erased real debt from it."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM customers WHERE code='C-MOB'")
    conn.execute("DELETE FROM regions WHERE code='ZMOB'")
    rid = conn.execute("INSERT INTO regions (code, name_th) VALUES ('ZMOB','เขตทดสอบมือถือ')").lastrowid
    # Region-scoped: the page LIMITs to 300 customers and the live-DB clone has
    # thousands, so an unscoped request would not render this row at all.
    conn.execute("INSERT INTO customers (code, name, region_id) VALUES ('C-MOB','ร้านมือถือทดสอบ',?)",
                 (rid,))
    conn.execute("""INSERT INTO sales_transactions
                      (date_iso, doc_no, doc_base, customer, customer_code,
                       qty, unit, unit_price, vat_type, total, net)
                    VALUES ('2026-07-01','IV-MOB-1','IV-MOB','ร้านมือถือทดสอบ','C-MOB',
                            1,'ตัว',900.0,1,900.0,900.0)""")
    cur = conn.execute("""INSERT INTO received_payments
                            (re_no, date_iso, customer, salesperson, cancelled, total)
                          VALUES ('RE-MOB-CANCELLED','2026-07-05','ร้านมือถือทดสอบ','S1',1,900.0)""")
    conn.execute("INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?,?,?,?)",
                 (cur.lastrowid, 'IV-MOB', 'IV', 900.0))
    conn.commit()
    conn.close()

    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1; sess['username'] = 'admin'; sess['role'] = 'admin'
    body = c.get(f'/m/sales-trip?region_id={rid}').get_data(as_text=True)

    assert 'ร้านมือถือทดสอบ' in body, 'control — the seeded customer is on the page'
    assert '900' in body, 'a cancelled receipt erased a real debt from /m/sales-trip'
