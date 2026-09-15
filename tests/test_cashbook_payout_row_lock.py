"""Payout-sourced cashbook rows are locked (issue #533) — same "linked & locked
row" idea as salary (ADR 0006) / advance / commission (ADR 0008), just keyed
by the marketplace_payouts natural key (payout_platform IS NOT NULL) instead
of a stable FK id (see cashbook_payout_mirror.py's module docstring for why).

Edit and delete on `/cashbook/txn/<id>/{edit,delete}` must refuse a
payout-sourced row (403, row unchanged) and keep manual rows unaffected.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database
import cashbook_payout_mirror as mirror


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("DELETE FROM marketplace_payouts")
    c.execute("DELETE FROM cashbook_transactions WHERE payout_platform IS NOT NULL")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _client_as(role, tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _seed_mirrored_row(conn):
    """Seed one real payout + mirror it, return the resulting txn_id."""
    conn.execute(
        "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders, status)"
        " VALUES ('shopee', '2026-03-10', 3596.00, 12, 'reconciled')"
    )
    conn.commit()
    mirror.mirror_platform(conn, 'shopee')
    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payout_platform='shopee'"
    ).fetchone()
    return row['id']


def test_edit_payout_row_rejected_403_unchanged(conn, tmp_db):
    txn_id = _seed_mirrored_row(conn)
    account_id = conn.execute(
        "SELECT account_id FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()['account_id']

    c = _client_as('admin', tmp_db)
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-03-11",
        "direction": "income", "category": "ยอดขายของ", "amount": "1",
    })
    assert resp.status_code == 403

    row = conn.execute(
        "SELECT amount, txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert float(row['amount']) == 3596.00 and row['txn_date'] == '2026-03-10'


def test_delete_payout_row_rejected_403_unchanged(conn, tmp_db):
    txn_id = _seed_mirrored_row(conn)

    c = _client_as('admin', tmp_db)
    resp = c.post(f"/cashbook/txn/{txn_id}/delete", data={})
    assert resp.status_code == 403

    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row is not None, "payout-sourced row must survive a delete attempt from the UI"


def test_manual_row_edit_and_delete_still_work(conn, tmp_db):
    account_id = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code='SPX'"
    ).fetchone()['id']
    cur = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, description, created_by)"
        " VALUES (?, '2026-03-01', 'income', 'ยอดขายของ', 200, 'คีย์มือ', 'พุธ')",
        (account_id,),
    )
    conn.commit()
    txn_id = cur.lastrowid

    c = _client_as('admin', tmp_db)
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-03-02",
        "direction": "income", "category": "ยอดขายของ", "amount": "250",
    })
    assert resp.status_code == 302

    row = conn.execute(
        "SELECT amount FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert float(row['amount']) == 250
