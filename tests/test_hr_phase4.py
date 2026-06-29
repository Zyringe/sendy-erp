"""Phase 4 Part A — enablers (Tasks 4.1–4.3).

Task 4.1: generate_run skips employees with on_payroll=0.
Task 4.2: create_employee/update_employee bind on_payroll. (The employee↔login
          link moved to /users on 2026-06-29 — HR no longer binds user_id; HR
          edits must preserve the existing link. See test_users_employee_link.py.)
Task 4.3: /users form offers shareholder + general role options.
"""
import os
import sqlite3

import pytest

import hr


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


# ── Task 4.1 ──────────────────────────────────────────────────────────────────

def test_payroll_excludes_off_payroll_employee(tmp_db_conn):
    """An active employee with on_payroll=0 must NOT appear in a payroll run."""
    conn = tmp_db_conn
    # Insert a shareholder-style employee: active but on_payroll=0
    conn.execute(
        "INSERT INTO employees(emp_code,full_name,company_id,is_active,on_payroll,start_date) "
        "VALUES('EMP777','ผู้ถือหุ้น ทดสอบ',1,1,0,'2026-01-01')"
    )
    conn.commit()
    run = hr.generate_run('2026-06', 1, created_by=1, conn=conn)
    got = conn.execute(
        "SELECT COUNT(*) FROM payroll_items pi "
        "JOIN employees e ON e.id=pi.employee_id "
        "WHERE pi.run_id=? AND e.emp_code='EMP777'",
        (run['id'],),
    ).fetchone()[0]
    assert got == 0, "on_payroll=0 employee must NOT get a payroll item"


def test_payroll_includes_on_payroll_employees(tmp_db_conn):
    """Existing on_payroll=1 employees are unaffected by the new filter."""
    conn = tmp_db_conn
    run = hr.generate_run('2026-06', 1, created_by=1, conn=conn)
    # The live DB has real employees with on_payroll DEFAULT 1 — at least 1 must appear
    got = conn.execute(
        "SELECT COUNT(*) FROM payroll_items pi "
        "JOIN employees e ON e.id=pi.employee_id "
        "WHERE pi.run_id=? AND e.on_payroll=1",
        (run['id'],),
    ).fetchone()[0]
    assert got > 0, "on_payroll=1 employees must still get payroll items"


# ── Task 4.2 ──────────────────────────────────────────────────────────────────

def test_create_employee_ignores_user_id_binds_on_payroll(tmp_db):
    """create_employee no longer binds user_id (the link is owned by /users);
    a passed user_id is ignored. on_payroll still binds."""
    import hr_queries as hrq
    eid = hrq.create_employee({
        "emp_code": "EMP800",
        "full_name": "แม่ ทดสอบ",
        "company_id": 1,
        "user_id": 10,   # must be ignored — link managed on /users
        "on_payroll": "0",
    })
    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_id, on_payroll FROM employees WHERE id=?", (eid,)
    ).fetchone()
    assert row == (None, 0)


def test_update_employee_preserves_user_link(tmp_db):
    """An HR edit must NOT touch the login link — even if user_id is absent or
    forged in the form. (EMP004/id 4 is linked to user 2 in the live DB.)"""
    import hr_queries as hrq
    hrq.update_employee(4, {
        "emp_code": "EMP004", "full_name": "วิภา ขมสันเทียะ",
        "company_id": 1, "user_id": "",   # would have wiped the link under old code
    })
    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_id FROM employees WHERE id=4"
    ).fetchone()
    assert row[0] == 2, "HR edit must leave employees.user_id untouched"


def test_update_employee_sets_on_payroll_zero(tmp_db):
    """update_employee can flip on_payroll to 0 on an existing row."""
    import hr_queries as hrq
    # EMP001 is id=1 after Phase 2 renumber
    hrq.update_employee(1, {
        "emp_code": "EMP001",
        "full_name": "วุฒิพงษ์ ทดสอบ",
        "company_id": 1,
        "on_payroll": "0",
    })
    row = sqlite3.connect(tmp_db).execute(
        "SELECT on_payroll FROM employees WHERE id=1"
    ).fetchone()
    assert row[0] == 0


# Note: the employee↔account link selection rule moved off HR (get_linkable_users
# removed) onto /users (get_linkable_employees). Its tests live in
# tests/test_users_employee_link.py.


def test_employee_form_no_user_link_but_on_payroll(tmp_db):
    """HR add form no longer edits the login link (managed on /users) but keeps
    the on_payroll field, and points to the user page."""
    c = _client_as('admin', tmp_db)
    html = c.get('/hr/employees/new').get_data(as_text=True)
    assert 'name="user_id"' not in html, "HR must not edit the login link anymore"
    assert 'name="on_payroll"' in html, "on_payroll field must remain in the add form"
    assert 'href="/users"' in html, "form should link to the user page"


# ── Task 4.3 ──────────────────────────────────────────────────────────────────

def test_users_form_offers_new_roles(tmp_db):
    """/users must offer shareholder + general in both role selects."""
    c = _client_as('admin', tmp_db)
    html = c.get('/users').get_data(as_text=True)
    assert 'value="shareholder"' in html
    assert 'value="general"' in html
