"""TDD — HR payroll salary-advance deduction (`inventory_app/hr.py`).

เบิกเงินล่วงหน้า (salary_advances, mig 055) feeds payroll as a deduction.

Risky money math → written FIRST, RED, then implement until GREEN.

Fixture: `tmp_db_conn` (copy of live DB; carries mig 054 HR schema + the
new mig 057 `payroll_items.salary_advance_deduction` column — 057 is
applied to the live DB so the copy inherits it). `empty_db` is broken at
mig 014 per project memory — do NOT use it here.

DEDUCTION RULE (documented decision — see hr.py):
  salary_advance_deduction for an employee's payroll month =
      SUM(salary_advances.amount)
        WHERE employee_id = emp
          AND advance_date <= period_end
          AND (deducted_in_run_id IS NULL OR deducted_in_run_id = this_run)
  net_pay = gross - unpaid_leave_deduction - sso_employee
            - other_deductions - salary_advance_deduction
  Draft generation does NOT stamp deducted_in_run_id (re-runnable without
  doubling). finalize_run() stamps it so a later month does not re-deduct.
"""
import sqlite3

import pytest

import hr


# ── helpers ──────────────────────────────────────────────────────────────────

def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=15000.0, sso_enrolled=1, company_id=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'M', ?, ?, 90, ?, 0, 1)""",
        (emp_code, full_name, company_id, start_date, sso_enrolled),
    )
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, ?, ?, 'initial')""",
        (eid, start_date, monthly_salary),
    )
    conn.commit()
    return eid


def _add_advance(conn, employee_id, advance_date, amount):
    conn.execute(
        """INSERT INTO salary_advances
             (employee_id, advance_date, amount, raw_name)
           VALUES (?, ?, ?, 'test')""",
        (employee_id, advance_date, amount),
    )
    conn.commit()


def _item(conn, run_id, employee_id):
    r = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, employee_id),
    ).fetchone()
    assert r is not None, "payroll_items row missing"
    return r


# ── 0. migration 057 column present ──────────────────────────────────────────

def test_migration_057_column_present(tmp_db_conn):
    cols = {r["name"] for r in tmp_db_conn.execute(
        "PRAGMA table_info(payroll_items)"
    ).fetchall()}
    assert "salary_advance_deduction" in cols
    n = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename='057_payroll_salary_advance.sql'"
    ).fetchone()[0]
    assert n == 1


# ── 1. two advances → summed deduction & net ─────────────────────────────────

def test_two_advances_deducted(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_ADV1', 'adv two', '2026-01-01',
                       monthly_salary=15000.0)
    _add_advance(tmp_db_conn, eid, '2026-03-05', 1300.0)
    _add_advance(tmp_db_conn, eid, '2026-03-20', 1300.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['salary_advance_deduction'] == 2600.0
    base = 15000.00
    sso = 750  # 15000*0.05 capped
    expected_net = round(base - 0 - sso - 0 - 2600.0, 2)
    assert it['net_pay'] == expected_net


# ── 2. no advances → 0 & net unchanged (regression guard) ────────────────────

def test_no_advances_zero(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_ADV0', 'adv none', '2026-01-01',
                       monthly_salary=15000.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['salary_advance_deduction'] == 0
    base = 15000.00
    sso = 750
    assert it['net_pay'] == round(base - sso, 2)


# ── 3. advance dated after period_end → not deducted ─────────────────────────

def test_advance_after_period_end_excluded(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_ADV2', 'adv late', '2026-01-01',
                       monthly_salary=15000.0)
    # March run; advance dated in April → must NOT be deducted in March
    _add_advance(tmp_db_conn, eid, '2026-04-02', 999.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['salary_advance_deduction'] == 0


# ── 4. idempotency: re-generate draft does not double / does not stamp ───────

def test_generate_run_idempotent_no_double(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_ADV3', 'adv idem', '2026-01-01',
                       monthly_salary=15000.0)
    _add_advance(tmp_db_conn, eid, '2026-03-05', 1300.0)
    _add_advance(tmp_db_conn, eid, '2026-03-20', 1300.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # still 2600, NOT 5200 (no double-count on re-run)
    assert it['salary_advance_deduction'] == 2600.0
    # draft must NOT stamp deducted_in_run_id
    stamped = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM salary_advances "
        "WHERE employee_id=? AND deducted_in_run_id IS NOT NULL", (eid,)
    ).fetchone()[0]
    assert stamped == 0


# ── 5. finalize_run stamps + next month does not re-deduct + re-finalize ────

def test_finalize_stamps_and_blocks_next_month(tmp_db_conn_hr_clean):
    # Uses tmp_db_conn_hr_clean: payroll_runs/items/salary_advances are
    # wiped from the live-DB copy so generate_run('2026-03'/'2026-04') is
    # guaranteed to create fresh runs (won't silently hit a UNIQUE-constraint
    # collision with a real production run).
    tmp_db_conn = tmp_db_conn_hr_clean
    eid = _mk_employee(tmp_db_conn, 'T_ADV4', 'adv final', '2026-01-01',
                       monthly_salary=15000.0)
    _add_advance(tmp_db_conn, eid, '2026-03-05', 1300.0)
    _add_advance(tmp_db_conn, eid, '2026-03-20', 1300.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['salary_advance_deduction'] == 2600.0

    fin = hr.finalize_run(run['id'], conn=tmp_db_conn)
    assert fin['status'] == 'finalized'
    assert fin['finalized_at'] is not None
    # advances now stamped with this run id
    stamped = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM salary_advances "
        "WHERE employee_id=? AND deducted_in_run_id=?", (eid, run['id'])
    ).fetchone()[0]
    assert stamped == 2

    # next month run for the SAME employee must NOT re-deduct those advances
    run2 = hr.generate_run('2026-04', 1, created_by=1, conn=tmp_db_conn)
    it2 = _item(tmp_db_conn, run2['id'], eid)
    assert it2['salary_advance_deduction'] == 0

    # re-finalize is a no-op (no error, returns row, no re-mark)
    again = hr.finalize_run(run['id'], conn=tmp_db_conn)
    assert again['status'] == 'finalized'
    assert again['finalized_at'] == fin['finalized_at']


# ── 6. update_payroll_item keeps the advance term in net_pay ─────────────────

def test_update_item_preserves_advance_term(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_ADV5', 'adv edit', '2026-01-01',
                       monthly_salary=15000.0)
    _add_advance(tmp_db_conn, eid, '2026-03-05', 1300.0)
    _add_advance(tmp_db_conn, eid, '2026-03-20', 1300.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    hr.update_payroll_item(it['id'], bonus=1000.0, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    base = 15000.00
    sso = 750
    gross = round(base + 1000.0, 2)
    net = round(gross - 0 - sso - 0 - 2600.0, 2)
    assert it['salary_advance_deduction'] == 2600.0
    assert it['gross'] == gross
    assert it['net_pay'] == net
