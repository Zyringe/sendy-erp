"""Route-level integration tests for bp_mobile.

Mobile-first /m/* routes — these are NOT gated by the staff/manager/admin
before_request middleware (any logged-in role can hit them), so admin
session is used here only to match the pattern of the other route-test
files. The same routes would render for staff too.

Uses tmp_db so route + models + templates execute against a live-DB
clone and never touch the real DB. Covers 3 of the 4 mobile endpoints:
stock search page, stock search JSON API, and the region-grouped sales-
trip view. customer_detail is covered by its issue-specific route tests.
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
    # #528: grouped/filtered by ภาค (customer_geo.region_of), not region_id —
    # an address that parses to ภาคตะวันออก scopes the request to a small,
    # deterministic group instead of relying on a row-count cap.
    conn.execute(
        "INSERT INTO customers (code, name, address) VALUES ('C-MOB','ร้านมือถือทดสอบ','123 ถ.สุขุมวิท ชลบุรี')")
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
    from urllib.parse import quote
    body = c.get(f"/m/sales-trip?region={quote('ภาคตะวันออก')}").get_data(as_text=True)

    assert 'ร้านมือถือทดสอบ' in body, 'control — the seeded customer is on the page'
    assert '900' in body, 'a cancelled receipt erased a real debt from /m/sales-trip'


def test_sales_trip_outstanding_still_excludes_hs_cash_sale(tmp_db):
    """#514: HS is a cash sale, paid on the spot — this AR-facing 'outstanding'
    figure keeps excluding it on purpose (money is revenue, not receivable).
    Control: a real unpaid IV for a second customer still shows its debt, so
    the assertion below proves the HS exclusion, not a broken query."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM customers WHERE code IN ('C-MOB-HS514','C-MOB-IV514')")
    conn.execute(
        "INSERT INTO customers (code, name, address) VALUES "
        "('C-MOB-HS514','ร้านทดสอบเงินสด514','123 ถ.สุขุมวิท ชลบุรี'),"
        "('C-MOB-IV514','ร้านทดสอบค้างชำระ514','456 ถ.สุขุมวิท ชลบุรี')")
    conn.execute("""INSERT INTO sales_transactions
                      (date_iso, doc_no, doc_base, customer, customer_code,
                       qty, unit, unit_price, vat_type, total, net)
                    VALUES ('2026-07-01','HS-MOB514-1','HS-MOB514','ร้านทดสอบเงินสด514','C-MOB-HS514',
                            1,'ตัว',7777.0,1,7777.0,7777.0)""")
    conn.execute("""INSERT INTO sales_transactions
                      (date_iso, doc_no, doc_base, customer, customer_code,
                       qty, unit, unit_price, vat_type, total, net)
                    VALUES ('2026-07-01','IV-MOB514-1','IV-MOB514','ร้านทดสอบค้างชำระ514','C-MOB-IV514',
                            1,'ตัว',5555.0,1,5555.0,5555.0)""")
    conn.commit()
    conn.close()

    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1; sess['username'] = 'admin'; sess['role'] = 'admin'
    from urllib.parse import quote
    body = c.get(f"/m/sales-trip?region={quote('ภาคตะวันออก')}").get_data(as_text=True)

    assert 'ร้านทดสอบเงินสด514' in body, 'control — the HS customer is on the page'
    assert 'ร้านทดสอบค้างชำระ514' in body, 'control — the IV customer is on the page'
    assert '7,777' not in body, 'HS cash sale must never render as outstanding debt'
    assert '5,555' in body, 'a real unpaid IV must still render as outstanding debt'


def _trip_due(html, customer_name):
    """The ฿ figure rendered on ONE customer's /m/sales-trip row, as a float.

    Scoped to that customer's own `<a href="/m/customer/code/...">` block on
    purpose: `trip-cust-due` and any given amount also appear elsewhere on the
    page (every other customer's row, the group banner), so a page-wide
    substring test answers a different question. Parses the number out rather
    than matching the formatted string — the template renders '{:,.0f}'.

    Returns None when the row rendered with no figure at all, and ASSERTS that
    the row is on the page: an absent row would make a "no figure" assertion
    vacuously true.
    """
    import re
    blocks = [b for b in html.split('<a href="/m/customer/code/')
              if 'trip-cust-name">{}<'.format(customer_name) in b]
    assert len(blocks) == 1, (
        'control failed — expected exactly 1 /m/sales-trip row for {!r}, found {}. '
        'Without the row on the page every assertion about its figure is vacuous.'
        .format(customer_name, len(blocks)))
    m = re.search(r'trip-cust-due">฿([\d,]+)', blocks[0])
    return float(m.group(1).replace(',', '')) if m else None


