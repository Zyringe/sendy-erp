"""TDD for Phase 3 of projects/customer-edit-card/plan.md — bulk page →
region-only + customer-list "include bill-less" toggle.

Two independent pieces:

  A. Bulk reassign loses its salesperson mode. Put's decision: no UI may
     change `customers.salesperson` for many customers at once — commission
     rules (models/commission.py) do not follow a master-record salesperson
     change, and 472 customers carry an active commission rule perfectly
     aligned with their master today (P1/P2 baton). `bulk_reassign_customers`
     is now region-only; a request that still carries a salesperson target
     is REJECTED, not silently ignored.

  B. `/customers` gets an opt-in "รวมลูกค้าที่ไม่มีบิล" checkbox.
     `get_customers` is `FROM sales_transactions LEFT JOIN customers`, so the
     page shows only customers with a bill. `include_billless=True` UNIONs in
     `customers` master rows with no sales_transactions row at all (doc_count
     0, total_net 0, last_date NULL). Default OFF, so today's view/totals are
     unchanged.

Baseline note (verified 2026-08-01 against the live DB copied by the `tmp_db`
fixture): the plan text cites 275 billing customers / 2,665 total — that has
already drifted to 276 / 2,666 by the time this file was written (one more
customer has since billed). Counts below are derived from an INDEPENDENT SQL
query against the SAME tmp_db snapshot the test runs against, not hardcoded
to either figure, so this file can't rot the way a literal would.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
from urllib.parse import quote


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _raw_conn(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    return conn


# ── A. bulk_reassign_customers is region-only ───────────────────────────────

def test_region_only_bulk_still_updates_customers(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    before = conn.execute(
        "SELECT region_id FROM customers WHERE code IN ('01ก01','01ค01')"
    ).fetchall()
    assert {r['region_id'] for r in before} == {3, 17}  # sanity: distinct, known start state

    result = models.bulk_reassign_customers(['01ก01', '01ค01'], region_id=1)
    assert result['ok'] is True
    assert result['updated'] == 2

    after = conn.execute(
        "SELECT code, region_id FROM customers WHERE code IN ('01ก01','01ค01')"
    ).fetchall()
    conn.close()
    assert {r['region_id'] for r in after} == {1}


def test_bulk_rejects_a_request_carrying_a_salesperson_target(tmp_db):
    """A stale page rendered before this deploy could still POST a
    `salesperson` value. It must be REJECTED with a clear error, and the
    master rows must be untouched — not silently dropped and applied
    region-only anyway (that would look like partial success)."""
    import models
    conn = _raw_conn(tmp_db)
    before = dict(conn.execute(
        "SELECT code, salesperson, region_id FROM customers WHERE code = '01ก01'"
    ).fetchone())

    result = models.bulk_reassign_customers(
        ['01ก01'], region_id=1, salesperson_code='13')
    assert result['ok'] is False
    assert result['updated'] == 0
    assert 'salesperson' in result['error'] or 'คอมมิชชั่น' in result['error']

    after = dict(conn.execute(
        "SELECT code, salesperson, region_id FROM customers WHERE code = '01ก01'"
    ).fetchone())
    conn.close()
    assert after == before  # nothing moved, not even the region


def test_bulk_no_mode_kwarg_left_in_signature(tmp_db):
    """The function no longer accepts a mode — region-only was the only mode
    that survives. Calling with the old `mode=` kwarg must fail loudly
    (TypeError), not be silently ignored."""
    import models
    import inspect
    sig = inspect.signature(models.bulk_reassign_customers)
    assert 'mode' not in sig.parameters


def test_bulk_still_rejects_empty_selection_and_bad_region(tmp_db):
    import models
    r1 = models.bulk_reassign_customers([], region_id=1)
    assert r1['ok'] is False
    r2 = models.bulk_reassign_customers(['01ก01'], region_id='')
    assert r2['ok'] is False
    r3 = models.bulk_reassign_customers(['01ก01'], region_id=999999)
    assert r3['ok'] is False


# ── A2. Route level: /customers/bulk-reassign POST ──────────────────────────

def test_route_region_only_post_updates_master(tmp_db):
    c = _client(tmp_db, role='admin')
    r = c.post('/customers/bulk-reassign', data={
        'customer_codes': ['01ก01'],
        'region_id': '1',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert 'อัปเดต 1 ลูกค้าเรียบร้อย' in r.data.decode()

    conn = _raw_conn(tmp_db)
    row = conn.execute("SELECT region_id FROM customers WHERE code='01ก01'").fetchone()
    conn.close()
    assert row['region_id'] == 1


def test_route_rejects_salesperson_field_when_present(tmp_db):
    """Simulates a stale pre-deploy form still submitting `salesperson`."""
    conn = _raw_conn(tmp_db)
    before = conn.execute("SELECT salesperson, region_id FROM customers WHERE code='01ก01'").fetchone()
    before = dict(before)
    conn.close()

    c = _client(tmp_db, role='admin')
    r = c.post('/customers/bulk-reassign', data={
        'customer_codes': ['01ก01'],
        'region_id': '1',
        'salesperson': '13',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert 'ไม่สามารถบันทึก' in r.data.decode()

    conn = _raw_conn(tmp_db)
    after = dict(conn.execute("SELECT salesperson, region_id FROM customers WHERE code='01ก01'").fetchone())
    conn.close()
    assert after == before


# ── A3. Template: salesperson dropdown + mode selector gone ─────────────────

def test_bulk_template_has_no_salesperson_or_mode_inputs(tmp_db):
    c = _client(tmp_db, role='admin')
    html = c.get('/customers/bulk-reassign').data.decode()
    # The bulk ASSIGNMENT dropdown named exactly "salesperson" (distinct from
    # the read-only "salesperson_filter" current-value filter, which stays).
    assert html.count('name="salesperson"') == 0
    assert html.count('name="mode"') == 0
    assert html.count('name="region_id"') == 1  # the one bulk-target select
    assert '>กำหนดเขตการขายหลายราย<' in html


# ── B. get_customers(include_billless=...) ──────────────────────────────────

def test_default_billing_only_matches_independent_count(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    expected = conn.execute("""
        SELECT COUNT(DISTINCT customer_code) FROM sales_transactions
        WHERE COALESCE(doc_base, doc_no) NOT IN (
            SELECT doc_no FROM ar_writeoffs WHERE excludes_revenue = 1)
    """).fetchone()[0]
    conn.close()

    rows, total = models.get_customers()
    assert total == expected
    assert len(rows) == min(expected, 50)  # default per_page


def test_include_billless_unions_master_rows_with_zero_sales(tmp_db):
    import models
    conn = _raw_conn(tmp_db)
    billing_count = conn.execute("""
        SELECT COUNT(DISTINCT customer_code) FROM sales_transactions
        WHERE COALESCE(doc_base, doc_no) NOT IN (
            SELECT doc_no FROM ar_writeoffs WHERE excludes_revenue = 1)
    """).fetchone()[0]
    billless_master_count = conn.execute("""
        SELECT COUNT(*) FROM customers c
        WHERE NOT EXISTS (SELECT 1 FROM sales_transactions s WHERE s.customer_code = c.code)
    """).fetchone()[0]
    conn.close()

    rows, total = models.get_customers(include_billless=True, per_page=5000)
    assert total == billing_count + billless_master_count

    by_code = {r['customer_code']: r for r in rows}
    known = by_code['01ก01']
    assert known['customer'] == 'หจก. กรีน ดิสทธิบิวชั่น (สิทธิกร)'
    assert known['doc_count'] == 0
    assert known['total_net'] == 0
    assert known['last_date'] is None


def test_include_billless_false_omits_known_billless_customer(tmp_db):
    import models
    rows, total = models.get_customers(include_billless=False, per_page=5000)
    codes = {r['customer_code'] for r in rows}
    assert '01ก01' not in codes


def test_search_and_region_filter_work_with_flag_on(tmp_db):
    import models
    # Search narrows to exactly the known bill-less customer.
    rows, total = models.get_customers(
        search='กรีน ดิสทธิบิวชั่น', include_billless=True)
    assert total == 1
    assert rows[0]['customer_code'] == '01ก01'

    # Region filter (region_id=3, the customer's real region) still includes it.
    rows2, total2 = models.get_customers(
        region_id=3, include_billless=True, per_page=5000)
    codes2 = {r['customer_code'] for r in rows2}
    assert '01ก01' in codes2
    assert all(r['region_id'] == 3 for r in rows2)

    # A region that is NOT its region excludes it.
    rows3, total3 = models.get_customers(
        region_id=17, include_billless=True, per_page=5000)
    codes3 = {r['customer_code'] for r in rows3}
    assert '01ก01' not in codes3


# ── B2. Route + template: /customers toggle ─────────────────────────────────

def test_customers_page_default_omits_billless_customer(tmp_db):
    c = _client(tmp_db, role='admin')
    html = c.get('/customers').data.decode()
    assert 'หจก. กรีน ดิสทธิบิวชั่น (สิทธิกร)' not in html


def test_customers_page_toggle_on_shows_billless_customer_linked_by_code(tmp_db):
    c = _client(tmp_db, role='admin')
    html = c.get(
        '/customers?include_billless=1&q=' + quote('กรีน ดิสทธิบิวชั่น')
    ).data.decode()
    assert 'หจก. กรีน ดิสทธิบิวชั่น (สิทธิกร)' in html
    # Bill-less rows have no bill name to link by — must be code-keyed, never
    # a name-built link (that would route through the shim and eject the
    # user, per the plan's non-obvious requirement).
    assert f"/customer/code/{quote('01ก01')}" in html


def test_customers_page_existing_row_also_linked_by_code(tmp_db):
    """P1's non-obvious requirement applies to every row this template
    renders, not just the new bill-less ones — pin the regression."""
    c = _client(tmp_db, role='admin')
    html = c.get('/customers?q=' + quote('คิมเฮง')).data.decode()
    assert f"/customer/code/{quote('11ค09')}" in html
    assert "partners.customer_summary" not in html  # no name-built href leaked


def test_customers_page_checkbox_present_and_default_unchecked(tmp_db):
    c = _client(tmp_db, role='admin')
    html = c.get('/customers').data.decode()
    assert 'name="include_billless"' in html
    assert 'id="billless-cb" name="include_billless" value="1" ' in html \
        or 'id="billless-cb"\n                 name="include_billless" value="1" ' in html
    # unchecked by default: no "checked" token right after the billless input
    import re
    m = re.search(r'<input[^>]*id="billless-cb"[^>]*>', html)
    assert m and 'checked' not in m.group(0)


def test_customers_page_checkbox_checked_when_toggle_on(tmp_db):
    c = _client(tmp_db, role='admin')
    html = c.get('/customers?include_billless=1').data.decode()
    import re
    m = re.search(r'<input[^>]*id="billless-cb"[^>]*>', html)
    assert m and 'checked' in m.group(0)
