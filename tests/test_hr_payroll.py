"""TDD — HR payroll money math (`inventory_app/hr.py`).

Risky money math → written FIRST, run RED, then implement until GREEN.

Fixture: `tmp_db_conn` (copy of live DB; already carries migration 054 HR
schema + seeds: EMP001 วุฒิพงษ์ start 2026-05-02 flat 13000;
EMP002 วิภา 13000 then 15000 from 2026-07-01). `empty_db` is broken at
mig 014 per project memory — do NOT use it here.

WORKED-DAY / PRORATION CONVENTION (documented decision — see also hr.py):
  worked_days for a payroll month =
      (min(period_end, end_date or period_end)
       - max(period_start, start_date or period_start)).days + 1
  i.e. INCLUSIVE calendar days the employee was on payroll within the month,
  then CAPPED at hr_config.day_divisor (30). base_amount =
  round(rate/day_divisor * worked_days, 2). Rationale: Thai monthly payroll
  conventionally divides by a fixed 30 ("วันต่อเดือน") regardless of 28/30/31;
  capping worked_days at the divisor means a full (or near-full) month never
  overpays. Consequence asserted in test_new_hire_proration_emp001:
  วุฒิพงษ์ started 2 May 2026 → May 2→31 inclusive = 30 calendar days,
  capped at divisor 30 → base_amount == 13000.00 (no proration loss for a
  2nd-of-month start in a 31-day month). A mid-month start (e.g. day 16)
  WOULD prorate (16 days).
"""
import sqlite3

import pytest

import hr


# ── helpers ──────────────────────────────────────────────────────────────────

def _leave_type_id(conn, code):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _add_advance(conn, employee_id, advance_date, amount):
    conn.execute(
        """INSERT INTO salary_advances
             (employee_id, advance_date, amount, raw_name)
           VALUES (?, ?, ?, 'test')""",
        (employee_id, advance_date, amount),
    )
    conn.commit()


