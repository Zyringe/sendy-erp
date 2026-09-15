"""Issue #532 §3 — the cashbook edit path validates the date the same way
new entry does (a real ISO date), not just non-empty. New entry already
does this via `date.fromisoformat` in `_validate_batch`; edit only checked
`if not txn_date`, so 'ไม่ใช่วันที่'/'2026-13-40' silently reached the UPDATE.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


def _client_as(role, user_id=1):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _active_account(tmp_db):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0"
        " ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("No active non-transfer cashbook account in live DB clone")
    return row[0]


def _plain_row(tmp_db, account_id, amount=250.0):
    conn = sqlite3.connect(tmp_db)
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount)"
        " VALUES (?, '2026-08-02', 'expense', 'อื่นๆ', NULL, ?)",
        (account_id, amount),
    ).lastrowid
    conn.commit()
    conn.close()
    return txn_id


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-13-40", "31/12/2026"])
def test_edit_malformed_date_refused(tmp_db, bad_date):
    account_id = _active_account(tmp_db)
    txn_id = _plain_row(tmp_db, account_id)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": bad_date, "direction": "expense",
        "category": "อื่นๆ", "amount": "250",
    }, follow_redirects=True)
    assert "รูปแบบวันที่ไม่ถูกต้อง" in resp.get_data(as_text=True)

    row = sqlite3.connect(tmp_db).execute(
        "SELECT txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "2026-08-02", "malformed date must never reach the UPDATE"


def test_edit_valid_iso_date_still_applies(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _plain_row(tmp_db, account_id)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-09", "direction": "expense",
        "category": "อื่นๆ", "amount": "250",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "2026-08-09"
