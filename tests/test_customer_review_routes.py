"""Route-level integration tests for bp_customer_review (/customers/normalize).

Uses tmp_db (a live-DB clone that already carries the customer_contact_review
rows from the P2 backfill) so routes + templates + the confirm/skip writes run
end-to-end and never touch the real DB. CSRF is disabled by conftest.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import json
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


def _pending_code(tmp_db):
    """A pending review row whose customer_code exists in customers."""
    row = sqlite3.connect(tmp_db).execute(
        """SELECT ccr.customer_code FROM customer_contact_review ccr
           JOIN customers c ON c.code = ccr.customer_code
           WHERE ccr.status='pending' ORDER BY ccr.customer_code LIMIT 1"""
    ).fetchone()
    if row is None:
        pytest.skip("No pending review row with a customers match in DB clone")
    return row[0]


def test_worklist_renders(admin_client):
    resp = admin_client.get('/customers/normalize')
    assert resp.status_code == 200, resp.data[:500]
    # status filter + 'all' both render
    assert admin_client.get('/customers/normalize?status=all').status_code == 200
    assert admin_client.get('/customers/normalize?status=applied').status_code == 200


def test_detail_renders(admin_client, tmp_db):
    code = _pending_code(tmp_db)
    resp = admin_client.get('/customers/normalize/' + code)
    assert resp.status_code == 200, resp.data[:500]


def test_confirm_writes_to_customers_and_freezes_original(admin_client, tmp_db):
    code = _pending_code(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    orig_json = conn.execute(
        "SELECT original_json FROM customer_contact_review WHERE customer_code=?",
        (code,)).fetchone()['original_json']
    # pending rows haven't been auto-applied, so the snapshot is not frozen yet
    pre = conn.execute(
        "SELECT contact_orig_json FROM customers WHERE code=?", (code,)).fetchone()
    assert pre['contact_orig_json'] is None
    conn.close()

    resp = admin_client.post('/customers/normalize/' + code + '/confirm', data={
        'proposed_name': 'ทดสอบ บริษัท',
        'proposed_nickname': 'เจ๊ทดสอบ',
        'proposed_phone': '099-9999999',
        'proposed_fax': '02-1111111',
        'proposed_contact': 'คุณทดสอบ',
        'proposed_address': '123 ถนนทดสอบ กรุงเทพมหานคร',
    })
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    cust = conn.execute("SELECT * FROM customers WHERE code=?", (code,)).fetchone()
    review = conn.execute(
        "SELECT * FROM customer_contact_review WHERE customer_code=?", (code,)).fetchone()
    conn.close()

    assert cust['phone'] == '099-9999999'
    assert cust['fax'] == '02-1111111'
    assert cust['nickname'] == 'เจ๊ทดสอบ'
    assert cust['contact'] == 'คุณทดสอบ'
    assert cust['name'] == 'ทดสอบ บริษัท'
    assert cust['contact_normalized_at'] is not None
    assert cust['contact_normalized_by'] == 'test-admin'
    # original snapshot frozen to the review's original_json (lossless guarantee)
    assert cust['contact_orig_json'] == orig_json
    # review row flipped to confirmed
    assert review['status'] == 'confirmed'
    assert review['reviewed_by'] == 'test-admin'
    assert review['proposed_phone'] == '099-9999999'


def test_confirm_blank_name_falls_back_to_existing(admin_client, tmp_db):
    code = _pending_code(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    existing_name = conn.execute(
        "SELECT name FROM customers WHERE code=?", (code,)).fetchone()['name']
    conn.close()

    resp = admin_client.post('/customers/normalize/' + code + '/confirm', data={
        'proposed_name': '',  # blank — must NOT null out customers.name
        'proposed_phone': '02-2222222',
    })
    assert resp.status_code == 302
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    name = conn.execute("SELECT name FROM customers WHERE code=?", (code,)).fetchone()['name']
    conn.close()
    assert name == existing_name  # preserved, not NULL


def test_skip_marks_skipped_without_touching_customers(admin_client, tmp_db):
    code = _pending_code(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    before = conn.execute("SELECT phone FROM customers WHERE code=?", (code,)).fetchone()['phone']
    conn.close()

    resp = admin_client.post('/customers/normalize/' + code + '/skip', data={})
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    review = conn.execute(
        "SELECT status, reviewed_by FROM customer_contact_review WHERE customer_code=?",
        (code,)).fetchone()
    after = conn.execute("SELECT phone FROM customers WHERE code=?", (code,)).fetchone()['phone']
    conn.close()
    assert review['status'] == 'skipped'
    assert review['reviewed_by'] == 'test-admin'
    assert after == before  # customers untouched