def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=13000.0, diligence=0.0, sso_enrolled=1,
                 company_id=1, gender='M'):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, ?, ?, ?, 90, ?, ?, 1)""",
        (emp_code, full_name, gender, company_id, start_date,
         sso_enrolled, diligence),
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


def _add_leave(conn, employee_id, code, start, end, days, status='approved'):
    conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (employee_id, _leave_type_id(conn, code), start, end, days, status),
    )
    conn.commit()


def _item(conn, run_id, employee_id):
    r = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, employee_id),
    ).fetchone()
    assert r is not None, "payroll_items row missing"
    return r


# ── 1. SSO ───────────────────────────────────────────────────────────────────

def test_sso_cap_at_750(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_SSO1', 'sso cap', '2026-01-01',
                       monthly_salary=30000.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # min(max(30000, 1650), 15000) * 0.05 = 15000*0.05 = 750
    assert it['sso_employee'] == 750
    assert it['sso_employer'] == 750


def test_sso_min_base_floor(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_SSO2', 'sso floor', '2026-01-01',
                       monthly_salary=1000.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # min(max(1000, 1650), 15000) * 0.05 = 1650 * 0.05 = 82.5
    assert it['sso_employee'] == 82.5
    assert it['sso_employer'] == 82.5


def test_sso_disabled_when_not_enrolled(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_SSO3', 'sso off', '2026-01-01',
                       monthly_salary=30000.0, sso_enrolled=0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['sso_employee'] == 0
    assert it['sso_employer'] == 0


# ── 2. Unpaid-leave deduction ────────────────────────────────────────────────

def test_unpaid_leave_deduction(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_UNP1', 'unpaid', '2026-01-01',
                       monthly_salary=15000.0)
    # 2 UNPAID days in March
    _add_leave(tmp_db_conn, eid, 'UNPAID', '2026-03-10', '2026-03-11', 2)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # 15000/30 * 2 = 1000.00
    assert it['unpaid_leave_days'] == 2
    assert it['unpaid_leave_deduction'] == 1000.00


# ── 3. Over-quota auto-unpaid ────────────────────────────────────────────────

def test_over_quota_sick_becomes_unpaid(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_OQ1', 'overquota sick', '2025-01-01',
                       monthly_salary=15000.0)
    # SICK quota = 30. Log 32 SICK days in the year. 30 within March (the run
    # month) carries the over-quota detection in that month: 32 used → 2 unpaid.
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-03-01', '2026-04-01', 32)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # 32 - 30 quota = 2 over-quota paid-leave days → auto unpaid
    assert it['unpaid_leave_days'] == 2
    assert it['unpaid_leave_deduction'] == round(15000 / 30 * 2, 2)
    assert it['note'] is not None and 'เกินสิทธิ' in it['note']


def test_over_quota_personal_excess_unpaid(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_OQ2', 'overquota personal', '2025-01-01',
                       monthly_salary=15000.0)
    # PERSONAL quota = 6 (Put 2026-07-22). Log 8 PERSONAL days → 2 excess unpaid.
    _add_leave(tmp_db_conn, eid, 'PERSONAL', '2026-03-01', '2026-03-08', 8)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['unpaid_leave_days'] == 2
    assert it['unpaid_leave_deduction'] == round(15000 / 30 * 2, 2)


# ── 5. เบี้ยขยัน (diligence) ──────────────────────────────────────────────────

def test_diligence_forfeited_on_leave(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_DIL1', 'dil leave', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-03-05', '2026-03-05', 1)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_forfeited'] == 1
    assert it['diligence_forfeit_reason'] == 'leave'
    # not added to gross
    assert it['gross'] == it['base_amount']


def test_diligence_kept_when_no_leave(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_DIL2', 'dil keep', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_forfeited'] == 0
    assert it['diligence_forfeit_reason'] is None
    assert it['gross'] == round(it['base_amount'] + 500.0, 2)


def test_diligence_forfeited_on_manual_late(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_DIL3', 'dil late', '2026-01-01',
                       monthly_salary=13000.0, diligence=500.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # initially kept (no leave)
    assert it['diligence_forfeited'] == 0
    # admin toggles "มาสาย" on the line
    hr.update_payroll_item(it['id'], late=True, conn=tmp_db_conn)
    it2 = _item(tmp_db_conn, run['id'], eid)
    assert it2['diligence_forfeited'] == 1
    assert it2['diligence_forfeit_reason'] == 'late'
    assert it2['gross'] == it2['base_amount']


# ── 6. Salary next-full-month resolution (วิภา 13000→15000 progression) ───────
# Hermetic: build the progression on a fresh employee. The live-DB EMP002 has
# drifted to real salary data, so we no longer read the mig-054 seed by code.

def test_resolve_salary_emp002_progression(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_PROG', 'progression hire', '2026-01-01',
                       monthly_salary=13000.0)
    # post-probation raise effective the 1st of the next full month (mirrors
    # the mig-054 EMP002 seed: 13000 then 15000 from 2026-07-01)
    tmp_db_conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2026-07-01', 15000, 'post_probation')""",
        (eid,),
    )
    tmp_db_conn.commit()
    assert hr.resolve_salary(eid, '2026-04', conn=tmp_db_conn)['monthly_salary'] == 13000
    assert hr.resolve_salary(eid, '2026-06', conn=tmp_db_conn)['monthly_salary'] == 13000
    assert hr.resolve_salary(eid, '2026-07', conn=tmp_db_conn)['monthly_salary'] == 15000
    assert hr.resolve_salary(eid, '2026-09', conn=tmp_db_conn)['monthly_salary'] == 15000


# ── 7. New-hire proration (วุฒิพงษ์-style new hire, start 2026-05-02) ──────────
# Hermetic: fresh new-hire (live-DB EMP001 has drifted). Same pattern as
# test_mid_month_start_prorates above.

def test_new_hire_proration_emp001(tmp_db_conn_hr_clean):
    # _hr_clean wipes payroll runs so generate_run builds fresh and includes
    # this new hire (the live DB already has finalized Apr/May runs that would
    # otherwise be returned without the just-added employee).
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PROR', 'new hire', '2026-05-02',
                       monthly_salary=13000.0)
    # May 2026: worked 2-May..31-May inclusive = 30 calendar days; capped at
    # day_divisor 30 → no proration loss. base == round(13000/30*30,2) = 13000.00
    run5 = hr.generate_run('2026-05', 1, created_by=1, conn=conn)
    it5 = _item(conn, run5['id'], eid)
    assert it5['salary_rate'] == 13000
    assert it5['base_amount'] == 13000.00
    # June 2026: full month → 13000 flat
    run6 = hr.generate_run('2026-06', 1, created_by=1, conn=conn)
    it6 = _item(conn, run6['id'], eid)
    assert it6['base_amount'] == 13000.00


