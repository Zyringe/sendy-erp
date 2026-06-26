"""Phase 5 — Self-service leave management.

Task 5.1: Migration 118 — add approved_by + approved_at columns to leave_requests.
Task 5.2: bp_me blueprint + /me/leave read view + resolver.
"""
import os
import sqlite3

import pytest


# ── Task 5.1 ─────────────────────────────────────────────────────────────────

def test_leave_requests_has_approval_columns(tmp_db):
    """leave_requests must have approved_by and approved_at TEXT columns."""
    cols = {r[1] for r in sqlite3.connect(tmp_db).execute("PRAGMA table_info(leave_requests)")}
    assert {'approved_by', 'approved_at'} <= cols


# ── Task 5.2 helpers ──────────────────────────────────────────────────────────

def _client(role, user_id, db):
    """Test client pre-logged-in as given role and user_id."""
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


# ── Task 5.2 tests ────────────────────────────────────────────────────────────

def test_my_leave_shows_only_own(tmp_db):
    """GET /me/leave returns only the logged-in employee's own leave rows."""
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'")
    c.commit()
    me = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    other = c.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    c.execute(
        "INSERT INTO leave_requests"
        "(employee_id,leave_type_id,start_date,end_date,days,reason,status,created_by)"
        " VALUES(?,1,'2026-07-01','2026-07-01',1,'MINE','approved','x')", (me,)
    )
    c.execute(
        "INSERT INTO leave_requests"
        "(employee_id,leave_type_id,start_date,end_date,days,reason,status,created_by)"
        " VALUES(?,1,'2026-07-02','2026-07-02',1,'THEIRS','approved','x')", (other,)
    )
    c.commit()
    cl = _client(role='staff', user_id=2, db=tmp_db)
    html = cl.get('/me/leave').get_data(as_text=True)
    assert 'MINE' in html and 'THEIRS' not in html


# ── Task 5.3 — self-scoped writes (the security core) ─────────────────────────

def test_submit_creates_pending_for_self(tmp_db):
    """POST /me/leave/new creates a pending row owned by the session employee.

    employee_id must come from the session (EMP004 ↔ user_id=2), never the form.
    """
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'")
    c.commit()
    me_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    c.close()
    cl = _client('staff', 2, tmp_db)
    cl.post('/me/leave/new', data={
        'leave_type_id': '1', 'start_date': '2026-08-01',
        'end_date': '2026-08-01', 'days': '1', 'reason': 'ธุระ',
    })
    c2 = sqlite3.connect(tmp_db)
    r = c2.execute(
        "SELECT employee_id, status FROM leave_requests WHERE reason='ธุระ'"
    ).fetchone()
    c2.close()
    assert r == (me_id, 'pending')


