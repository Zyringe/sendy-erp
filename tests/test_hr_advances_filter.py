"""Filters + default views for HR advances & leave.

Change under test:
  - get_salary_advances(employee_id, status) filters by employee and by
    รอหัก (pending, deducted_in_run_id IS NULL) / ถูกหักแล้ว (deducted).
  - /hr/advances DEFAULTS to รอหัก (money still owed back); ?status=all shows
    every status.
  - /hr/leave DEFAULTS to the current month; ?month= (blank) shows all months.

Fixture `tmp_db_conn_hr_clean` wipes salary_advances / leave_requests /
payroll_runs from the copied live DB so counts are deterministic, while
preserving the seeded employees + leave_types.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from datetime import date

import hr_queries as hrq


def _two_emps(conn):
    rows = conn.execute(
        "SELECT id FROM employees WHERE is_active=1 ORDER BY id LIMIT 2"
    ).fetchall()
    return rows[0][0], rows[1][0]


def _mk_run(conn, ym):
    return conn.execute(
        "INSERT INTO payroll_runs (year_month, company_id, status, created_by, created_at) "
        "VALUES (?, 1, 'finalized', 'test', datetime('now','localtime'))",
        (ym,),
    ).lastrowid


def _mk_advance(conn, emp_id, advance_date, amount, deducted_run=None):
    return conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, deducted_in_run_id) "
        "VALUES (?, ?, ?, ?)",
        (emp_id, advance_date, amount, deducted_run),
    ).lastrowid


def _client(role='admin', user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    cl = a.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return cl


# ── query-level ──────────────────────────────────────────────────────────────

def test_get_salary_advances_status_and_employee_filter(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, b = _two_emps(c)
    run = _mk_run(c, '2026-05')
    pend = _mk_advance(c, a, '2026-07-01', 111.0)          # pending, emp A
    dedu = _mk_advance(c, b, '2026-05-02', 222.0, run)     # deducted, emp B
    c.commit()

    all_ids = {r['id'] for r in hrq.get_salary_advances(conn=c)}
    assert all_ids == {pend, dedu}

    pending_ids = {r['id'] for r in hrq.get_salary_advances(status='pending', conn=c)}
    assert pending_ids == {pend}

    deducted_ids = {r['id'] for r in hrq.get_salary_advances(status='deducted', conn=c)}
    assert deducted_ids == {dedu}

    a_ids = {r['id'] for r in hrq.get_salary_advances(employee_id=a, conn=c)}
    assert a_ids == {pend}

    # combined AND: emp A's only advance is pending → A+deducted = empty
    assert hrq.get_salary_advances(employee_id=a, status='deducted', conn=c) == []


# ── route defaults ───────────────────────────────────────────────────────────

def test_advances_route_defaults_to_pending(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, b = _two_emps(c)
    run = _mk_run(c, '2026-05')
    _mk_advance(c, a, '2026-07-01', 40197.0)          # pending, unique amount
    _mk_advance(c, b, '2026-05-02', 40286.0, run)     # deducted, unique amount
    c.commit()

    cl = _client()
    html = cl.get('/hr/advances').get_data(as_text=True)
    assert '40,197' in html, "pending advance must show in the default view"
    assert '40,286' not in html, "deducted advance must be hidden in the default (รอหัก) view"

    html_all = cl.get('/hr/advances?status=all').get_data(as_text=True)
    assert '40,197' in html_all and '40,286' in html_all, "?status=all shows every status"


def test_leave_route_defaults_to_current_month(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    a, _ = _two_emps(c)
    sick = c.execute("SELECT id FROM leave_types WHERE code='SICK'").fetchone()[0]
    cur = date.today().strftime('%Y-%m')
    c.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date, end_date, days, status, reason) "
        "VALUES (?, ?, ?, ?, 1, 'approved', 'CURMONTHMARK')",
        (a, sick, cur + '-15', cur + '-15'),
    )
    c.execute(
        "INSERT INTO leave_requests (employee_id, leave_type_id, start_date, end_date, days, status, reason) "
        "VALUES (?, ?, '2020-01-10', '2020-01-10', 1, 'approved', 'OLDMONTHMARK')",
        (a, sick),
    )
    c.commit()

    cl = _client()
    html = cl.get('/hr/leave').get_data(as_text=True)
    assert 'CURMONTHMARK' in html, "current-month leave must show by default"
    assert 'OLDMONTHMARK' not in html, "past-month leave must be hidden by the current-month default"

    html_all = cl.get('/hr/leave?month=').get_data(as_text=True)
    assert 'CURMONTHMARK' in html_all and 'OLDMONTHMARK' in html_all, "?month= (blank) shows all months"


# ── account name falls back to the code ─────────────────────────────────────

def test_advance_account_name_falls_back_to_the_account_code(tmp_db_conn_hr_clean):
    """`cashbook_accounts.display_name` is NULL for every account on prod, and
    the /admin form has no field that writes it — so this query, the only
    consumer that read it WITHOUT a fallback, rendered '—' in the โอนจากบัญชี
    column for all 28 advances (verified in the browser 2026-08-05).

    Everywhere else already falls back: COALESCE(ca.display_name, ca.code) in
    get_employees, `{{ acct.display_name or acct.code }}` in the cashbook
    templates. Both directions are asserted so the test cannot pass by always
    returning one of them.
    """
    c = tmp_db_conn_hr_clean
    emp, _ = _two_emps(c)
    acct = c.execute(
        "SELECT id, code FROM cashbook_accounts ORDER BY id LIMIT 1").fetchone()
    assert acct is not None, "fixture must have an account, or this pins nothing"
    c.execute("UPDATE cashbook_accounts SET display_name=NULL WHERE id=?",
              (acct["id"],))
    adv_id = c.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id)"
        " VALUES (?, '2026-10-05', 500, ?)", (emp, acct["id"])).lastrowid
    c.commit()

    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["account_name"] == acct["code"]

    # ...and a real display_name still wins over the code
    c.execute("UPDATE cashbook_accounts SET display_name='ชื่อจริงของบัญชี' WHERE id=?",
              (acct["id"],))
    c.commit()
    row = next(r for r in hrq.get_salary_advances(conn=c) if r["id"] == adv_id)
    assert row["account_name"] == 'ชื่อจริงของบัญชี'