def test_sales_trip_outstanding_excludes_written_off_bill(tmp_db):
    """#568: /m/sales-trip's outstanding is what a rep is told the shop owes
    before walking in, so it must drop documents the accountant wrote off —
    the WHOLE ar_writeoffs table, not sales_filters' revenue-only
    `excludes_revenue = 1` subset (ADR 0012).

    ⚠ The far-side row is deliberately flagged 0. That is the shape of both
    write-offs that actually reach this population on prod (IV6701775
    ฿10,200.00 and IV6801241 ฿2.00, both `excludes_revenue = 0`, measured
    2026-09-17), and a fixture flagged 1 would sit on the KEEPING side of a
    flag-only filter — the clause would never fire and deleting it would stay
    green (#471).

    Controls, both in this run: an ordinary unpaid bill of the same shape for a
    second customer still renders its debt, and the written-off customer's own
    row is asserted present (by `_trip_due`) before its figure is asserted
    absent. Then the ar_writeoffs row alone is deleted and the page re-read, so
    the disappearance is pinned to THAT row and not to some other filter.
    """
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    # Force the state; never inherit it — tmp_db is a clone of the live DB.
    conn.execute("DELETE FROM customers WHERE code IN ('C-WO568','C-OK568')")
    conn.execute("DELETE FROM sales_transactions WHERE doc_base IN ('IV-WO568','IV-OK568')")
    conn.execute("DELETE FROM ar_writeoffs WHERE doc_no IN ('IV-WO568','IV-OK568')")
    # ภาคตะวันออก (customer_geo.region_of on the address) scopes the request to
    # a small deterministic group, same trick as the tests above.
    conn.execute(
        "INSERT INTO customers (code, name, address) VALUES "
        "('C-WO568','ร้านทดสอบตัดหนี้568','123 ถ.สุขุมวิท ชลบุรี'),"
        "('C-OK568','ร้านทดสอบค้างจริง568','456 ถ.สุขุมวิท ชลบุรี')")
    # Two unpaid IVs of identical shape. The only difference is the write-off.
    conn.execute("""INSERT INTO sales_transactions
                      (date_iso, doc_no, doc_base, customer, customer_code,
                       qty, unit, unit_price, vat_type, total, net)
                    VALUES ('2026-07-01','IV-WO568-1','IV-WO568','ร้านทดสอบตัดหนี้568','C-WO568',
                            1,'ตัว',12345.0,1,12345.0,12345.0)""")
    conn.execute("""INSERT INTO sales_transactions
                      (date_iso, doc_no, doc_base, customer, customer_code,
                       qty, unit, unit_price, vat_type, total, net)
                    VALUES ('2026-07-01','IV-OK568-1','IV-OK568','ร้านทดสอบค้างจริง568','C-OK568',
                            1,'ตัว',6789.0,1,6789.0,6789.0)""")
    conn.execute("""INSERT INTO ar_writeoffs
                      (doc_no, customer_code, customer_name, amount, type,
                       writeoff_date, reason, excludes_revenue)
                    VALUES ('IV-WO568','C-WO568','ร้านทดสอบตัดหนี้568',12345.0,'expense',
                            '2026-06-05','#568 fixture — bad debt, revenue kept',0)""")
    conn.commit()
    # The far-side property, asserted rather than assumed: a row this clause
    # excludes and the flag-only reading does NOT.
    flag = conn.execute(
        "SELECT excludes_revenue FROM ar_writeoffs WHERE doc_no='IV-WO568'").fetchone()[0]
    assert flag == 0, 'fixture drifted to the keeping side of a flag-only filter'
    conn.close()

    from app import app
    app.config['TESTING'] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1; sess['username'] = 'admin'; sess['role'] = 'admin'
    from urllib.parse import quote
    url = f"/m/sales-trip?region={quote('ภาคตะวันออก')}"
    body = c.get(url).get_data(as_text=True)

    assert _trip_due(body, 'ร้านทดสอบค้างจริง568') == 6789.0, (
        'control failed — an ordinary unpaid IV of the same shape must still '
        'render as outstanding debt, or this test proves nothing about write-offs')
    leaked = _trip_due(body, 'ร้านทดสอบตัดหนี้568')
    assert leaked is None, (
        'written-off IV-WO568 (฿12,345.00, excludes_revenue=0, written off '
        '2026-06-05) leaked into /m/sales-trip as ฿{} — a rep would be told a '
        'forgiven bill is owed (#568)'.format(leaked))

    # Pin the cause: drop ONLY the ar_writeoffs row and the same bill returns.
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM ar_writeoffs WHERE doc_no='IV-WO568'")
    conn.commit()
    conn.close()
    body2 = c.get(url).get_data(as_text=True)
    assert _trip_due(body2, 'ร้านทดสอบตัดหนี้568') == 12345.0, (
        'control failed — with the ar_writeoffs row gone the bill must reappear, '
        'otherwise something other than the write-off exclusion hid it')
