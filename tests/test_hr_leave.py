"""TDD — HR leave balance / entitlement logic (`inventory_app/hr.py`).

Fixture: `tmp_db_conn` (copy of live DB; carries migration 054 HR schema +
seeds). `empty_db` is broken at mig 014 per project memory — not used.

Covered:
  - leave_balance per type: entitlement / used / remaining / over
  - entitlement override row beats leave_types.default_quota_days
  - ANNUAL eligibility: <1yr service at year-end → entitlement 0;
    >=1yr → default 6 (or override)
  - used = SUM(days) of APPROVED leave_requests in the calendar year only
    (pending/rejected/cancelled excluded; other years excluded)
"""
import sqlite3

import pytest

import hr


def _leave_type_id(conn, code):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _mk_employee(conn, emp_code, start_date):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'M', 1, ?, 90, 1, 0, 1)""",
        (emp_code, emp_code, start_date),
    )
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, ?, 13000, 'initial')""",
        (eid, start_date),
    )
    conn.commit()
    return eid


def _add_leave(conn, employee_id, code, start, end, days, status='approved'):
    conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (employee_id, _leave_type_id(conn, code), start, end, days, status),
    )
    conn.commit()


# ── default quota / used / remaining / over ──────────────────────────────────

def test_sick_balance_basic(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'L_SICK', '2024-01-01')
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-02-01', '2026-02-04', 4)
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    sick = bal['SICK']
    assert sick['entitlement'] == 30
    assert sick['used'] == 4
    assert sick['remaining'] == 26
    assert sick['over'] == 0


def test_over_quota_reported(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'L_OVER', '2024-01-01')
    _add_leave(tmp_db_conn, eid, 'PERSONAL', '2026-03-01', '2026-03-05', 5)
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    p = bal['PERSONAL']
    assert p['entitlement'] == 3
    assert p['used'] == 5
    assert p['remaining'] == 0          # clamped at 0, not negative
    assert p['over'] == 2


def test_only_approved_in_year_counted(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'L_FILTER', '2024-01-01')
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-01-10', '2026-01-11', 2)         # counts
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-02-01', '2026-02-01', 1,
               status='pending')                                                # excluded
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-02-05', '2026-02-05', 1,
               status='rejected')                                               # excluded
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-02-08', '2026-02-08', 1,
               status='cancelled')                                              # excluded
    _add_leave(tmp_db_conn, eid, 'SICK', '2025-12-30', '2025-12-31', 2)         # other year
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['SICK']['used'] == 2


# ── entitlement override beats default ───────────────────────────────────────

def test_entitlement_override(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'L_OVR', '2024-01-01')
    tmp_db_conn.execute(
        """INSERT INTO employee_leave_entitlements
             (employee_id, leave_type_id, year, quota_days)
           VALUES (?, ?, 2026, 10)""",
        (eid, _leave_type_id(tmp_db_conn, 'PERSONAL')),
    )
    tmp_db_conn.commit()
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['PERSONAL']['entitlement'] == 10
    assert bal['PERSONAL']['remaining'] == 10


# ── ANNUAL eligibility (contract 5.3: >=1yr service) ─────────────────────────

def test_annual_zero_when_under_one_year(tmp_db_conn):
    # start mid-2026, year-end 2026-12-31 → < 1yr service → ANNUAL 0
    eid = _mk_employee(tmp_db_conn, 'L_NEW', '2026-06-01')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 0
    assert bal['ANNUAL']['remaining'] == 0


def test_annual_default_when_over_one_year(tmp_db_conn):
    # start 2024 → at 2026-12-31 has >=1yr → ANNUAL default 6
    eid = _mk_employee(tmp_db_conn, 'L_OLD', '2024-01-01')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 6


def test_annual_override_applies_even_under_one_year(tmp_db_conn):
    # explicit override row wins over the after_1yr gate
    eid = _mk_employee(tmp_db_conn, 'L_NEWOVR', '2026-06-01')
    tmp_db_conn.execute(
        """INSERT INTO employee_leave_entitlements
             (employee_id, leave_type_id, year, quota_days)
           VALUES (?, ?, 2026, 3)""",
        (eid, _leave_type_id(tmp_db_conn, 'ANNUAL')),
    )
    tmp_db_conn.commit()
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 3


# ── seeded employees sanity ──────────────────────────────────────────────────

def test_seeded_emp002_annual_under_one_year(tmp_db_conn):
    # วิภา started 2026-04-01 → for year 2026 < 1yr → ANNUAL entitlement 0
    eid = tmp_db_conn.execute(
        "SELECT id FROM employees WHERE emp_code='EMP002'"
    ).fetchone()['id']
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 0
