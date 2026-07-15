"""Admin cashbook-account management (/cashbook-accounts).

Self-service CRUD for the cash/bank accounts behind the /cashbook dashboard
cards. Admin-only, mirrors /users. NO schema change.

The load-bearing test is the delete guard: a hard delete is offered only for a
truly-unreferenced account, and the route must REFUSE (not 500) when the account
is referenced by ANY FK — transactions OR a user/employee default OR a salary
advance (scrutiny finding: a 0-transaction account can still be someone's
default, and foreign_keys=ON makes the DELETE raise IntegrityError).
"""
import os
import sqlite3

import pytest


def _client_as(role, tmp_db):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _accounts(db):
    return _conn(db).execute("SELECT id, code FROM cashbook_accounts").fetchall()


def _acct_by_code(db, code):
    return _conn(db).execute(
        "SELECT id, is_transfer, account_owner_name, bank_name, bank_account_no,"
        " note, is_active FROM cashbook_accounts WHERE code=?", (code,)
    ).fetchone()


# ── rendering + auth ──────────────────────────────────────────────────────────

def test_list_renders_for_admin(tmp_db):
    """GET renders 200 — catches url_for BuildErrors the fresh-app tests miss."""
    c = _client_as('admin', tmp_db)
    r = c.get('/cashbook-accounts')
    assert r.status_code == 200
    assert 'จัดการบัญชี' in r.get_data(as_text=True)


def test_manager_forbidden(tmp_db):
    """Only admin manages accounts (matches /users)."""
    c = _client_as('manager', tmp_db)
    assert c.get('/cashbook-accounts').status_code == 403


# ── create ────────────────────────────────────────────────────────────────────

def test_create_cash_account_blank_bank(tmp_db):
    """The concrete use case: a เงินสด account with no bank fields → NULLs."""
    c = _client_as('admin', tmp_db)
    r = c.post('/cashbook-accounts/new', data={
        'code': 'เงินสด', 'is_transfer': '0', 'account_owner_name': '',
        'bank_name': '', 'bank_account_no': '', 'note': 'เงินสดหน้าร้าน',
    }, follow_redirects=True)
    assert r.status_code == 200
    row = _acct_by_code(tmp_db, 'เงินสด')
    assert row is not None
    assert row['is_transfer'] == 0
    assert row['bank_name'] is None and row['bank_account_no'] is None
    assert row['account_owner_name'] is None
    assert row['note'] == 'เงินสดหน้าร้าน'
    assert row['is_active'] == 1


def test_create_transfer_account(tmp_db):
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': 'พักโอน1', 'is_transfer': '1'},
           follow_redirects=True)
    assert _acct_by_code(tmp_db, 'พักโอน1')['is_transfer'] == 1


def test_duplicate_code_rejected(tmp_db):
    """Existing code '392' must not create a second row."""
    before = len(_accounts(tmp_db))
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': '392'}, follow_redirects=True)
    assert len(_accounts(tmp_db)) == before


def test_blank_code_rejected(tmp_db):
    before = len(_accounts(tmp_db))
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': '   '}, follow_redirects=True)
    assert len(_accounts(tmp_db)) == before


# ── edit ──────────────────────────────────────────────────────────────────────

def test_edit_updates_fields_and_deactivate(tmp_db):
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': 'TMP1'}, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'TMP1')['id']
    # Edit with is_active unchecked (omitted) → deactivate.
    c.post(f'/cashbook-accounts/{aid}/edit', data={
        'code': 'TMP1', 'is_transfer': '0', 'bank_name': 'KBank',
        'account_owner_name': 'สมชาย',
    }, follow_redirects=True)
    row = _acct_by_code(tmp_db, 'TMP1')
    assert row['bank_name'] == 'KBank'
    assert row['account_owner_name'] == 'สมชาย'
    assert row['is_active'] == 0  # checkbox absent → inactive


# ── delete guard (the load-bearing part) ───────────────────────────────────────

def test_delete_unreferenced_account(tmp_db):
    """0 refs → real hard delete."""
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': 'DELME'}, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'DELME')['id']
    c.post(f'/cashbook-accounts/{aid}/delete', follow_redirects=True)
    assert _acct_by_code(tmp_db, 'DELME') is None


def test_delete_account_with_transactions_blocked(tmp_db):
    """Live account id=1 (code 392) has transactions → refuse, no 500."""
    c = _client_as('admin', tmp_db)
    r = c.post('/cashbook-accounts/1/delete', follow_redirects=True)
    assert r.status_code == 200            # graceful, not a 500
    assert _acct_by_code(tmp_db, '392') is not None  # still there


def test_delete_account_that_is_an_employee_default_blocked(tmp_db):
    """SCRUTINY FIX: a 0-transaction account can still be an employee's
    default_cashbook_account_id. The txn-count hint says 0, but the FK must
    still block the delete (IntegrityError caught → refuse, not 500)."""
    c = _client_as('admin', tmp_db)
    c.post('/cashbook-accounts/new', data={'code': 'DEFACC'}, follow_redirects=True)
    aid = _acct_by_code(tmp_db, 'DEFACC')['id']
    # Point an employee's default at it (0 transactions, but now referenced).
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE employees SET default_cashbook_account_id=? WHERE id=5", (aid,))
    conn.commit()
    conn.close()
    r = c.post(f'/cashbook-accounts/{aid}/delete', follow_redirects=True)
    assert r.status_code == 200                       # not a 500
    assert _acct_by_code(tmp_db, 'DEFACC') is not None  # refused — still there
