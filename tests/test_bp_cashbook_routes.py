"""Route-level integration tests for bp_cashbook.

Uses tmp_db so route + models + templates execute against a live-DB clone
and never touch the real DB. Logs in as admin via session pre-population
because the cashbook before_request middleware in app.py blocks staff
entirely from cashbook.* and manager from POST /cashbook/import.

Covers 3 GET endpoints: dashboard, per-account ledger, and the import
form page (GET — the POST path needs an .xlsx upload and is out of scope
for happy-path coverage).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

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


def _first_active_cashbook_account_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM cashbook_accounts WHERE is_active = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No active cashbook accounts in live DB clone")
    return row[0]


def test_cashbook_dashboard_renders(admin_client):
    """P&L headline + per-account totals — the landing page for the
    cashbook module."""
    resp = admin_client.get('/cashbook/')
    assert resp.status_code == 200, resp.data[:500]


def test_cashbook_account_ledger_renders(admin_client, tmp_db):
    """Per-account paginated ledger view with month/direction filters."""
    aid = _first_active_cashbook_account_id(tmp_db)
    resp = admin_client.get(f'/cashbook/account/{aid}')
    assert resp.status_code == 200, resp.data[:500]


def test_cashbook_import_view_renders(admin_client):
    """GET form page for the .xlsx import flow — POST path needs a file
    and is covered separately by test_cashbook_import.py."""
    resp = admin_client.get('/cashbook/import')
    assert resp.status_code == 200, resp.data[:500]
