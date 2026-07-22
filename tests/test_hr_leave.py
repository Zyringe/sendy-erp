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


def _set_emp(conn, eid, probation_end=None, probation_days=None,
             is_active=None, start_date=None):
    """Override employee fields _mk_employee doesn't expose (partial update)."""
    sets, vals = [], []
    if probation_end is not None:
        sets.append("probation_end_date=?"); vals.append(probation_end)
    if probation_days is not None:
        sets.append("probation_days=?"); vals.append(probation_days)
    if is_active is not None:
        sets.append("is_active=?"); vals.append(is_active)
    if start_date is not None:
        sets.append("start_date=?"); vals.append(start_date)
    vals.append(eid)
    conn.execute(f"UPDATE employees SET {', '.join(sets)} WHERE id=?", vals)
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
    # PERSONAL quota is 6 (Put 2026-07-22) → 7 used → 1 over
    eid = _mk_employee(tmp_db_conn, 'L_OVER', '2024-01-01')
    _add_leave(tmp_db_conn, eid, 'PERSONAL', '2026-03-01', '2026-03-07', 7)
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    p = bal['PERSONAL']
    assert p['entitlement'] == 6
    assert p['used'] == 7
    assert p['remaining'] == 0          # clamped at 0, not negative
    assert p['over'] == 1


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


# ── ANNUAL prorate-after-probation (Put 2026-07-22: replaces after_1yr) ──────
# entitlement = round_half(6 × days_from_hire_in_year / days_in_year), available
# once probation ends within the year (Model P, evaluated at year-end). Prorate
# base = hire date. Later full calendar years → 6.

def test_annual_prorate_when_hired_this_year(tmp_db_conn):
    # start 2026-06-01, probation_end NULL → derive from +90d ≈ 2026-08-30
    # (≤ year-end) → 6×214/365 = 3.52 → nearest 0.5 = 3.5 (was 0 under after_1yr)
    eid = _mk_employee(tmp_db_conn, 'L_NEW', '2026-06-01')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 3.5
    assert bal['ANNUAL']['remaining'] == 3.5


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

def test_seeded_emp002_annual_prorated(tmp_db_conn):
    # EMP002 = เซียง, started 2026-02-01 → prorate 6×334/365 = 5.49 → 5.5
    # (was 0 under after_1yr; probation ends within 2026 either way)
    eid = tmp_db_conn.execute(
        "SELECT id FROM employees WHERE emp_code='EMP002'"
    ).fetchone()['id']
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 5.5


# ── ANNUAL proration — explicit cases ────────────────────────────────────────

def test_annual_prorate_first_year_from_hire(tmp_db_conn):
    # hire 2026-04-01 → 6×275/365 = 4.52 → 4.5
    eid = _mk_employee(tmp_db_conn, 'L_PR1', '2026-04-01')
    _set_emp(tmp_db_conn, eid, probation_end='2026-06-29')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 4.5


def test_annual_prorate_rounds_to_nearest_half(tmp_db_conn):
    # hire 2026-04-17 → 6×259/365 = 4.2575 → nearest 0.5 = 4.5
    eid = _mk_employee(tmp_db_conn, 'L_PR3', '2026-04-17')
    _set_emp(tmp_db_conn, eid, probation_end='2026-07-16')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 4.5


def test_annual_full_six_in_later_full_year(tmp_db_conn):
    # hired 2026-04-01; for 2027 (worked full calendar year) → 6 exactly
    eid = _mk_employee(tmp_db_conn, 'L_PR4', '2026-04-01')
    _set_emp(tmp_db_conn, eid, probation_end='2026-06-29')
    bal = hr.leave_balance(eid, 2027, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 6


def test_annual_zero_when_no_start_date(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'L_PR5', '2026-04-01')
    tmp_db_conn.execute("UPDATE employees SET start_date=NULL WHERE id=?", (eid,))
    tmp_db_conn.commit()
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 0


def test_annual_zero_when_probation_ends_after_yearend(tmp_db_conn):
    # hired late; probation runs into next year → Model P gate: 0 for hire year
    eid = _mk_employee(tmp_db_conn, 'L_PR6', '2026-11-01')
    _set_emp(tmp_db_conn, eid, probation_end='2027-01-30')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 0


def test_annual_prorate_derives_probation_end_when_null(tmp_db_conn):
    # probation_end_date blank → derive from start + probation_days (90);
    # 2026-04-01 + 90d ≈ 2026-06-30 ≤ year-end → prorated 4.5, not 0
    eid = _mk_employee(tmp_db_conn, 'L_PR7', '2026-04-01')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 4.5


def test_annual_override_still_wins_over_prorate(tmp_db_conn):
    # explicit override row beats the prorate computation
    eid = _mk_employee(tmp_db_conn, 'L_PROVR', '2026-04-01')
    tmp_db_conn.execute(
        """INSERT INTO employee_leave_entitlements
             (employee_id, leave_type_id, year, quota_days)
           VALUES (?, ?, 2026, 2)""",
        (eid, _leave_type_id(tmp_db_conn, 'ANNUAL')),
    )
    tmp_db_conn.commit()
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['ANNUAL']['entitlement'] == 2


def test_personal_quota_is_six(tmp_db_conn):
    # ลากิจ 3 → 6 (Put 2026-07-22), from day one, no gate
    eid = _mk_employee(tmp_db_conn, 'L_PERS6', '2026-06-01')
    bal = hr.leave_balance(eid, 2026, conn=tmp_db_conn)
    assert bal['PERSONAL']['entitlement'] == 6
