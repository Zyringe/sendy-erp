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

def test_reopen_warns_and_holds_when_a_new_employee_would_be_added(tmp_db_conn_hr_clean):
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

    with pytest.raises(hr.RosterDriftWarning):
        hr.reopen_run(rid, reason='ขอแก้ตัวเลข', actor='admin', conn=c)

    # not acknowledged: still finalized, roster untouched
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


def test_regenerate_after_reopen_refuses_a_roster_that_drifted_since(tmp_db_conn_hr_clean):
    """The reopen door is not atomic with the rebuild it precedes (Codex, 2026-08-05).

    `reopen_run` checks the roster and flips the run to draft; `generate_run`
    does the destructive `DELETE FROM payroll_items` + re-INSERT later, in a
    separate transaction. Anyone hired in between lands in the active set, and
    the add-check at the door has already passed — so the exact history damage
    this guard exists to prevent reappears one step further along.

    A run that was reopened carries an audit_log row holding `reopen_reason`
    (written in the same transaction as the status flip, and exempt from
    `prune_audit_log`, whose predicate is transactions/INSERT+DELETE only). That
    marker is what lets generate_run tell reopened history apart from an
    ordinary working draft, with no migration.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2026-11', 1, created_by=1, conn=c)
    rid = run['id']
    before = {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))}
    assert len(before) >= 1
    hr.finalize_run(rid, conn=c)

    # roster still matches, so the door lets it through
    hr.reopen_run(rid, reason='แก้ตัวเลข', actor='admin', conn=c)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'draft'

    # ...and only now does the roster drift
    newbie = _mk_employee(c, 'T_DRIFT', 'hired-after-reopen', '2026-11-01')

    with pytest.raises(ValueError):
        hr.generate_run('2026-11', 1, created_by=1, conn=c)

    roster = {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))}
    assert roster == before, "the rebuild must not have run"
    assert newbie not in roster


def test_confirmed_reopen_opens_the_run_and_per_item_repair_then_works(tmp_db_conn_hr_clean):
    """The recovery path the refusal text promises must actually exist.

    Per-item repair is gated on `status != 'finalized'`
    (blueprints/hr.py::payroll_item_edit) and `reopen_run` is the only door to
    draft — so a hard refusal there left runs 3 and 4 on prod unfixable through
    the UI while the message told the operator to fix them per item. Codex
    review of PR #367. Reopen now warns and proceeds on acknowledgement;
    generate_run keeps its hard refusal, so the destructive path stays shut.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2026-12', 1, created_by=1, conn=c)
    rid = run['id']
    before = {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))}
    assert len(before) >= 1
    hr.finalize_run(rid, conn=c)
    newbie = _mk_employee(c, 'T_CONF', 'hired-later', '2026-12-01')

    dropped, added = hr.roster_drift(rid, conn=c)
    assert added == {newbie} and not dropped
    assert hr.roster_drift_note(rid, conn=c), "the page must have something to show"

    hr.reopen_run(rid, reason='แก้ตัวเลขรายคน', actor='admin', conn=c,
                  confirm_roster_change=True)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'draft'
    assert {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))} == before, \
        "reopening must not have touched the roster"

    # the repair itself now works...
    item_id, = c.execute(
        "SELECT id FROM payroll_items WHERE run_id=? LIMIT 1", (rid,)).fetchone()
    hr.update_payroll_item(item_id, bonus=500.0, conn=c)
    assert c.execute("SELECT bonus FROM payroll_items WHERE id=?",
                     (item_id,)).fetchone()[0] == 500.0

    # ...while the destructive path stays shut
    with pytest.raises(ValueError):
        hr.generate_run('2026-12', 1, created_by=1, conn=c)
    assert newbie not in {r[0] for r in c.execute(
        "SELECT employee_id FROM payroll_items WHERE run_id=?", (rid,))}


def test_reopen_without_drift_needs_no_confirmation(tmp_db_conn_hr_clean):
    """Control: the acknowledgement must be demanded only when it means
    something, or every reopen grows a checkbox nobody reads."""
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2026-12', 1, created_by=1, conn=c)
    rid = run['id']
    hr.finalize_run(rid, conn=c)
    assert hr.roster_drift_note(rid, conn=c) is None

    hr.reopen_run(rid, reason='แก้ตัวเลข', actor='admin', conn=c)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'draft'


