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
