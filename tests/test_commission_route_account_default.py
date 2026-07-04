"""Phase 3 — /commission/payout pay-from account defaulting + validation
(plan.md decision C4). Route-level: the payout POST always resolves an
account (form override, else the logged-in user's data-entry default) BEFORE
recording anything; an invalid/missing account writes nothing. The atomicity
of a single record_payout call is already covered at the unit level in
test_commission_cashbook_writeback.py — this file covers the route's account
resolution + multi-row loop wiring.
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


def _client_as_user(user_id, role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _account_id_by_code(db, code):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    conn.close()
    if row is None:
        pytest.skip(f"No cashbook_accounts.code={code!r} in live DB clone")
    return row[0]


def test_payout_with_no_account_defaults_to_users_account(migrated_db):
    # mig 126 seeds admin (user id 1) -> cashbook_accounts.code '392'.
    acct_392 = _account_id_by_code(migrated_db, '392')
    c = _client_as_user(1, 'admin')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '350',
        'paid_date': '2026-07-11',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE category='จ่ายค่าคอมมิชชั่น' AND amount=350"
    ).fetchone()
    conn.close()
    assert row is not None, "auto-post must use the user's default account when none is given"
    assert row['account_id'] == acct_392


def test_payout_with_invalid_account_writes_nothing_no_500(migrated_db):
    # Count-diff, not an amount filter: the live DB clone carries 2071
    # historical payouts, so a fixed test amount can collide with a real
    # historical row (caught during TDD — amount=450 already existed).
    conn = sqlite3.connect(migrated_db)
    n_payout_before = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]
    n_cb_before = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]
    conn.close()

    c = _client_as_user(1, 'admin')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '450',
        'paid_date': '2026-07-12', 'account_id': '999999',
    }, follow_redirects=False)
    assert resp.status_code == 302, "invalid account must flash + redirect, never 500"

    conn = sqlite3.connect(migrated_db)
    n_payout_after = conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0]
    n_cb_after = conn.execute("SELECT COUNT(*) FROM cashbook_transactions").fetchone()[0]
    conn.close()
    assert n_payout_after == n_payout_before, "no commission_payouts row on an invalid account"
    assert n_cb_after == n_cb_before


def test_payout_with_explicit_account_override(migrated_db):
    conn = sqlite3.connect(migrated_db)
    acct = conn.execute(
        "SELECT id FROM cashbook_accounts"
        " WHERE is_active=1 AND is_transfer=0 AND code != '392' LIMIT 1"
    ).fetchone()
    conn.close()
    if acct is None:
        pytest.skip("no second non-transfer account in the live DB clone")
    acct_id = acct[0]

    c = _client_as_user(1, 'admin')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '260',
        'paid_date': '2026-07-13', 'account_id': str(acct_id),
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    row = conn.execute(
        "SELECT account_id FROM cashbook_transactions"
        " WHERE amount=260 AND category='จ่ายค่าคอมมิชชั่น'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == acct_id, "explicit account_id in the form must override the default"


def test_payout_user_with_no_default_and_no_override_writes_nothing(migrated_db):
    # user id 4 (manager 'ss') has default_cashbook_account_id = NULL in the
    # live DB (mirrors test_cashbook_new_bulk_and_defaults.py's ground truth).
    conn = sqlite3.connect(migrated_db)
    default_acct = conn.execute(
        "SELECT default_cashbook_account_id FROM users WHERE id=4"
    ).fetchone()[0]
    conn.close()
    if default_acct is not None:
        pytest.skip("user id 4 unexpectedly has a default account in this DB clone")

    c = _client_as_user(4, 'manager')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '06', 'amount_06': '170',
        'paid_date': '2026-07-14',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    n_cb = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE amount=170"
    ).fetchone()[0]
    n_payout = conn.execute(
        "SELECT COUNT(*) FROM commission_payouts WHERE amount_paid=170"
    ).fetchone()[0]
    conn.close()
    assert n_cb == 0
    assert n_payout == 0, "the whole payout must be rejected, not just the cashbook side"


def test_delete_payout_route_cascades_linked_cashbook_row(migrated_db):
    c = _client_as_user(1, 'admin')
    resp = c.post('/commission/payout', data={
        'month': '2026-07', 'sp_code': '03', 'amount_03': '90',
        'paid_date': '2026-07-15',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    payout = conn.execute(
        "SELECT id FROM commission_payouts WHERE salesperson_code='03' AND amount_paid=90"
    ).fetchone()
    assert payout is not None
    payout_id = payout['id']
    txn = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()
    assert txn is not None
    conn.close()

    resp2 = c.post(f'/commission/payout/{payout_id}/delete', follow_redirects=False)
    assert resp2.status_code == 302

    conn = sqlite3.connect(migrated_db)
    n_payout = conn.execute(
        "SELECT COUNT(*) FROM commission_payouts WHERE id=?", (payout_id,)
    ).fetchone()[0]
    n_cb = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE id=?", (txn['id'],)
    ).fetchone()[0]
    conn.close()
    assert n_payout == 0
    assert n_cb == 0
