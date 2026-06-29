"""/users page: employee↔account linking + all-5-roles rendering.

Design (grilled 2026-06-29): `/users` is the SOLE editor of the employee↔login
link (HR shows it read-only). The link column stays on `employees.user_id`; the
page writes it. The 1:1 rule (an employee has at most one login) is enforced by
`get_linkable_employees` (selection) + a `WHERE user_id IS NULL` guard (integrity).
"""
import os
import sqlite3

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

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


def _emp_user(db, emp_id):
    return sqlite3.connect(db).execute(
        "SELECT user_id FROM employees WHERE id=?", (emp_id,)
    ).fetchone()[0]


def _user_id(db, username):
    row = sqlite3.connect(db).execute(
        "SELECT id FROM users WHERE username=?", (username,)
    ).fetchone()
    return row[0] if row else None


# Live-DB facts the fixture copies (verified 2026-06-29):
#   EMP004 (id 4, นิคเนม หลุย) → user 2 (l, staff)
#   EMP005 (id 5, บอล) → ACTIVE + unlinked (the only linkable employee)
#   EMP006 (id 6, ริน) → is_active=0, so NOT offered for linking
#   user 10 (mamaput) → shareholder; user 9 (a) → EMP007


# ── 1:1 selection helper ──────────────────────────────────────────────────────

def test_get_linkable_employees_excludes_already_linked(tmp_db):
    import hr_queries as hrq
    ids = [e["id"] for e in hrq.get_linkable_employees()]
    assert 4 not in ids, "EMP004 is linked to user 2 — must not be offered"
    assert 5 in ids, "active+unlinked employee must be offered"
    assert 6 not in ids, "inactive employee (EMP006) must not be offered"


def test_get_linkable_employees_includes_current(tmp_db):
    import hr_queries as hrq
    # Editing user 2 (linked to EMP004): EMP004 must appear as the current pick.
    ids = [e["id"] for e in hrq.get_linkable_employees(user_id=2)]
    assert 4 in ids, "the user's currently-linked employee must be included"
    assert 5 in ids, "unlinked employees still offered alongside the current one"


# ── create: attach an employee at account creation ────────────────────────────

def test_user_new_links_employee(tmp_db):
    c = _client_as('admin', tmp_db)
    r = c.post('/users/new', data={
        'username': 'newacct', 'display_name': 'บอล', 'role': 'general',
        'password': 'secret-pw-123', 'employee_id': '5',
    })
    assert r.status_code in (302, 200)
    uid = _user_id(tmp_db, 'newacct')
    assert uid is not None, "user must be created"
    assert _emp_user(tmp_db, 5) == uid, "EMP005 must now point to the new account"


def test_user_new_without_employee_leaves_unlinked(tmp_db):
    c = _client_as('admin', tmp_db)
    c.post('/users/new', data={
        'username': 'sysacct', 'display_name': 'ระบบ', 'role': 'staff',
        'password': 'secret-pw-123', 'employee_id': '',
    })
    assert _user_id(tmp_db, 'sysacct') is not None
    # no employee got hijacked
    assert _emp_user(tmp_db, 5) is None


def test_user_new_cannot_steal_taken_employee(tmp_db):
    """Forged employee_id pointing at an already-linked employee must NOT steal it."""
    c = _client_as('admin', tmp_db)
    c.post('/users/new', data={
        'username': 'thief', 'display_name': 'x', 'role': 'staff',
        'password': 'secret-pw-123', 'employee_id': '4',  # EMP004 → user 2
    })
    assert _emp_user(tmp_db, 4) == 2, "EMP004 must stay linked to user 2"


# ── edit: link later / change / clear ─────────────────────────────────────────

def test_user_edit_links_then_unlinks(tmp_db):
    c = _client_as('admin', tmp_db)
    # user 8 (teststaff) is unlinked — link it to EMP005 (บอล, active+free)
    c.post('/users/8/edit', data={
        'display_name': 'Test Staff', 'role': 'staff',
        'is_active': 'on', 'employee_id': '5',
    })
    assert _emp_user(tmp_db, 5) == 8, "EMP005 must now point to user 8"
    # clear the link
    c.post('/users/8/edit', data={
        'display_name': 'Test Staff', 'role': 'staff',
        'is_active': 'on', 'employee_id': '',
    })
    assert _emp_user(tmp_db, 5) is None, "clearing the picker must unlink"


def test_user_edit_cannot_steal_taken_employee(tmp_db):
    c = _client_as('admin', tmp_db)
    # user 8 tries to grab EMP004 (linked to user 2) — must be refused
    c.post('/users/8/edit', data={
        'display_name': 'Test Staff', 'role': 'staff',
        'is_active': 'on', 'employee_id': '4',
    })
    assert _emp_user(tmp_db, 4) == 2, "EMP004 must remain with user 2"


# ── delete: the latent FK-500 fix ─────────────────────────────────────────────

def test_delete_linked_account_unlinks_then_deletes(tmp_db):
    """Deleting a linked account must NULL the employee link and not 500."""
    c = _client_as('admin', tmp_db)
    # user 9 (a) is linked to EMP007 (id 7)
    assert _emp_user(tmp_db, 7) == 9
    r = c.post('/users/9/delete')
    assert r.status_code in (302, 200), "must not crash with a FK error"
    assert _user_id(tmp_db, 'a') is None, "account must be deleted"
    assert _emp_user(tmp_db, 7) is None, "employee link must be cleared, employee kept"
    # employee row itself survives
    assert sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM employees WHERE id=7"
    ).fetchone()[0] == 1


# ── rendering: employee column + all-5 role labels + picker ───────────────────

def test_user_list_renders_employee_picker_and_roles(tmp_db):
    c = _client_as('admin', tmp_db)
    html = c.get('/users').get_data(as_text=True)
    assert 'name="employee_id"' in html, "the employee picker must be present"
    assert 'หลุย' in html, "a linked employee's nickname must show on the card (#1)"
    assert 'ไม่ผูก' in html, "unlinked accounts must show a placeholder"
    # all five Thai role labels appear (badge + summary), incl. the two newer roles
    for label in ('ผู้ดูแลระบบ', 'ผู้จัดการ', 'พนักงานออฟฟิศ', 'ผู้ถือหุ้น', 'พนักงานทั่วไป'):
        assert label in html, f"role label {label} missing (#3/#4)"
