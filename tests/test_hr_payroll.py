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

def test_sso_cap_at_875(tmp_db_conn):
    """The ceiling is 17,500 from 1 ม.ค. 2569 (mig 155), so a high salary caps
    at 875, not the old 750.

    ⚠ The figure comes from hr_config.sso_max_base in the copied live DB, not
    from a constant here — the ceiling is set by กฎกระทรวง and already has two
    more increases scheduled (20,000 in 2572, 23,000 in 2575). When those land,
    this expectation moves with them.
    """
    eid = _mk_employee(tmp_db_conn, 'T_SSO1', 'sso cap', '2026-01-01',
                       monthly_salary=30000.0)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    # min(max(30000, 1650), 17500) * 0.05 = 17500*0.05 = 875
    assert it['sso_employee'] == 875
    assert it['sso_employer'] == 875


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
    # SICK quota = 30. This request spans 1 Mar – 1 Apr = 32 days: 31 of them
    # are March's, the 32nd is April's. The 2-day excess is therefore allocated
    # chronologically — day 31 (31 Mar) to March, day 32 (1 Apr) to April — so
    # THIS month deducts 1, not the whole annual excess.
    #
    # Updated 2026-08-06 (HR review finding 3). The old expectation of 2 here
    # was the bug: April's run matched the same leave type and deducted the
    # full 2 again, so a 2-day excess cost 4 days of pay. Cross-month
    # allocation and its totals are proven in
    # tests/test_leave_chronological_allocation.py.
    _add_leave(tmp_db_conn, eid, 'SICK', '2026-03-01', '2026-04-01', 32)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)
    assert it['unpaid_leave_days'] == 1
    assert it['unpaid_leave_deduction'] == round(15000 / 30 * 1, 2)
    assert it['note'] is not None and 'เกินสิทธิ' in it['note']


