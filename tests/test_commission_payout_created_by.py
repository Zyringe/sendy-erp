"""Issue #532 §4 — a commission payout's auto-posted cashbook row must stamp
the same keyer name as a cashbook form entry by the same user.

Before this fix, `/commission/payout` stamped the login USERNAME
(`session['username']`, e.g. 'admin') while the cashbook form and payroll
stamp the DISPLAY NAME (`session['display_name']`, e.g. 'Put') — one person
showed up two ways in the ledger's `created_by` column.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _client_as_user(user_id, role, display_name):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = display_name
        sess['role'] = role
    return c


def _account_id_by_code(db, code):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    conn.close()
    if row is None:
        pytest.skip(f"No cashbook_accounts.code={code!r} in live DB clone")
    return row[0]


def test_payout_stamps_display_name_same_as_cashbook_form(migrated_db):
    acct_392 = _account_id_by_code(migrated_db, '392')
    c = _client_as_user(1, 'admin', 'Put')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '350',
        'paid_date': '2026-07-11',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    row = conn.execute(
        "SELECT created_by FROM cashbook_transactions"
        " WHERE category='จ่ายค่าคอมมิชชั่น' AND amount=350"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 'Put', "must stamp the display name, not the login username"


def test_payout_falls_back_to_username_when_no_display_name(migrated_db):
    """Session without a display_name (e.g. an older login) must not stamp
    an empty string — falls back to username, same as the cashbook form's
    own `session.get('display_name') or session.get('username')`."""
    acct_392 = _account_id_by_code(migrated_db, '392')
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        # deliberately no display_name

    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '360',
        'paid_date': '2026-07-11',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    row = conn.execute(
        "SELECT created_by FROM cashbook_transactions"
        " WHERE category='จ่ายค่าคอมมิชชั่น' AND amount=360"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 'admin'