def test_mid_month_start_prorates(tmp_db_conn):
    # Sanity: a true mid-month start DOES prorate (16-Mar..31-Mar = 16 days).
    eid = _mk_employee(tmp_db_conn, 'T_MID', 'mid hire', '2026-03-16',
                       monthly_salary=15000.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['base_amount'] == round(15000 / 30 * 16, 2)


# ── 8. Half-day leave ────────────────────────────────────────────────────────

def test_half_day_unpaid(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_HALF', 'half day', '2026-01-01',
                       monthly_salary=15000.0)
    _add_leave(tmp_db_conn, eid, 'UNPAID', '2026-03-10', '2026-03-10', 0.5)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['unpaid_leave_days'] == 0.5
    assert it['unpaid_leave_deduction'] == round(15000 / 30 * 0.5, 2)


# ── 9. Full net_pay combined scenario ────────────────────────────────────────

def test_combined_net_pay(tmp_db_conn):
    """net = base + diligence(if kept) + bonus + other_additions
             - unpaid_leave_deduction - sso_employee - other_deductions."""
    eid = _mk_employee(tmp_db_conn, 'T_NET', 'combined', '2026-01-01',
                       monthly_salary=15000.0, diligence=500.0)
    # 1 UNPAID day in March (does NOT affect diligence-by-itself? UNPAID
    # affects_diligence=1 in seed → forfeits diligence). Use ANNUAL-free path:
    # to keep diligence we must have NO affects_diligence leave. So here we
    # intentionally take UNPAID (forfeits diligence) and assert that path.
    _add_leave(tmp_db_conn, eid, 'UNPAID', '2026-03-10', '2026-03-10', 1)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # add bonus / other via edit
    hr.update_payroll_item(it['id'], bonus=1000.0, other_additions=200.0,
                           other_deductions=150.0, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    base = 15000.00                              # full month
    sso = 750                                    # 15000*0.05
    unpaid = round(15000 / 30 * 1, 2)            # 500.00
    # UNPAID affects_diligence=1 → diligence forfeited (reason 'leave')
    assert it['diligence_forfeited'] == 1
    assert it['diligence_forfeit_reason'] == 'leave'
    diligence_kept = 0.0
    gross = round(base + diligence_kept + 1000.0 + 200.0, 2)
    net = round(gross - unpaid - sso - 150.0, 2)
    assert it['base_amount'] == base
    assert it['sso_employee'] == sso
    assert it['unpaid_leave_deduction'] == unpaid
    assert it['gross'] == gross
    assert it['net_pay'] == net


def test_combined_net_pay_diligence_kept(tmp_db_conn):
    """No affects_diligence leave → diligence kept and in gross."""
    eid = _mk_employee(tmp_db_conn, 'T_NET2', 'combined2', '2026-01-01',
                       monthly_salary=20000.0, diligence=500.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    hr.update_payroll_item(it['id'], bonus=2000.0, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    base = 20000.00
    sso = 750                                    # capped
    gross = round(base + 500.0 + 2000.0, 2)
    net = round(gross - 0 - sso - 0, 2)
    assert it['diligence_forfeited'] == 0
    assert it['gross'] == gross
    assert it['net_pay'] == net


# ── Migration 054 idempotency (realistic scenario) ───────────────────────────
# NOTE: tests/test_migration_runner_idempotent.py exercises the runner
# mechanism with a SYNTHETIC probe migration (999_selfrecord_probe.sql) in a
# crafted temp migrations dir — it auto-discovers nothing and does NOT cover
# 054 specifically. 054 is already applied to the live DB and (per plan) does
# NOT self-insert into applied_migrations. This asserts the realistic
# invariant on the tmp_db copy: 054 recorded exactly once, all 9 HR tables
# present, seeds (2 employees, 5 leave types, 4 config keys) present once.

def test_migration_054_applied_exactly_once(tmp_db_conn):
    n = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM applied_migrations WHERE filename='054_hr_module.sql'"
    ).fetchone()[0]
    assert n == 1

    for t in ('employees', 'employee_salary_history', 'leave_types',
              'employee_leave_entitlements', 'leave_requests', 'payroll_runs',
              'payroll_items', 'hr_config', 'company_holidays'):
        assert tmp_db_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone() is not None, f"missing table {t}"

    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM employees WHERE emp_code IN ('EMP001','EMP002')"
    ).fetchone()[0] == 2
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM leave_types"
    ).fetchone()[0] == 5
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM hr_config"
    ).fetchone()[0] == 4