def test_unpaid_deduction_never_exceeds_the_month_base(tmp_db_conn):
    """A month cannot deduct more than it pays.

    `unpaid_leave_days` is a CALENDAR-day count (31 in a 31-day month) but the
    deduction divides by the fixed 30-day divisor, while `base_amount` caps
    worked_days at that same divisor. So a full 31-day month of unpaid leave
    deducted 15,500 against a 15,000 base — arithmetically impossible; the
    worst honest case is a zero-wage month.

    Put, 2026-08-06: cap the MONEY, not the day count — 31 stays on the record
    as the true absence, and a note explains why only 30 days were deducted.
    """
    eid = _mk_employee(tmp_db_conn, 'T_CLAMP1', 'full month unpaid', '2024-01-01',
                       monthly_salary=15000.0)
    _add_leave(tmp_db_conn, eid, 'UNPAID', '2027-03-01', '2027-03-31', 31)
    run = hr.generate_run('2027-03', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    assert it['unpaid_leave_days'] == 31, "the day count stays truthful"
    assert it['unpaid_leave_deduction'] == it['base_amount'] == 15000.00, \
        "but the money is capped at the month's base pay"
    assert it['gross'] - it['unpaid_leave_deduction'] == 0
    assert it['note'] and 'ไม่เกินฐานเงินเดือน' in it['note'], \
        "the payslip must explain why 31 days deducted only 30"


def test_long_maternity_month_does_not_deduct_more_than_base(tmp_db_conn):
    """The same clamp on the path the maternity cap newly reaches: a calendar
    month falling wholly past the 45 paid days is a zero-wage month, not a
    negative-wage one."""
    eid = _mk_employee(tmp_db_conn, 'T_CLAMP2', 'long maternity', '2024-01-01',
                       monthly_salary=15000.0)
    _add_leave(tmp_db_conn, eid, 'MATERNITY', '2026-11-15', '2027-02-20', 98)
    run = hr.generate_run('2027-01', 1, created_by=1, conn=tmp_db_conn)
    it = _item(tmp_db_conn, run['id'], eid)

    assert it['unpaid_leave_days'] == 31
    assert it['unpaid_leave_deduction'] == it['base_amount'] == 15000.00
    # SSO is still charged (a separate policy question raised with Put's
    # accountant), so net is -sso rather than 0 — but never worse than that.
    assert it['net_pay'] == -it['sso_employee']


def test_floored_annual_entitlement_deducts_the_excess(tmp_db_conn_hr_clean):
    """Rounding the ANNUAL entitlement DOWN is a money path, not a display change.

    Put chose floor over round-to-nearest-0.5 on 2026-08-10. The consequence
    Codex flagged on PR #371: an employee whose entitlement drops below the days
    they have already taken has the difference deducted as UNPAID leave.

    Hire 2026-04-17 → 6 × 259/365 = 4.2575. Under the old rule that rounded to
    4.5 and 4.5 days taken was exactly on quota — nothing unpaid. Under floor it
    is 4, so the last half day crosses and costs 15,000/30 × 0.5 = ฿250.

    This test is the one that goes red if the rounding is ever reverted, because
    a 4.5 entitlement makes the excess zero.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_FLOOR', 'floored annual', '2026-04-17',
                       monthly_salary=15000.0)
    assert hr.leave_balance(eid, 2026, conn=conn)['ANNUAL']['entitlement'] == 4, \
        "precondition: the floored entitlement, not 4.5"

    _add_leave(conn, eid, 'ANNUAL', '2026-05-04', '2026-05-07', 4)    # exactly on quota
    _add_leave(conn, eid, 'ANNUAL', '2026-06-01', '2026-06-01', 0.5)  # crosses it

    bal = hr.leave_balance(eid, 2026, conn=conn)
    assert bal['ANNUAL']['used'] == 4.5
    assert bal['ANNUAL']['over'] == 0.5

    # May sits inside the entitlement — nothing deducted there.
    may = _item(conn, hr.generate_run('2026-05', 1, created_by=1, conn=conn)['id'], eid)
    assert may['unpaid_leave_days'] == 0
    assert may['unpaid_leave_deduction'] == 0

    # June is the month that crossed, so June carries the whole excess.
    jun = _item(conn, hr.generate_run('2026-06', 1, created_by=1, conn=conn)['id'], eid)
    assert jun['unpaid_leave_days'] == 0.5
    assert jun['unpaid_leave_deduction'] == 250.00 == round(15000 / 30 * 0.5, 2)
    assert jun['net_pay'] == round(jun['gross'] - 250.00 - jun['sso_employee'], 2)
    assert jun['note'] and 'เกินสิทธิ' in jun['note']


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
    sso = 875                                    # capped at 17,500 (mig 155)
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

def test_regenerate_refuses_to_drop_a_deactivated_employee(tmp_db_conn_hr_clean):
    """SUPERSEDES `test_regenerate_unstamps_advances_for_dropped_employees`
    (2026-08-05).

    That test pinned a MITIGATION: let the regenerate drop employee X, then
    un-stamp X's advance so it "follows them to the next paid run". Measured on
    a prod snapshot, the drop itself is the damage — it deletes X's payslip
    record for that month, and a deactivated employee has no next paid run for
    the advance to follow them to. `generate_run` now REFUSES instead, so the
    mitigation is unreachable by this path.

    Note the correct flag for someone leaving mid-month is `end_date` (which
    keeps them in the run with a prorated amount), not `is_active = 0` — the
    latter means "gone", and is normally set only after their final payroll.
    """
    eid = _mk_employee(tmp_db_conn_hr_clean, 'T_ORPH', 'orphan-target',
                       '2026-01-01', monthly_salary=15000.0)
    _add_advance(tmp_db_conn_hr_clean, eid, '2026-09-05', 500.0)
    run = hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)
    rid = run['id']
    hr.finalize_run(rid, conn=tmp_db_conn_hr_clean)
    stamped = tmp_db_conn_hr_clean.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=?",
        (eid,),
    ).fetchone()[0]
    assert stamped == rid

    hr.reopen_run(rid, reason='need to drop X', actor='admin',
                  conn=tmp_db_conn_hr_clean)
    tmp_db_conn_hr_clean.execute(
        "UPDATE employees SET is_active=0 WHERE id=?", (eid,)
    )
    tmp_db_conn_hr_clean.commit()

    with pytest.raises(ValueError):
        hr.generate_run('2026-09', 1, created_by=1, conn=tmp_db_conn_hr_clean)

    # X keeps their row AND their stamp — nothing was destroyed or released.
    assert tmp_db_conn_hr_clean.execute(
        "SELECT COUNT(*) FROM payroll_items WHERE run_id=? AND employee_id=?",
        (rid, eid),
    ).fetchone()[0] == 1
    assert tmp_db_conn_hr_clean.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=?",
        (eid,),
    ).fetchone()[0] == rid


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



# ── 9. reopen refuses a roster change in BOTH directions ────────────────────

def test_reopen_refuses_when_a_new_employee_would_be_added(tmp_db_conn_hr_clean):
    """The drop guard (2026-08-05) is one-directional, and the damage is not.

    Measured on prod the same day: `generate_run` returns untouched on a
    finalized run, so `reopen_run` is the ONLY door to a regenerate — and run 3
    (พ.ค. 2026) passed the drop check while regenerating it would have ADDED
    เซี้ยม and ปู้ to a closed month they were never part of. Inventing payslip
    rows in a finalized month is the same class of history damage as deleting
    them, so the door refuses both.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2026-10', 1, created_by=1, conn=c)
    rid = run['id']
    before = {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))}
    assert len(before) >= 1, "fixture must produce a non-empty run, or this pins nothing"
    hr.finalize_run(rid, conn=c)

    # hired afterwards, but start_date lands inside the now-closed month
    newbie = _mk_employee(c, 'T_ADD', 'added-later', '2026-10-01')
    assert newbie not in before

    with pytest.raises(ValueError):
        hr.reopen_run(rid, reason='ขอแก้ตัวเลข', actor='admin', conn=c)

    # refused at the door: still finalized, roster untouched
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'finalized'
    assert {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))} == before


def test_reopen_still_allowed_when_the_roster_is_unchanged(tmp_db_conn_hr_clean):
    """Control for the test above — the guard must distinguish, not always fire.

    Without this, making `reopen_run` raise unconditionally would pass the
    refusal test.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2026-10', 1, created_by=1, conn=c)
    rid = run['id']
    hr.finalize_run(rid, conn=c)

    hr.reopen_run(rid, reason='แก้ตัวเลข', actor='admin', conn=c)

    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'draft'


def test_regenerating_a_draft_run_still_picks_up_a_new_hire(tmp_db_conn_hr_clean):
    """The legitimate flow the guard must NOT break: a run still in draft is a
    working document, so regenerating it to pick up someone hired mid-month is
    the intended use. Only a run that reached 'finalized' has history to
    protect, and that one is reachable solely through the guarded reopen door.
    """
    c = tmp_db_conn_hr_clean
    hr.generate_run('2026-10', 1, created_by=1, conn=c)   # left in draft
    newbie = _mk_employee(c, 'T_HIRE', 'new-hire', '2026-10-01')

    run = hr.generate_run('2026-10', 1, created_by=1, conn=c)

    roster = {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (run['id'],))}
    assert newbie in roster, "a draft run must still absorb a new hire"
