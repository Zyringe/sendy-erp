"""Phase 4 Part A — enablers (Tasks 4.1–4.3).

Task 4.1: generate_run skips employees with on_payroll=0.
Task 4.2: create_employee/update_employee bind on_payroll; get_linkable_users
          excludes already-linked accounts.
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

def test_create_employee_links_user_and_off_payroll(tmp_db):
    """create_employee binds user_id and on_payroll=0 when supplied."""
    import hr_queries as hrq
    eid = hrq.create_employee({
        "emp_code": "EMP800",
        "full_name": "แม่ ทดสอบ",
        "company_id": 1,
        "user_id": 10,
        "on_payroll": "0",
    })
    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_id, on_payroll FROM employees WHERE id=?", (eid,)
    ).fetchone()
    assert row == (10, 0)


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


def test_linkable_users_excludes_already_linked(tmp_db):
    """get_linkable_users must not return users already linked to another employee."""
    import hr_queries as hrq
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=3 WHERE emp_code='EMP002'")
    c.commit(); c.close()
    ids = [u["id"] for u in hrq.get_linkable_users()]
    assert 3 not in ids, "user 3 is linked to EMP002 — must not appear in linkable list"


def test_linkable_users_includes_self(tmp_db):
    """When editing employee X already linked to user Y, Y must appear in the list."""
    import hr_queries as hrq
    c = sqlite3.connect(tmp_db)
    c.execute("UPDATE employees SET user_id=3 WHERE emp_code='EMP002'")
    c.commit(); c.close()
    # When editing EMP002 (id=2), user 3 should appear (it's the current link)
    ids = [u["id"] for u in hrq.get_linkable_users(employee_id=2)]
    assert 3 in ids, "self-linked user must be included when editing the same employee"


def test_employee_form_has_user_link_and_on_payroll(tmp_db):
    """Add-employee form renders the user_id dropdown and on_payroll checkbox."""
    c = _client_as('admin', tmp_db)
    html = c.get('/hr/employees/new').get_data(as_text=True)
    assert 'name="user_id"' in html, "user_id dropdown must be in the add form"
    assert 'name="on_payroll"' in html, "on_payroll field must be in the add form"


# ── Task 4.3 ──────────────────────────────────────────────────────────────────

def test_users_form_offers_new_roles(tmp_db):
    """/users must offer shareholder + general in both role selects."""
    c = _client_as('admin', tmp_db)
    html = c.get('/users').get_data(as_text=True)
    assert 'value="shareholder"' in html
    assert 'value="general"' in html