def test_pending_advance_stamp_is_independent_of_roster_drift(tmp_db_conn_hr_clean):
    """Re-finalizing a reopened run stamps every un-deducted advance dated on or
    before that month's end (finalize_run) — for employees in the run. On a
    month whose payslips were already PAID that marks the advance deducted
    without any cash ever being withheld, so the money is silently never
    collected.

    That hazard has nothing to do with the roster, so it must not be reported
    through `roster_drift_note`: this run has NO drift and still needs the
    warning. The trigger is the bug class this whole arc started from — บอล's
    ฿1,000 was keyed 2026-07-03 but dated 2026-06-27, landing inside a month
    that had already closed.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-01', 1, created_by=1, conn=c)
    rid = run['id']
    emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                    (rid,)).fetchone()[0]
    hr.finalize_run(rid, conn=c)
    assert hr.roster_drift_note(rid, conn=c) is None, "no drift — the other warning must stay silent"
    assert hr.pending_advance_stamp(rid, conn=c) == (0, 0.0)

    # an advance keyed late, back-dated into the closed month
    _add_advance(c, emp, '2027-01-20', 750.0)
    assert hr.pending_advance_stamp(rid, conn=c) == (1, 750.0)
    note = hr.pending_advance_note(rid, conn=c)
    assert note and '750' in note
    assert hr.roster_drift_note(rid, conn=c) is None, "still no roster drift"


def test_pending_advance_stamp_ignores_advances_after_the_month(tmp_db_conn_hr_clean):
    """Control: finalize_run bounds the stamp at period_end, so a LATER advance
    is not at risk and must not be warned about — otherwise every run carries a
    permanent scary banner. This is why prod shows 0 today: all five un-stamped
    advances are dated after both closed months."""
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-01', 1, created_by=1, conn=c)
    rid = run['id']
    emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                    (rid,)).fetchone()[0]
    hr.finalize_run(rid, conn=c)

    _add_advance(c, emp, '2027-02-05', 900.0)
    assert hr.pending_advance_stamp(rid, conn=c) == (0, 0.0)
    assert hr.pending_advance_note(rid, conn=c) is None


# ── enforcement sits at the money boundary, not at the reopen door ──────────

def test_first_finalize_still_stamps_advances_without_any_confirmation(tmp_db_conn_hr_clean):
    """CONTROL, and the most important test here: stamping advances IS the job
    of a first finalize. If the new guard fires on this path, monthly payroll
    stops working for everyone. It must key on the run having been REOPENED,
    not on advances merely existing.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-03', 1, created_by=1, conn=c)
    rid = run['id']
    emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                    (rid,)).fetchone()[0]
    _add_advance(c, emp, '2027-03-10', 800.0)
    adv = c.execute("SELECT MAX(id) FROM salary_advances").fetchone()[0]
    assert hr.pending_advance_stamp(rid, conn=c) == (1, 800.0)

    hr.finalize_run(rid, conn=c)          # no confirmation passed

    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'finalized'
    assert c.execute("SELECT deducted_in_run_id FROM salary_advances WHERE id=?",
                     (adv,)).fetchone()[0] == rid, "the normal stamp must still happen"


