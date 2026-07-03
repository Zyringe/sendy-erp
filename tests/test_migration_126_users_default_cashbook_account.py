"""Migration 126 — users.default_cashbook_account_id (Phase 1a of the
cashbook /new overhaul, projects/cashbook-entry-reconcile/plan.md decisions
A1-A3).

Verifies (via a real `database.init_db()` boot, matching how this migration
applies to an actual existing DB — not the from-empty bootstrap path):
  - users gains default_cashbook_account_id
  - the prod-safe seed (lookup by cashbook_accounts.code + users.username)
    resolves admin->392, mamaput->ชฎามาศ, s->กิติยา
  - the migration applies cleanly and is recorded in applied_migrations once
  - /users/<uid>/edit (admin-only) can set/clear the field
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import database


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _client_as(role, tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = f'test-{role}'
        sess['role']     = role
    return c


def test_users_table_gains_column(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        assert 'default_cashbook_account_id' in _cols(conn, 'users')
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations "
            "WHERE filename = '126_add_users_default_cashbook_account.sql'"
        ).fetchone()[0]
        assert applied == 1
    finally:
        conn.close()


def test_migration_is_idempotent_via_runner(tmp_db):
    """Running init_db() twice must not error and must not duplicate the
    applied_migrations row (mirrors test_migration_123's convention)."""
    database.init_db()
    database.init_db()

    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations "
            "WHERE filename = '126_add_users_default_cashbook_account.sql'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_seed_resolves_correct_account_codes(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT u.username, a.code
              FROM users u
              JOIN cashbook_accounts a ON a.id = u.default_cashbook_account_id
             WHERE u.username IN ('admin', 'mamaput', 's')
        """).fetchall()
        by_username = {r['username']: r['code'] for r in rows}
        assert by_username.get('admin') == '392'
        assert by_username.get('mamaput') == 'ชฎามาศ'
        assert by_username.get('s') == 'กิติยา'
    finally:
        conn.close()


def test_user_edit_sets_default_cashbook_account(tmp_db):
    database.init_db()
    # Pick some active account other than the one already seeded for a plain
    # staff user (id 2, username 'l', unseeded by mig 126) to prove the write
    # path, not just that the seed value happens to already be there.
    conn = sqlite3.connect(tmp_db)
    try:
        acct_id = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
        # preserve whatever employee is currently linked to user 2 — this test
        # is only about default_cashbook_account_id, not the employee picker.
        cur_emp = conn.execute(
            "SELECT id FROM employees WHERE user_id=2"
        ).fetchone()
    finally:
        conn.close()

    c = _client_as('admin', tmp_db)
    r = c.post('/users/2/edit', data={
        'display_name': 'Louis', 'role': 'staff', 'is_active': 'on',
        'employee_id': str(cur_emp[0]) if cur_emp else '',
        'default_cashbook_account_id': str(acct_id),
    })
    assert r.status_code in (302, 200)

    conn = sqlite3.connect(tmp_db)
    try:
        got = conn.execute(
            "SELECT default_cashbook_account_id FROM users WHERE id=2"
        ).fetchone()[0]
    finally:
        conn.close()
    assert got == acct_id


def test_user_edit_clears_default_cashbook_account(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        # preserve whatever employee is currently linked to user 1 (admin) —
        # this test is only about default_cashbook_account_id.
        cur_emp = conn.execute("SELECT id FROM employees WHERE user_id=1").fetchone()
    finally:
        conn.close()

    c = _client_as('admin', tmp_db)
    # admin (id 1) is seeded with account 392 by mig 126 — clearing the
    # dropdown ("— ไม่ตั้ง —" -> empty string) must NULL it out.
    r = c.post('/users/1/edit', data={
        'display_name': 'Put', 'role': 'admin', 'is_active': 'on',
        'employee_id': str(cur_emp[0]) if cur_emp else '',
        'default_cashbook_account_id': '',
    })
    assert r.status_code in (302, 200)

    conn = sqlite3.connect(tmp_db)
    try:
        got = conn.execute(
            "SELECT default_cashbook_account_id FROM users WHERE id=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert got is None


def test_user_list_renders_cashbook_dropdown(tmp_db):
    database.init_db()
    c = _client_as('admin', tmp_db)
    html = c.get('/users').get_data(as_text=True)
    assert 'name="default_cashbook_account_id"' in html
    assert '— ไม่ตั้ง —' in html
