"""#542 — cancelling a commission payout stays admin-only; only the WORDING
changes for every role that cannot cancel. Two surfaces name the wording:

  1. The lock tooltip on a commission-linked cashbook row (account_ledger.html,
     added by #541).
  2. The flash in `_reject_if_commission_row` (blueprints/cashbook.py), hit on
     a direct POST to txn_edit / txn_delete of a commission-linked row.

"Can cancel" is derived from the SAME rule that gates
`commission.commission_delete_payout` — `access_control.role_can_post(role,
'commission.commission_delete_payout')` — never a hand-typed role tuple here.
Today that rule says admin-only (the endpoint is absent from every POST
whitelist except admin's implicit bypass); `test_role_can_post_commission_delete_payout`
below pins that fact independently of the render/flash tests, so a future
change to the whitelist (e.g. manager gaining the right to cancel) is caught
by ONE test rather than silently drifting the wording out of sync.

Assertions on rendered HTML are scoped to the ONE `<tr>` for the commission
row (never a page-wide substring — the page's own JS/other rows can repeat
the same words), per .claude/rules/verification-discipline.md.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest

import access_control
import database
import commission as commission_mod

SP_CODE = '542-TEST-SP'
SP_NAME = 'ทดสอบ 542 /99'

ADMIN_WORDING = 'ยกเลิกได้ที่หน้าคอมมิชชั่นเท่านั้น'
NONADMIN_WORDING = 'ให้แอดมินยกเลิกที่หน้าคอมมิชชั่น'


@pytest.fixture
def conn(tmp_db):
    database.init_db()
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # Force the state — don't rely on the cloned dev DB already holding a
    # commission-linked row or this salesperson code (mirrors #541's fixture).
    c.execute("DELETE FROM cashbook_transactions WHERE commission_payout_id IS NOT NULL")
    c.execute("DELETE FROM commission_payouts WHERE salesperson_code = ?", (SP_CODE,))
    c.execute("DELETE FROM salespersons WHERE code = ?", (SP_CODE,))
    c.execute("INSERT INTO salespersons (code, name) VALUES (?, ?)", (SP_CODE, SP_NAME))
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


def _account_id(conn, code='392'):
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    assert row is not None, f"expected seeded cashbook_accounts row code={code}"
    return row['id']


def _row_fragment(html, marker):
    rows = re.findall(r'<tr\b.*?</tr>', html, re.DOTALL)
    hits = [r for r in rows if marker in r]
    assert len(hits) == 1, f"expected exactly one row containing {marker!r}, found {len(hits)}"
    return hits[0]


def _seed_commission_row(conn):
    """Real write-back path (commission.record_payout with account_id set —
    same call the /commission/payout route makes), so the fixture matches a
    real linked row byte-for-byte (mirrors #541's fixture)."""
    account_id = _account_id(conn)
    payout_id = commission_mod.record_payout(
        year_month='2026-07', salesperson_code=SP_CODE, amount_paid=999.0,
        paid_date='2026-07-05', paid_method='cash', paid_by='test-admin',
        account_id=account_id, conn=conn,
    )
    conn.commit()
    marker = f'ค่าคอมมิชชั่น 2026-07 — {SP_NAME}'
    txn_id = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE commission_payout_id=?", (payout_id,)
    ).fetchone()['id']
    return account_id, txn_id, marker


# ── 0. the derivation rule itself ───────────────────────────────────────────

def test_role_can_post_commission_delete_payout():
    """Pins the rule the wording is derived from, independent of the
    render/flash tests — if this ever flips (e.g. manager gains cancel
    rights), the wording tests below must be revisited too."""
    assert access_control.role_can_post('admin', 'commission.commission_delete_payout') is True
    assert access_control.role_can_post('manager', 'commission.commission_delete_payout') is False
    assert access_control.role_can_post('shareholder', 'commission.commission_delete_payout') is False
    assert access_control.role_can_post('staff', 'commission.commission_delete_payout') is False


# ── 1. render: lock tooltip on account_ledger.html ─────────────────────────

def test_admin_sees_admin_cancel_wording_in_tooltip(conn, tmp_db):
    account_id, _txn_id, marker = _seed_commission_row(conn)
    html = _client_as('admin', tmp_db).get(
        f'/cashbook/account/{account_id}?month=2026-07'
    ).get_data(as_text=True)
    row = _row_fragment(html, marker)
    assert ADMIN_WORDING in row
    assert NONADMIN_WORDING not in row
    # #541's lock + link must be unchanged in this same fragment.
    assert 'bi-lock-fill' in row
    assert 'href="/commission/payouts"' in row


@pytest.mark.parametrize('role', ['manager', 'shareholder'])
def test_nonadmin_sees_admin_wording_in_tooltip(conn, tmp_db, role):
    account_id, _txn_id, marker = _seed_commission_row(conn)
    html = _client_as(role, tmp_db).get(
        f'/cashbook/account/{account_id}?month=2026-07'
    ).get_data(as_text=True)
    row = _row_fragment(html, marker)
    assert NONADMIN_WORDING in row
    assert ADMIN_WORDING not in row
    assert 'bi-lock-fill' in row
    assert 'href="/commission/payouts"' in row


# ── 2. flash: direct POST to txn_edit / txn_delete of a commission row ─────

@pytest.mark.parametrize('role,expected_wording,other_wording', [
    ('admin', ADMIN_WORDING, NONADMIN_WORDING),
    ('manager', NONADMIN_WORDING, ADMIN_WORDING),
    ('shareholder', NONADMIN_WORDING, ADMIN_WORDING),
])
def test_txn_delete_commission_row_flash_is_role_aware(conn, tmp_db, role, expected_wording, other_wording):
    account_id, txn_id, _marker = _seed_commission_row(conn)
    client = _client_as(role, tmp_db)
    resp = client.post(f'/cashbook/txn/{txn_id}/delete', data={}, follow_redirects=False)
    assert resp.status_code == 403, f"{role} POST must still 403 on a commission row"
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    msgs = [m for _cat, m in flashes]
    assert any(expected_wording in m for m in msgs), (
        f"{role}: expected {expected_wording!r} in flashes, got {msgs!r}"
    )
    assert not any(other_wording in m for m in msgs)
    # The row must still exist untouched — the 403 happened before any write.
    still_there = conn.execute(
        "SELECT commission_payout_id FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert still_there is not None and still_there['commission_payout_id'] is not None


@pytest.mark.parametrize('role,expected_wording,other_wording', [
    ('admin', ADMIN_WORDING, NONADMIN_WORDING),
    ('manager', NONADMIN_WORDING, ADMIN_WORDING),
])
def test_txn_edit_commission_row_flash_is_role_aware(conn, tmp_db, role, expected_wording, other_wording):
    account_id, txn_id, _marker = _seed_commission_row(conn)
    client = _client_as(role, tmp_db)
    resp = client.post(f'/cashbook/txn/{txn_id}/edit', data={
        'account_id': str(account_id), 'txn_date': '2026-07-05', 'direction': 'expense',
        'category': 'จ่ายค่าคอมมิชชั่น', 'amount': '1.00',
    }, follow_redirects=False)
    assert resp.status_code == 403
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    msgs = [m for _cat, m in flashes]
    assert any(expected_wording in m for m in msgs)
    assert not any(other_wording in m for m in msgs)
    amount = conn.execute(
        "SELECT amount FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()['amount']
    assert amount == 999.0, "edit must not have applied — the row is locked"