def test_refinalize_after_reopen_refuses_to_swallow_a_backdated_advance(tmp_db_conn_hr_clean):
    """The hazard, pinned where it actually happens.

    The reopen-time banner disappears the moment the run turns draft
    (blueprints/hr.py::payroll_detail computed it for finalized runs only), and
    the Finalize button called finalize_run with no check at all — so an
    advance added or back-dated AFTER the reopen was stamped silently, at the
    exact moment the money changed state. Codex review of PR #367.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-04', 1, created_by=1, conn=c)
    rid = run['id']
    emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                    (rid,)).fetchone()[0]
    hr.finalize_run(rid, conn=c)
    hr.reopen_run(rid, reason='แก้ตัวเลข', actor='admin', conn=c)

    # keyed only now, back-dated into the month that already closed
    _add_advance(c, emp, '2027-04-11', 650.0)
    adv = c.execute("SELECT MAX(id) FROM salary_advances").fetchone()[0]

    with pytest.raises(hr.PendingAdvanceStampWarning):
        hr.finalize_run(rid, conn=c)

    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'draft', "refused before any mutation"
    assert c.execute("SELECT deducted_in_run_id FROM salary_advances WHERE id=?",
                     (adv,)).fetchone()[0] is None, "and the advance is untouched"

    # There is NO override. Stamping without deducting is not a thing an
    # operator can consent to, because consent cannot make the money move:
    # salary_advance_deduction and net_pay are computed in _build_item during
    # generate_run, and generate_run is refused on a reopened run whose roster
    # drifted. An earlier version offered a confirm_advance_stamp checkbox
    # whose label promised a deduction it could not perform (Codex).
    import inspect
    assert 'confirm_advance_stamp' not in inspect.signature(hr.finalize_run).parameters

    # The way out is to fix the DATA: re-date the advance to a month that is
    # still open, so a real generate_run can put it into someone's net pay.
    c.execute("UPDATE salary_advances SET advance_date='2027-05-11' WHERE id=?", (adv,))
    c.commit()
    assert hr.pending_advance_stamp(rid, conn=c) == (0, 0.0)
    hr.finalize_run(rid, conn=c)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'finalized'
    assert c.execute("SELECT deducted_in_run_id FROM salary_advances WHERE id=?",
                     (adv,)).fetchone()[0] is None, \
        "re-dated out of the month, so this run must not claim it"


def test_the_stamp_a_first_finalize_writes_is_actually_in_the_payslip(tmp_db_conn_hr_clean):
    """The invariant the refusal exists to protect, stated positively.

    A stamp is only honest when the same advance is inside that item's
    salary_advance_deduction — _build_item computes it during generate_run,
    finalize_run only marks it. Asserting the stamp alone (which the first
    version of these tests did) pins the broken state just as happily as the
    correct one.
    """
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-06', 1, created_by=1, conn=c)
    rid = run['id']
    emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                    (rid,)).fetchone()[0]
    _add_advance(c, emp, '2027-06-09', 400.0)
    run = hr.generate_run('2027-06', 1, created_by=1, conn=c)   # rebuild to pick it up
    hr.finalize_run(rid, conn=c)

    item = c.execute(
        "SELECT salary_advance_deduction, gross, net_pay FROM payroll_items"
        " WHERE run_id=? AND employee_id=?", (rid, emp)).fetchone()
    assert item['salary_advance_deduction'] == 400.0, "the money must be in the payslip"
    assert round(item['gross'] - item['net_pay'], 2) >= 400.0
    assert c.execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE employee_id=? AND amount=400",
        (emp,)).fetchone()[0] == rid


def test_refinalize_after_reopen_with_nothing_pending_needs_no_confirmation(tmp_db_conn_hr_clean):
    """Control: the demand must be tied to money actually being at stake."""
    c = tmp_db_conn_hr_clean
    run = hr.generate_run('2027-05', 1, created_by=1, conn=c)
    rid = run['id']
    hr.finalize_run(rid, conn=c)
    hr.reopen_run(rid, reason='แก้ตัวเลข', actor='admin', conn=c)
    assert hr.pending_advance_stamp(rid, conn=c) == (0, 0.0)

    hr.finalize_run(rid, conn=c)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (rid,)).fetchone()[0] == 'finalized'


# ── the decision and the write must be one transaction ─────────────────────

def _concurrent_insert_blocked(db_path, employee_id, advance_date, amount):
    """Try to write an advance from a SECOND connection with a short timeout.

    Returns True if the writer was locked out. `timeout` is deliberately tiny
    so "excluded" is a fast, certain answer instead of a stall.
    """
    other = sqlite3.connect(db_path, timeout=0.1)
    try:
        other.execute(
            "INSERT INTO salary_advances (employee_id, advance_date, amount)"
            " VALUES (?,?,?)", (employee_id, advance_date, amount))
        other.commit()
        return False
    except sqlite3.OperationalError as e:
        return "locked" in str(e).lower()
    finally:
        other.close()


def test_finalize_holds_the_write_lock_across_the_pending_check(tmp_db, monkeypatch):
    """The pending check and the stamp must be one BEGIN IMMEDIATE transaction.

    Under the default deferred isolation the connection holds no lock until it
    writes, so the sequence Codex described was live: A reads pending = 0, B
    inserts a back-dated advance and commits, A flips the run to finalized, and
    A's UPDATE then stamps B's advance as deducted while no payslip carries it.
    Railway runs gunicorn -w 2, so the second worker is real.

    The seam is patched BEFORE the first write — a probe placed after it would
    be excluded either way and would pass with the fix removed.
    """
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_RACE', 'race-target', '2027-07-01')
        run = hr_mod.generate_run('2027-07', 1, created_by=1, conn=conn)
        rid = run['id']
        conn.commit()
    finally:
        conn.close()

    seen = {}
    real = hr_mod._was_reopened

    def probe(c, run_id):
        # _was_reopened is consulted on EVERY finalize and before every write,
        # so the probe lands in the window the fix is meant to close.
        seen['blocked'] = _concurrent_insert_blocked(tmp_db, eid, '2027-07-10', 500.0)
        return real(c, run_id)

    monkeypatch.setattr(hr_mod, '_was_reopened', probe)
    hr_mod.finalize_run(rid, db_path=tmp_db)

    assert seen, "the probe never ran — the seam moved, so this test proves nothing"
    assert seen['blocked'] is True, (
        "a concurrent writer got in between the pending check and the stamp")


def test_generate_holds_the_write_lock_across_the_roster_check(tmp_db, monkeypatch):
    """Same shape, same hazard: generate_run decides from the active set and
    then DELETEs every payroll_item of the run. An employee deactivated between
    the two makes the guard decide on a roster that no longer exists."""
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_RACE2', 'race-target-2', '2027-08-01')
        conn.commit()
    finally:
        conn.close()
    # Create the run FIRST. On a brand-new month generate_run INSERTs the
    # payroll_runs row before the roster check, which takes the write lock for
    # unrelated reasons — the probe would then be excluded with or without the
    # fix and the test would pass for the wrong reason.
    hr_mod.generate_run('2027-08', 1, created_by=1, db_path=tmp_db)

    seen = {}
    real = hr_mod._active_employees_for_month

    def probe(c, company_id, period_start, period_end):
        seen['blocked'] = _concurrent_insert_blocked(tmp_db, eid, '2027-08-10', 100.0)
        return real(c, company_id, period_start, period_end)

    monkeypatch.setattr(hr_mod, '_active_employees_for_month', probe)
    hr_mod.generate_run('2027-08', 1, created_by=1, db_path=tmp_db)

    assert seen, "the probe never ran — the seam moved"
    assert seen['blocked'] is True, (
        "a concurrent writer got in between the roster check and the rebuild")


def test_generate_rereads_the_run_after_taking_the_lock(tmp_db, monkeypatch):
    """A worker that finalizes while we wait for the lock must not have its run
    rebuilt underneath it.

    The status read happened BEFORE _begin_immediate, so the decision "this run
    is a draft, I may DELETE its items" rested on a value another worker could
    invalidate before we ever acquired the lock (Codex).
    """
    import hr as hr_mod
    hr_mod.generate_run('2027-09', 1, created_by=1, db_path=tmp_db)
    rid = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM payroll_runs WHERE year_month='2027-09'").fetchone()[0]
    # item IDs, not employee IDs: a rebuild produces the SAME employees with
    # NEW rows, so comparing the employee set cannot see the DELETE+INSERT.
    before = {r[0] for r in sqlite3.connect(tmp_db).execute(
        "SELECT id FROM payroll_items WHERE run_id=?", (rid,))}
    assert before, "fixture must produce items, or this pins nothing"

    fired = {}
    real = hr_mod._begin_immediate

    def finalize_first(c):
        # the interleaving worker, committing before we hold the lock
        if not fired:
            fired['yes'] = True
            other = sqlite3.connect(tmp_db, timeout=5)
            other.execute("UPDATE payroll_runs SET status='finalized' WHERE id=?", (rid,))
            other.commit()
            other.close()
        return real(c)

    monkeypatch.setattr(hr_mod, '_begin_immediate', finalize_first)
    hr_mod.generate_run('2027-09', 1, created_by=1, db_path=tmp_db)

    assert fired, "the seam never ran"
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT status FROM payroll_runs WHERE id=?",
                        (rid,)).fetchone()[0] == 'finalized'
    assert {r[0] for r in conn.execute(
        "SELECT id FROM payroll_items WHERE run_id=?", (rid,))} == before, \
        "a finalized run must not be rebuilt"


def test_generate_reads_config_inside_the_lock(tmp_db, monkeypatch):
    """_load_config feeds _build_item for every employee, so reading it before
    the lock lets a config change land mid-decision (Codex)."""
    import hr as hr_mod
    seen = {}
    real = hr_mod._load_config

    def probe(c):
        seen['in_txn'] = c.in_transaction
        return real(c)

    monkeypatch.setattr(hr_mod, '_load_config', probe)
    hr_mod.generate_run('2027-10', 1, created_by=1, db_path=tmp_db)

    assert seen, "the probe never ran"
    assert seen['in_txn'] is True, "config was read outside the write transaction"


def test_money_paths_refuse_a_caller_transaction_already_in_flight(tmp_db):
    """Silently downgrading the guarantee is worse than refusing: the caller
    believes the payroll write is serialized and it is not (Codex)."""
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE hr_config SET value = value WHERE key='sso_rate'")
        assert conn.in_transaction
        with pytest.raises(hr_mod.CallerTransactionInFlight):
            hr_mod.generate_run('2027-11', 1, created_by=1, conn=conn)
    finally:
        conn.rollback()
        conn.close()


# ── the serialization boundary must cover the whole state machine ──────────

def _payroll_state_machine_writers():
    """The functions that read payroll_runs.status (or a row's parent status)
    and then write based on it. Every one must hold the write lock across that
    pair, or the other side of any pairing decides from state it no longer has."""
    return ('generate_run', 'finalize_run', 'reopen_run',
            'post_salary_payment', 'update_payroll_item')


def test_reopen_and_post_cannot_interleave(tmp_db, monkeypatch):
    """The paired race Codex found by sweeping the writers.

      reopen_run sees paid_count = 0
          post_salary_payment sees the run as finalized
      reopen flips the run to draft
          post inserts the payment row

    Result: a draft run with money already posted against it — and a draft run
    is editable, so those figures can then be changed underneath a payment that
    has already left the account. Locking only reopen_run does not help: post
    still decides from a status it read before the flip.
    """
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_PAIR', 'pair-race', '2027-12-01')
        run = hr_mod.generate_run('2027-12', 1, created_by=1, conn=conn)
        rid = run['id']
        hr_mod.finalize_run(rid, conn=conn)
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? AND employee_id=?",
            (rid, eid)).fetchone()[0]
        acct = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE is_transfer=0 LIMIT 1").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    seen = {}
    real = hr_mod._begin_immediate

    def probe(c):
        # BEGIN IMMEDIATE *is* the first write, so the window that matters is
        # right after it and before the status read: lock held, decision not
        # yet made. Probing before it would merely flip the run to draft for
        # real and prove nothing about serialization.
        out = real(c)
        if 'blocked' not in seen:
            seen['blocked'] = _concurrent_update_blocked(
                tmp_db, "UPDATE payroll_runs SET status='draft' WHERE id=%d" % rid)
        return out

    monkeypatch.setattr(hr_mod, '_begin_immediate', probe)
    hr_mod.post_salary_payment(item_id, acct, '2027-12-28', 'admin', db_path=tmp_db)

    assert seen, "post_salary_payment never took the lock — the seam never ran"
    assert seen['blocked'] is True, (
        "another worker could reopen the run while a payment was being posted")


def _concurrent_update_blocked(db_path, sql):
    other = sqlite3.connect(db_path, timeout=0.1)
    try:
        other.execute(sql)
        other.commit()
        return False
    except sqlite3.OperationalError as e:
        return "locked" in str(e).lower()
    finally:
        other.close()


def test_edit_and_finalize_cannot_interleave(tmp_db, monkeypatch):
    """update_payroll_item refuses a finalized parent, but read the status and
    then wrote without holding the lock — so a finalize landing in between let
    an issued payslip be rewritten."""
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        _mk_employee(conn, 'T_EDITRACE', 'edit-race', '2028-01-01')
        run = hr_mod.generate_run('2028-01', 1, created_by=1, conn=conn)
        rid = run['id']
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? LIMIT 1", (rid,)).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    seen = {}
    real = hr_mod._begin_immediate

    def probe(c):
        out = real(c)
        if 'blocked' not in seen:
            seen['blocked'] = _concurrent_update_blocked(
                tmp_db, "UPDATE payroll_runs SET status='finalized' WHERE id=%d" % rid)
        return out

    monkeypatch.setattr(hr_mod, '_begin_immediate', probe)
    hr_mod.update_payroll_item(item_id, bonus=100.0, db_path=tmp_db)

    assert seen, "update_payroll_item never took the lock"
    assert seen['blocked'] is True, (
        "a finalize could land between the status check and the edit")


def test_every_state_machine_writer_refuses_a_caller_transaction(tmp_db):
    """The contract must hold across the whole boundary, not just where it was
    introduced — a writer that silently accepts an unknown transaction is the
    hole the others are guarding.

    Each writer is given state that makes it REACH its lock. finalize_run is
    the one that needs it: its "already finalized" exit is a read-only no-op
    taken before the lock, and Codex ruled that path should not be refused —
    so testing it with an arbitrary id would assert the opposite of the agreed
    contract.
    """
    import hr as hr_mod
    setup = sqlite3.connect(tmp_db, timeout=10)
    setup.row_factory = sqlite3.Row
    try:
        _mk_employee(setup, 'T_CONTRACT', 'contract-probe', '2028-03-01')
        draft = hr_mod.generate_run('2028-03', 1, created_by=1, conn=setup)
        draft_id = draft['id']
        item_id = setup.execute(
            "SELECT id FROM payroll_items WHERE run_id=? LIMIT 1", (draft_id,)).fetchone()[0]
        acct = setup.execute(
            "SELECT id FROM cashbook_accounts WHERE is_transfer=0 LIMIT 1").fetchone()[0]
        setup.commit()
    finally:
        setup.close()

    calls = {
        'generate_run':        lambda c: hr_mod.generate_run('2028-04', 1, created_by=1, conn=c),
        'finalize_run':        lambda c: hr_mod.finalize_run(draft_id, conn=c),
        'reopen_run':          lambda c: hr_mod.reopen_run(draft_id, reason='x', actor='a', conn=c),
        'post_salary_payment': lambda c: hr_mod.post_salary_payment(item_id, acct, '2028-03-28', 'a', conn=c),
        'update_payroll_item': lambda c: hr_mod.update_payroll_item(item_id, bonus=1.0, conn=c),
    }
    assert set(calls) == set(_payroll_state_machine_writers())

    for name, call in calls.items():
        conn = sqlite3.connect(tmp_db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN")
            conn.execute("UPDATE hr_config SET value = value WHERE key='sso_rate'")
            assert conn.in_transaction
            with pytest.raises(hr_mod.CallerTransactionInFlight):
                call(conn)
        finally:
            conn.rollback()
            conn.close()


def test_no_writer_leaves_the_caller_connection_holding_the_lock(tmp_db):
    """A refusal or a no-op must not leave the caller inside our transaction.

    _begin_immediate opens the transaction, but _ConnCtx deliberately does not
    manage a caller-supplied connection — so every raise and every early return
    after the lock left it in_transaction=True, holding SQLite's single
    database-wide write lock until the caller happened to commit or close.
    Every other writer would block meanwhile, and a later unrelated commit
    would silently include whatever we had done (Codex review of PR #367).

    Safe to fix precisely because the helper REFUSES a pre-existing
    transaction: anything open at that point is ours to end.
    """
    import hr as hr_mod
    setup = sqlite3.connect(tmp_db, timeout=10)
    setup.row_factory = sqlite3.Row
    try:
        _mk_employee(setup, 'T_LEAK', 'leak-probe', '2028-06-01')
        draft = hr_mod.generate_run('2028-06', 1, created_by=1, conn=setup)
        draft_id = draft['id']
        item_id = setup.execute(
            "SELECT id FROM payroll_items WHERE run_id=? LIMIT 1", (draft_id,)).fetchone()[0]
        gone = hr_mod.generate_run('2028-07', 1, created_by=1, conn=setup)
        hr_mod.finalize_run(gone['id'], conn=setup)
        setup.commit()
    finally:
        setup.close()

    # (label, callable, expects_raise) — each hits a rejection or a no-op
    # AFTER the lock is taken, which is where the leak lived.
    cases = [
        ('reopen_run/not-found',
         lambda c: hr_mod.reopen_run(999999, reason='x', actor='a', conn=c), False),
        ('reopen_run/already-draft',
         lambda c: hr_mod.reopen_run(draft_id, reason='x', actor='a', conn=c), False),
        ('finalize_run/already-finalized',
         lambda c: hr_mod.finalize_run(gone['id'], conn=c), False),
        ('update_payroll_item/not-found',
         lambda c: hr_mod.update_payroll_item(999999, bonus=1.0, conn=c), True),
        ('post_salary_payment/not-found',
         lambda c: hr_mod.post_salary_payment(999999, 1, '2028-06-28', 'a', conn=c), True),
        ('post_salary_payment/draft-parent',
         lambda c: hr_mod.post_salary_payment(item_id, 1, '2028-06-28', 'a', conn=c), True),
    ]

    for label, call, expects_raise in cases:
        c = sqlite3.connect(tmp_db, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            assert not c.in_transaction, label
            if expects_raise:
                with pytest.raises(Exception):
                    call(c)
            else:
                call(c)
            assert c.in_transaction is False, (
                f"{label}: the caller was left holding our transaction")
        finally:
            c.rollback()
            c.close()

    # ...and a writer that RAISES its own guard must release it too
    c = sqlite3.connect(tmp_db, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        emp = c.execute("SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
                        (draft_id,)).fetchone()[0]
        c.execute("UPDATE employees SET is_active=0 WHERE id=?", (emp,))
        c.commit()
        with pytest.raises(ValueError):
            hr_mod.generate_run('2028-06', 1, created_by=1, conn=c)   # roster guard
        assert c.in_transaction is False, "generate_run's roster refusal leaked the lock"
    finally:
        c.rollback()
        c.close()


# ── _ConnCtx cleanup contract ──────────────────────────────────────────────

class _FlakyConn:
    """Wraps a real connection and fails one chosen method, so the cleanup
    path can be exercised without waiting for a real disk error."""

    def __init__(self, real, fail_on, also_fail=None):
        self._real, self._fail_on, self.closed = real, fail_on, False
        self._also = also_fail

    def _should_fail(self, name):
        return name == self._fail_on or name == self._also

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def commit(self):
        if self._should_fail('commit'):
            raise sqlite3.OperationalError('disk I/O error')
        return self._real.commit()

    def rollback(self):
        if self._should_fail('rollback'):
            raise sqlite3.OperationalError('disk I/O error')
        return self._real.rollback()

    def close(self):
        self.closed = True
        if self._should_fail('close'):
            raise sqlite3.OperationalError('cannot close')
        return self._real.close()


def test_connctx_closes_its_connection_even_when_commit_fails(tmp_db, monkeypatch):
    """A failing commit must not skip close(). The previous __exit__ called
    commit() before close() with nothing between them, so a raising commit
    leaked the connection — and under gunicorn that leak accumulates until the
    process is recycled (Codex)."""
    import hr as hr_mod
    flaky = {}

    def fake_connect(db_path=None):
        real = sqlite3.connect(tmp_db, timeout=10)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        flaky['c'] = _FlakyConn(real, 'commit')
        return flaky['c']

    monkeypatch.setattr(hr_mod, '_connect', fake_connect)
    # reopen_run on a missing id returns cleanly WITHOUT committing, so the
    # commit under test is the one __exit__ performs. Picking a function that
    # commits inside its own body would route through the exception path and
    # prove nothing about __exit__.
    with pytest.raises(sqlite3.OperationalError):
        hr_mod.reopen_run(999999, reason='x', actor='a')

    assert flaky['c'].closed is True, "the connection leaked when commit failed"


def test_connctx_rollback_failure_does_not_mask_the_real_error(tmp_db, monkeypatch):
    """The body's exception is the one the operator needs. A rollback that
    fails while unwinding must not replace it (Codex)."""
    import hr as hr_mod
    flaky = {}

    def fake_connect(db_path=None):
        real = sqlite3.connect(tmp_db, timeout=10)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        flaky['c'] = _FlakyConn(real, 'rollback')
        return flaky['c']

    monkeypatch.setattr(hr_mod, '_connect', fake_connect)
    # a run id that does not exist -> update_payroll_item raises ValueError
    with pytest.raises(ValueError):
        hr_mod.update_payroll_item(999999, bonus=1.0)
    assert flaky['c'].closed is True


def test_connctx_closes_when_the_lock_cannot_be_taken(tmp_db, monkeypatch):
    """BEGIN IMMEDIATE can fail on its own (another writer holds the lock past
    the timeout). __enter__ raises then, so __exit__ never runs and the owned
    connection has to be closed on the way out (Codex)."""
    import hr as hr_mod
    flaky = {}

    def fake_connect(db_path=None):
        real = sqlite3.connect(tmp_db, timeout=10)
        real.row_factory = sqlite3.Row
        flaky['c'] = _FlakyConn(real, None)
        return flaky['c']

    def boom(c):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(hr_mod, '_connect', fake_connect)
    monkeypatch.setattr(hr_mod, '_begin_immediate', boom)
    with pytest.raises(sqlite3.OperationalError):
        hr_mod.generate_run('2028-09', 1, created_by=1)
    assert flaky['c'].closed is True, "connection leaked when the lock could not be taken"


def test_a_successful_owned_run_commits_and_closes(tmp_db, monkeypatch):
    """Control for the three above: the ordinary path must still commit its
    work and close, or the cleanup tests would pass on a function that never
    does anything."""
    import hr as hr_mod
    flaky = {}

    def fake_connect(db_path=None):
        real = sqlite3.connect(tmp_db, timeout=10)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        flaky['c'] = _FlakyConn(real, None)
        return flaky['c']

    monkeypatch.setattr(hr_mod, '_connect', fake_connect)
    run = hr_mod.generate_run('2028-10', 1, created_by=1)
    assert run is not None
    assert flaky['c'].closed is True
    n = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM payroll_items WHERE run_id=?", (run['id'],)).fetchone()[0]
    assert n >= 1, "the work must actually be committed, not just cleaned up"


def test_an_exception_after_a_partial_mutation_rolls_the_whole_thing_back(tmp_db, monkeypatch):
    """The gap Codex listed that the other cleanup tests do not reach.

    generate_run DELETEs every payroll_item of the run and then rebuilds them
    one employee at a time. If anything raises partway, the run must not be
    left with the DELETE applied and only some rows back — that is a payroll
    month silently missing people. Nothing else in the suite forces a failure
    *between* the destructive step and the end of the rebuild.
    """
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        _mk_employee(conn, 'T_PART1', 'partial-one', '2028-11-01')
        _mk_employee(conn, 'T_PART2', 'partial-two', '2028-11-01')
        run = hr_mod.generate_run('2028-11', 1, created_by=1, conn=conn)
        rid = run['id']
        conn.commit()
        before = {r[0] for r in conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=?", (rid,))}
        assert len(before) >= 2, "need several rows, or a partial rebuild cannot show"
    finally:
        conn.close()

    calls = {'n': 0}
    real = hr_mod._build_item

    def boom(c, emp, year_month, cfg, run_id=None):
        calls['n'] += 1
        if calls['n'] == 2:          # after the DELETE and at least one INSERT
            raise sqlite3.OperationalError('disk I/O error')
        return real(c, emp, year_month, cfg, run_id=run_id)

    monkeypatch.setattr(hr_mod, '_build_item', boom)
    with pytest.raises(sqlite3.OperationalError):
        hr_mod.generate_run('2028-11', 1, created_by=1, db_path=tmp_db)

    assert calls['n'] >= 2, "the failure never landed mid-rebuild"
    after = {r[0] for r in sqlite3.connect(tmp_db).execute(
        "SELECT id FROM payroll_items WHERE run_id=?", (rid,))}
    assert after == before, (
        "the DELETE and the partial rebuild were not rolled back — "
        "the month is left missing rows")


def test_borrowed_connection_is_rolled_back_and_still_usable(tmp_db, monkeypatch):
    """Replaces what the owned-connection version of this test could not prove.

    On an OWNED connection close() rolls back by itself, so asserting the data
    afterwards passes whether or not __exit__ ever called rollback. A BORROWED
    connection is never closed here, so its state after the call is entirely
    __exit__'s doing (Codex).
    """
    import hr as hr_mod
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        _mk_employee(conn, 'T_BORROW1', 'borrow-one', '2029-01-01')
        _mk_employee(conn, 'T_BORROW2', 'borrow-two', '2029-01-01')
        run = hr_mod.generate_run('2029-01', 1, created_by=1, conn=conn)
        rid = run['id']
        conn.commit()
        before = {r[0] for r in conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=?", (rid,))}
        assert len(before) >= 2

        calls = {'n': 0}
        real = hr_mod._build_item

        def boom(c, emp, year_month, cfg, run_id=None):
            calls['n'] += 1
            if calls['n'] == 2:
                raise sqlite3.OperationalError('disk I/O error')
            return real(c, emp, year_month, cfg, run_id=run_id)

        monkeypatch.setattr(hr_mod, '_build_item', boom)
        with pytest.raises(sqlite3.OperationalError):
            hr_mod.generate_run('2029-01', 1, created_by=1, conn=conn)
        assert calls['n'] >= 2, "the failure never landed mid-rebuild"

        # the three things only __exit__ can be responsible for here
        assert conn.in_transaction is False, "the caller was left inside our transaction"
        assert {r[0] for r in conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=?", (rid,))} == before, \
            "the DELETE and partial rebuild were not rolled back"
        assert conn.execute("SELECT 1").fetchone()[0] == 1, "connection unusable"
    finally:
        conn.rollback()
        conn.close()


def test_a_failing_commit_on_a_borrowed_connection_releases_the_transaction(tmp_db, monkeypatch):
    """A commit that fails must still hand the connection back clean.

    The caller keeps using it — leaving it inside our transaction hands them
    SQLite's write lock indefinitely. The commit failure is what propagates;
    the rollback is cleanup (Codex)."""
    import hr as hr_mod
    real_conn = sqlite3.connect(tmp_db, timeout=10)
    real_conn.row_factory = sqlite3.Row
    borrowed = _FlakyConn(real_conn, 'commit')
    try:
        # reopen_run on a missing id returns cleanly without committing itself,
        # so the failing commit is the one __exit__ performs.
        with pytest.raises(sqlite3.OperationalError):
            hr_mod.reopen_run(999999, reason='x', actor='a', conn=borrowed)
        assert real_conn.in_transaction is False, (
            "a failing commit left the caller holding the transaction")
        assert borrowed.closed is False, "a borrowed connection must not be closed"
        assert real_conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        real_conn.rollback()
        real_conn.close()


def test_a_failing_rollback_on_a_borrowed_connection_is_not_silent(tmp_db):
    """Swallowing it would hand the caller a connection still holding the lock
    while telling them only about the original error — they would never learn
    the cleanup failed (Codex).

    An OWNED connection is the opposite case: the finally closes it, so the
    transaction dies regardless and the body's exception is the more useful
    thing to propagate. That asymmetry is the point, and it is why this test
    exists — break-it-once showed that swallowing here changed no test at all.
    """
    import hr as hr_mod
    real_conn = sqlite3.connect(tmp_db, timeout=10)
    real_conn.row_factory = sqlite3.Row
    borrowed = _FlakyConn(real_conn, 'rollback')
    try:
        # update_payroll_item raises ValueError on a missing id, inside the
        # locked block — so __exit__ takes its exception path and rollback fails
        with pytest.raises(hr_mod.ConnectionCleanupError) as caught:
            hr_mod.update_payroll_item(999999, bonus=1.0, conn=borrowed)
        # typed attributes, not implicit chaining: `raise X from Y` reads
        # "X caused by Y" and the order here is the reverse (Codex).
        assert isinstance(caught.value.primary_error, ValueError), (
            "the body's error must stay identifiable as the real cause")
        assert isinstance(caught.value.cleanup_error, sqlite3.OperationalError)
        assert borrowed.closed is False, "a borrowed connection must not be closed"
    finally:
        real_conn.close()


def _owned_flaky(monkeypatch, hr_mod, tmp_db, fail_on, also=None, holder=None):
    def fake_connect(db_path=None):
        real = sqlite3.connect(tmp_db, timeout=10)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        f = _FlakyConn(real, fail_on, also)
        if holder is not None:
            holder['c'] = f
        return f
    monkeypatch.setattr(hr_mod, '_connect', fake_connect)


def test_close_failure_never_replaces_the_error_that_actually_happened(tmp_db, monkeypatch):
    """The blocker: close() lives in a finally, so if IT raises it replaces the
    body's exception and the operator is told about the wrong thing (Codex).

    Both failures must survive, and the one that matters must be identifiable
    without reading a traceback."""
    import hr as hr_mod
    holder = {}
    _owned_flaky(monkeypatch, hr_mod, tmp_db, 'close', holder=holder)
    with pytest.raises(hr_mod.ConnectionCleanupError) as caught:
        hr_mod.update_payroll_item(999999, bonus=1.0)     # body raises ValueError
    assert isinstance(caught.value.primary_error, ValueError), \
        "the real cause must be reachable as a typed attribute"
    assert isinstance(caught.value.cleanup_error, sqlite3.OperationalError)
    assert holder['c'].closed is True, "close was still attempted"


def test_lock_failure_plus_close_failure_reports_both(tmp_db, monkeypatch):
    """__enter__'s own cleanup path has the same hazard: the lock could not be
    taken AND the connection could not be closed."""
    import hr as hr_mod
    holder = {}
    _owned_flaky(monkeypatch, hr_mod, tmp_db, 'close', holder=holder)
    monkeypatch.setattr(hr_mod, '_begin_immediate',
                        lambda c: (_ for _ in ()).throw(
                            sqlite3.OperationalError('database is locked')))
    with pytest.raises(hr_mod.ConnectionCleanupError) as caught:
        hr_mod.generate_run('2029-03', 1, created_by=1)
    assert 'locked' in str(caught.value.primary_error)
    assert isinstance(caught.value.cleanup_error, sqlite3.OperationalError)
    assert holder['c'].closed is True


def test_commit_failure_plus_close_failure_keeps_the_commit_as_primary(tmp_db, monkeypatch):
    """A clean body, a failing commit, and a failing close. The commit failure
    is what the caller must act on; the close failure is noise that must not
    bury it."""
    import hr as hr_mod
    holder = {}
    _owned_flaky(monkeypatch, hr_mod, tmp_db, 'commit', also='close', holder=holder)
    with pytest.raises(hr_mod.ConnectionCleanupError) as caught:
        hr_mod.reopen_run(999999, reason='x', actor='a')   # clean return, __exit__ commits
    assert isinstance(caught.value.primary_error, sqlite3.OperationalError)
    assert 'disk I/O' in str(caught.value.primary_error)
    assert holder['c'].closed is True