# ── full-month diligence rule ────────────────────────────────────────────────

def test_partial_first_month_no_diligence(tmp_db_conn):
    """Employee who starts mid-month gets no diligence that month."""
    eid = _mk_employee(tmp_db_conn, 'T_PART1', 'partial-first', '2026-09-15',
                       monthly_salary=15000.0, diligence=500.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_allowance'] == 0
    assert it['diligence_forfeited'] == 0  # not 'forfeited' — simply not eligible


def test_full_month_after_partial_gets_diligence(tmp_db_conn):
    """Next full month after partial start: diligence resumes."""
    eid = _mk_employee(tmp_db_conn, 'T_PART2', 'partial-then-full', '2026-09-15',
                       monthly_salary=15000.0, diligence=500.0)
    run = hr.generate_run('2026-10', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_allowance'] == 500
    assert it['diligence_forfeited'] == 0


def test_partial_last_month_no_diligence(tmp_db_conn):
    """Employee whose end_date is mid-month gets no diligence that month."""
    eid = _mk_employee(tmp_db_conn, 'T_PART3', 'partial-end', '2026-01-01',
                       monthly_salary=15000.0, diligence=500.0)
    tmp_db_conn.execute("UPDATE employees SET end_date='2026-09-15' WHERE id=?", (eid,))
    tmp_db_conn.commit()
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_allowance'] == 0


def test_start_on_first_day_is_full_month(tmp_db_conn):
    """start_date == period_start counts as full month (boundary check)."""
    eid = _mk_employee(tmp_db_conn, 'T_PART4', 'first-day', '2026-09-01',
                       monthly_salary=15000.0, diligence=500.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['diligence_allowance'] == 500


# ── 7. reopen_run — un-finalize a finalized payroll run ─────────────────────

def _make_finalized_run(conn, year_month='2026-09'):
    eid = _mk_employee(conn, 'T_REO', 'reopen-target', '2026-01-01',
                       monthly_salary=15000.0)
    run = hr.generate_run(year_month, 1, created_by=1, conn=conn)
    hr.finalize_run(run['id'], conn=conn)
    return run['id'], eid


def test_reopen_run_un_finalizes(tmp_db_conn_hr_clean):
    rid, _ = _make_finalized_run(tmp_db_conn_hr_clean)
    row = hr.reopen_run(rid, reason='ทดสอบ', actor='admin',
                       conn=tmp_db_conn_hr_clean)
    assert row['status'] == 'draft'
    assert row['finalized_at'] is None


def test_reopen_run_writes_audit_log_with_actor_and_reason(tmp_db_conn_hr_clean):
    rid, _ = _make_finalized_run(tmp_db_conn_hr_clean)
    hr.reopen_run(rid, reason='แก้ไข bonus',
                  actor='alice', conn=tmp_db_conn_hr_clean)
    log = tmp_db_conn_hr_clean.execute(
        """SELECT user, changed_fields FROM audit_log
            WHERE table_name='payroll_runs' AND row_id=?
              AND user IS NOT NULL
            ORDER BY id DESC LIMIT 1""",
        (rid,),
    ).fetchone()
    assert log is not None, 'reopen_run should write an explicit audit_log row'
    assert log['user'] == 'alice'
    assert 'แก้ไข bonus' in log['changed_fields']


def test_reopen_run_requires_reason(tmp_db_conn_hr_clean):
    rid, _ = _make_finalized_run(tmp_db_conn_hr_clean)
    with pytest.raises(ValueError):
        hr.reopen_run(rid, reason='', actor='a',
                      conn=tmp_db_conn_hr_clean)
    with pytest.raises(ValueError):
        hr.reopen_run(rid, reason='   ', actor='a',
                      conn=tmp_db_conn_hr_clean)


def test_reopen_run_idempotent_on_draft(tmp_db_conn_hr_clean):
    """Calling reopen on an already-draft run is a no-op (returns the row,
    does NOT write another audit_log entry)."""
    eid = _mk_employee(tmp_db_conn_hr_clean, 'T_REO2', 'draft-target',
                       '2026-01-01', monthly_salary=15000.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    assert run['status'] == 'draft'
    before = tmp_db_conn_hr_clean.execute(
        """SELECT COUNT(*) FROM audit_log WHERE table_name='payroll_runs'
            AND row_id=? AND user IS NOT NULL""", (run['id'],),
    ).fetchone()[0]
    row = hr.reopen_run(run['id'], reason='try', actor='a',
                       conn=tmp_db_conn_hr_clean)
    assert row['status'] == 'draft'
    after = tmp_db_conn_hr_clean.execute(
        """SELECT COUNT(*) FROM audit_log WHERE table_name='payroll_runs'
            AND row_id=? AND user IS NOT NULL""", (run['id'],),
    ).fetchone()[0]
    assert after == before


def test_reopen_run_missing_id_returns_none(tmp_db_conn_hr_clean):
    assert hr.reopen_run(99999, reason='x', actor='a',
                         conn=tmp_db_conn_hr_clean) is None


# ── 8. generate_run reconciles orphaned advance stamps ──────────────────────

def test_regenerate_unstamps_advances_for_dropped_employees(tmp_db_conn_hr_clean):
    """Scenario: run finalized → advances stamped → reopen → employee X
    set inactive → regenerate drops X. Without the reconcile, X's advance
    stays stamped to this run forever and is never deducted. With the
    reconcile, the stamp is cleared so X's advance follows them to the
    next paid run."""
    eid = _mk_employee(tmp_db_conn_hr_clean, 'T_ORPH', 'orphan-target',
                       '2026-01-01', monthly_salary=15000.0)
    _add_advance(tmp_db_conn_hr_clean, eid, '2026-09-05', 500.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    rid = run['id']
    hr.finalize_run(rid, conn=tmp_db_conn_hr_clean)
    # Advance is now stamped to this run.
    stamped = tmp_db_conn_hr_clean.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=?",
        (eid,),
    ).fetchone()[0]
    assert stamped == rid
    # Reopen + deactivate employee + regenerate
    hr.reopen_run(rid, reason='need to drop X', actor='admin',
                  conn=tmp_db_conn_hr_clean)
    tmp_db_conn_hr_clean.execute(
        "UPDATE employees SET is_active=0 WHERE id=?", (eid,)
    )
    tmp_db_conn_hr_clean.commit()
    hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    # X's advance must be un-stamped (NULL), not orphan-stamped to rid.
    after = tmp_db_conn_hr_clean.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=?",
        (eid,),
    ).fetchone()[0]
    assert after is None, (
        f"orphaned advance still stamped to run {after} — would never deduct"
    )


def test_regenerate_keeps_stamps_for_employees_still_in_run(tmp_db_conn_hr_clean):
    """Inverse: when the employee is still in the regenerated run, the
    advance stamp must NOT be cleared. Otherwise the next month would
    double-deduct."""
    eid = _mk_employee(tmp_db_conn_hr_clean, 'T_KEEP', 'keep-stamp',
                       '2026-01-01', monthly_salary=15000.0)
    _add_advance(tmp_db_conn_hr_clean, eid, '2026-09-05', 500.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    rid = run['id']
    hr.finalize_run(rid, conn=tmp_db_conn_hr_clean)
    hr.reopen_run(rid, reason='unrelated edit', actor='admin',
                  conn=tmp_db_conn_hr_clean)
    hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    after = tmp_db_conn_hr_clean.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=?",
        (eid,),
    ).fetchone()[0]
    assert after == rid, (
        "stamp must persist when employee is still in regenerated run"
    )

