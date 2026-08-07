"""Creating an employee and seeding their first salary is ONE transaction.

`employee_new` called `create_employee()` (which commits) and only then parsed
`float(initial_salary)` and called `add_salary_history()` (which commits
again). So an unparseable salary — or any error in the second step — left a
real employee row behind while the page said "ไม่สามารถบันทึก". Retrying then
collided on the UNIQUE emp_code, and the half-made employee could enter payroll
resolving to a zero salary (`_build_item`: `rate = ... if sal else 0.0`).

Both halves now go in one connection, one transaction, with the salary parsed
before anything is written.
"""
import os
import sqlite3

import pytest

import hr_queries as hrq


# ── helpers ──────────────────────────────────────────────────────────────────

def _free_emp_code(conn):
    """An EMP code whose numeric id is unused.

    create_employee sets `id` explicitly from the code's numeric suffix (the
    Phase-2 id==emp_code invariant), so the code is only usable if that ID is
    free too — taking MAX(id)+GAP guarantees both.

    GAP is 5 rather than 1 for the same reason as
    test_hr_phase2_renumber.py::test_create_employee_sets_id_from_emp_code:
    autoincrement also hands out MAX(id)+1, so `emp_id == expected_id` would
    hold whether or not the explicit-id path ran.
    """
    n = conn.execute("SELECT COALESCE(MAX(id), 0) + 5 FROM employees").fetchone()[0]
    code = "EMP%03d" % n
    assert conn.execute(
        "SELECT 1 FROM employees WHERE emp_code=?", (code,)).fetchone() is None
    return code, n


def _counts(conn, emp_code):
    emp = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE emp_code=?", (emp_code,)
    ).fetchone()[0]
    sal = conn.execute(
        """SELECT COUNT(*) FROM employee_salary_history
            WHERE employee_id IN (SELECT id FROM employees WHERE emp_code=?)""",
        (emp_code,),
    ).fetchone()[0]
    return emp, sal


def _payload(emp_code):
    return {
        "emp_code": emp_code,
        "full_name": "ทดสอบ อะตอมมิก",
        "company_id": 1,
        "start_date": "2026-03-01",
    }


def _admin_client():
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


# ── engine ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_salary", ["abc", "0", "-100", "0.0"])
def test_bad_salary_creates_neither_row(tmp_db_conn, bad_salary):
    """A salary that cannot be a real wage must be refused BEFORE the employee
    row is written — the whole point is that no half-made employee survives.

    (Blank is NOT in this list: it legitimately means 'no initial salary' —
    see test_blank_salary_still_creates_the_employee_alone.)
    """
    code, _ = _free_emp_code(tmp_db_conn)

    with pytest.raises(ValueError):
        hrq.create_employee_with_initial_salary(
            _payload(code), bad_salary, "2026-03-01", conn=tmp_db_conn)

    assert _counts(tmp_db_conn, code) == (0, 0)


def test_second_step_failure_rolls_back_the_employee(tmp_db_conn, monkeypatch):
    """The original defect: the employee was already committed when the salary
    step blew up. Any second-step DB error must now take the employee with it."""
    code, _ = _free_emp_code(tmp_db_conn)

    def _boom(*a, **kw):
        raise sqlite3.IntegrityError("simulated second-step failure")

    monkeypatch.setattr(hrq, "_insert_salary_history", _boom)

    with pytest.raises(sqlite3.IntegrityError):
        hrq.create_employee_with_initial_salary(
            _payload(code), "15000", "2026-03-01", conn=tmp_db_conn)

    assert _counts(tmp_db_conn, code) == (0, 0)


def test_valid_submission_creates_both_rows(tmp_db_conn):
    code, expected_id = _free_emp_code(tmp_db_conn)

    emp_id = hrq.create_employee_with_initial_salary(
        _payload(code), "15000", "2026-03-01", conn=tmp_db_conn)

    assert emp_id == expected_id
    assert _counts(tmp_db_conn, code) == (1, 1)
    row = tmp_db_conn.execute(
        "SELECT monthly_salary, effective_date, reason"
        "  FROM employee_salary_history WHERE employee_id=?", (emp_id,)
    ).fetchone()
    assert row["monthly_salary"] == 15000.0
    assert row["effective_date"] == "2026-03-01"
    assert row["reason"] == "initial"


def test_blank_salary_still_creates_the_employee_alone(tmp_db_conn):
    """Seeding a salary is optional — a blank field must keep meaning
    'no initial salary row', not 'reject the employee'."""
    code, _ = _free_emp_code(tmp_db_conn)

    emp_id = hrq.create_employee_with_initial_salary(
        _payload(code), "", "2026-03-01", conn=tmp_db_conn)

    assert emp_id is not None
    assert _counts(tmp_db_conn, code) == (1, 0)


def test_retry_after_a_rejected_salary_succeeds(tmp_db_conn):
    """The user-visible consequence: the form said saving failed, so the admin
    fixed the salary and submitted again — and hit 'UNIQUE constraint failed:
    employees.emp_code' on a row they were told did not exist."""
    code, _ = _free_emp_code(tmp_db_conn)

    with pytest.raises(ValueError):
        hrq.create_employee_with_initial_salary(
            _payload(code), "-100", "2026-03-01", conn=tmp_db_conn)

    emp_id = hrq.create_employee_with_initial_salary(
        _payload(code), "15000", "2026-03-01", conn=tmp_db_conn)

    assert emp_id is not None
    assert _counts(tmp_db_conn, code) == (1, 1)


# ── route ────────────────────────────────────────────────────────────────────

def test_route_rejects_non_positive_salary_and_writes_nothing(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    code, _ = _free_emp_code(conn)
    conn.close()

    resp = _admin_client().post('/hr/employees/new', data={
        **_payload(code), 'initial_salary': '0',
    })

    assert resp.status_code == 200, "re-renders the form with the error"
    conn = sqlite3.connect(tmp_db)
    try:
        assert _counts(conn, code) == (0, 0)
    finally:
        conn.close()


def test_route_rejects_unparseable_salary_and_writes_nothing(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    code, _ = _free_emp_code(conn)
    conn.close()

    _admin_client().post('/hr/employees/new', data={
        **_payload(code), 'initial_salary': 'ห้าหมื่น',
    })

    conn = sqlite3.connect(tmp_db)
    try:
        assert _counts(conn, code) == (0, 0)
    finally:
        conn.close()


def test_route_creates_both_rows_on_a_valid_submission(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    code, expected_id = _free_emp_code(conn)
    conn.close()

    resp = _admin_client().post('/hr/employees/new', data={
        **_payload(code), 'initial_salary': '15000',
    })

    assert resp.status_code == 302
    assert resp.headers['Location'].endswith(f'/hr/employees/{expected_id}')
    conn = sqlite3.connect(tmp_db)
    try:
        assert _counts(conn, code) == (1, 1)
    finally:
        conn.close()