def test_cannot_cancel_another_employees_leave(tmp_db):
    """Cross-employee cancel → hard 403 and the row is unchanged."""
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'")
    c.commit()
    other_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests"
        "(employee_id, leave_type_id, start_date, end_date, days, status, created_by) "
        "VALUES(?,1,'2026-09-01','2026-09-01',1,'pending','x') RETURNING id", (other_id,)
    ).fetchone()[0]
    c.commit()
    c.close()
    cl = _client('staff', 2, tmp_db)   # user 2 = EMP004, NOT the owner of rid
    r = cl.post(f'/me/leave/{rid}/cancel', data={})
    assert r.status_code == 403
    c3 = sqlite3.connect(tmp_db)
    status = c3.execute(
        "SELECT status FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()[0]
    c3.close()
    assert status == 'pending'


def test_cannot_edit_another_employees_leave(tmp_db):
    """Cross-employee edit → hard 403 and the row is unchanged."""
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'")
    c.commit()
    other_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests"
        "(employee_id, leave_type_id, start_date, end_date, days, reason, status, created_by) "
        "VALUES(?,1,'2026-09-01','2026-09-01',1,'ORIGINAL','pending','x') RETURNING id",
        (other_id,)
    ).fetchone()[0]
    c.commit()
    c.close()
    cl = _client('staff', 2, tmp_db)   # user 2 = EMP004, NOT the owner of rid
    r = cl.post(f'/me/leave/{rid}/edit', data={
        'leave_type_id': '1', 'start_date': '2099-01-01',
        'end_date': '2099-01-01', 'days': '9', 'reason': 'HACKED',
    })
    assert r.status_code == 403
    c3 = sqlite3.connect(tmp_db)
    row = c3.execute(
        "SELECT employee_id, reason, status FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()
    c3.close()
    assert row == (other_id, 'ORIGINAL', 'pending')


# ── Task 5.4 — manager approval workflow ─────────────────────────────────────

def test_manager_can_approve_leave(tmp_db):
    """Manager POSTing /hr/leave/<rid>/approve sets status='approved' and stamps approved_by."""
    import sqlite3; c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit()
    emp_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests(employee_id,leave_type_id,start_date,end_date,days,status,created_by) "
        "VALUES(?,1,'2026-08-01','2026-08-01',1,'pending','x') RETURNING id", (emp_id,)
    ).fetchone()[0]; c.commit(); c.close()
    cl = _client('manager', 99, tmp_db)   # manager (no employee link needed)
    r = cl.post(f'/hr/leave/{rid}/approve', data={})
    assert r.status_code in (200, 302)
    row = sqlite3.connect(tmp_db).execute(
        "SELECT status, approved_by FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == 'approved'
    assert row[1] is not None   # approved_by was stamped


def test_staff_cannot_approve_leave(tmp_db):
    """Staff POSTing /hr/leave/<rid>/approve is blocked (302 or 403) and leave remains pending."""
    import sqlite3; c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit()
    emp_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests(employee_id,leave_type_id,start_date,end_date,days,status,created_by) "
        "VALUES(?,1,'2026-08-01','2026-08-01',1,'pending','x') RETURNING id", (emp_id,)
    ).fetchone()[0]; c.commit(); c.close()
    cl = _client('staff', 2, tmp_db)
    r = cl.post(f'/hr/leave/{rid}/approve', data={})
    assert r.status_code in (302, 403)
    assert sqlite3.connect(tmp_db).execute(
        "SELECT status FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()[0] == 'pending'   # unchanged


def test_manager_can_reject_leave_with_stamp(tmp_db):
    """Manager POSTing /hr/leave/<rid>/reject sets status='rejected' and stamps approved_by + approved_at."""
    import sqlite3; c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit()
    emp_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests(employee_id,leave_type_id,start_date,end_date,days,status,created_by) "
        "VALUES(?,1,'2026-08-02','2026-08-02',1,'pending','x') RETURNING id", (emp_id,)
    ).fetchone()[0]; c.commit(); c.close()
    cl = _client('manager', 99, tmp_db)
    cl.post(f'/hr/leave/{rid}/reject', data={})
    row = sqlite3.connect(tmp_db).execute(
        "SELECT status, approved_by, approved_at FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == 'rejected'
    assert row[1] is not None   # approved_by was stamped
    assert row[2] is not None   # approved_at was stamped


# ── Task 5.5 — audit_log attribution ─────────────────────────────────────────

def test_leave_changes_logged_to_audit(tmp_db):
    """submit + approve each produce an audit_log row naming the correct actor."""
    import sqlite3 as _s
    c = _s.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit()
    lt_id = c.execute("SELECT id FROM leave_types LIMIT 1").fetchone()[0]
    c.close()

    # employee submits — should log action='INSERT' to audit_log
    cl_staff = _client('staff', 2, tmp_db)
    cl_staff.post('/me/leave/new', data={
        'leave_type_id': str(lt_id), 'start_date': '2026-09-01',
        'end_date': '2026-09-01', 'days': '1', 'reason': 'ทดสอบ audit',
    })

    # fetch the new request id
    c2 = _s.connect(tmp_db)
    row = c2.execute(
        "SELECT id FROM leave_requests WHERE reason='ทดสอบ audit'"
    ).fetchone()
    c2.close()
    assert row is not None, "leave request was not created"
    rid = row[0]

    # manager approves — should log action='UPDATE' to audit_log
    cl_mgr = _client('manager', 99, tmp_db)
    cl_mgr.post(f'/hr/leave/{rid}/approve', data={})

    # verify app-side audit rows (user IS NOT NULL — triggers leave user=NULL)
    c3 = _s.connect(tmp_db)
    rows = c3.execute(
        "SELECT action, user FROM audit_log"
        " WHERE table_name='leave_requests' AND row_id=? AND user IS NOT NULL",
        (rid,)
    ).fetchall()
    c3.close()
    actions = {r[0] for r in rows}
    users = {r[1] for r in rows}
    assert len(rows) >= 2, f"Expected >=2 app-side audit rows, got {len(rows)}: {rows}"
    assert None not in users, f"audit_log.user must not be null, got users={users}"
    assert 'INSERT' in actions, f"Expected INSERT action for submit, got {actions}"
    assert 'UPDATE' in actions, f"Expected UPDATE action for approve, got {actions}"


def test_cannot_edit_non_pending_leave(tmp_db):
    """Owner can only self-edit PENDING leave; editing an approved row → 403."""
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'")
    c.commit()
    me_id = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    rid = c.execute(
        "INSERT INTO leave_requests"
        "(employee_id, leave_type_id, start_date, end_date, days, reason, status, created_by) "
        "VALUES(?,1,'2026-10-01','2026-10-01',1,'APPROVED-MINE','approved','x') RETURNING id",
        (me_id,)
    ).fetchone()[0]
    c.commit()
    c.close()
    cl = _client('staff', 2, tmp_db)   # owner, but the row is already approved
    r = cl.post(f'/me/leave/{rid}/cancel', data={})
    assert r.status_code == 403
    c3 = sqlite3.connect(tmp_db)
    status = c3.execute(
        "SELECT status FROM leave_requests WHERE id=?", (rid,)
    ).fetchone()[0]
    c3.close()
    assert status == 'approved'


# ── Task 5.6 — access wiring: general role + mobile nav ──────────────────────

def test_general_can_access_me_leave(tmp_db):
    """general role GET /me/leave must return 200 (endpoint now in _GENERAL_ALLOWED)."""
    import sqlite3; c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit(); c.close()
    cl = _client('general', 2, tmp_db)
    r = cl.get('/me/leave')
    assert r.status_code == 200, f"general GET /me/leave should be 200, got {r.status_code}"


def test_general_can_post_leave(tmp_db):
    """general role POST /me/leave/new must not be blocked (200 or 302)."""
    import sqlite3; c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=2 WHERE emp_code='EMP004'"); c.commit()
    lt_id = c.execute("SELECT id FROM leave_types LIMIT 1").fetchone()[0]
    c.close()
    cl = _client('general', 2, tmp_db)
    r = cl.post('/me/leave/new', data={
        'leave_type_id': str(lt_id), 'start_date': '2026-10-01',
        'end_date': '2026-10-01', 'days': '1', 'reason': 'general test',
    })
    assert r.status_code in (200, 302), f"general POST /me/leave/new should succeed, got {r.status_code}"


def test_general_still_blocked_from_hr(tmp_db):
    """general role must still be denied /hr/ and /products after the leave wiring."""
    cl = _client('general', 99, tmp_db)
    assert cl.get('/hr/').status_code in (302, 403), "general must not reach /hr/"
    assert cl.get('/products').status_code in (302, 403), "general must not reach /products"
