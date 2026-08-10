"""Leave that exceeds its paid allowance is charged ONCE, in the month whose
own days crossed the line.

Two defects, one mechanism:

1. MATERNITY's 45-day employer-paid cap was compared against the maternity days
   *inside the current payroll month only*. A calendar month holds at most 31
   days, so `maternity_in_month - 45` was never positive and a 60- or 98-day
   maternity leave was paid in full, every month.

2. The annual over-quota amount was the WHOLE calendar-year `over`, added to
   every payroll month that any leave of that type touched. 20 sick days in
   January plus 20 in February is 10 days over a 30-day quota, but both runs
   deducted 10 → 20 days deducted for a 10-day excess.

Both are now allocated by walking the leave chronologically, month by month,
and charging only the days that fall past the allowance to the month they
actually fall in.

The headline consequence: an over-quota day is deducted in the month it was
TAKEN, even when that month falls in the following calendar year. A sick leave
running 20 Dec – 8 Jan that crosses the quota on 1 January is docked in
January's payroll, not December's.

Each allowance is judged over a COHORT, never over one month's slice:
  * annual quota — the cohort is a calendar YEAR, and a request belongs to the
    year it STARTED in (exactly what `leave_balance` counts as `used`, so the
    deductions reconcile with the balance /hr shows). A payroll month evaluates
    every cohort year reachable from the leaves touching it, so a straddler is
    still judged against the quota it actually consumes — and cannot eat into
    the next year's.
  * maternity cap — the cohort is the leave itself: every maternity request
    overlapping the year, so a leave crossing 31 December keeps counting toward
    its 45 paid days instead of restarting.

Two designs were tried and rejected on the way here, both pinned by tests: a
population of requests STARTING in the payroll year (strands the January part
of a straddler's excess — nobody ever deducts it), and folding that spill back
onto December (total right, but docks a month the employee was still inside
quota).
"""
import sqlite3

import pytest

import hr


# ── helpers ──────────────────────────────────────────────────────────────────

def _leave_type_id(conn, code):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _mk_employee(conn, emp_code, start_date, monthly_salary=15000.0):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'F', 1, ?, 90, 0, 0, 1)""",
        (emp_code, emp_code, start_date),
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


def _unpaid(conn, year_month, employee_id, company_id=1):
    """Generate `year_month` and return that employee's unpaid_leave_days."""
    run = hr.generate_run(year_month, company_id, created_by=1, conn=conn)
    row = conn.execute(
        "SELECT unpaid_leave_days FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], employee_id),
    ).fetchone()
    assert row is not None, f"no payroll_items row for {year_month}"
    return row['unpaid_leave_days']



# ── MATERNITY: the 45-day employer-paid cap across months ────────────────────

def test_maternity_day_45_is_still_paid(tmp_db_conn_hr_clean):
    """Exactly 45 days (1 Jan – 14 Feb) is the whole paid entitlement — nothing
    unpaid in either month."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_45', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-02-14', 45)

    assert _unpaid(conn, '2026-01', eid) == 0
    assert _unpaid(conn, '2026-02', eid) == 0


def test_maternity_day_46_becomes_unpaid(tmp_db_conn_hr_clean):
    """One day past the cap (1 Jan – 15 Feb = 46 days): January is untouched,
    February carries the single unpaid day."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_46', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-02-15', 46)

    assert _unpaid(conn, '2026-01', eid) == 0
    assert _unpaid(conn, '2026-02', eid) == 1


def test_full_98_day_maternity_allocates_unpaid_to_the_right_months(
        tmp_db_conn_hr_clean):
    """1 Jan – 8 Apr = 98 days (Jan 31, Feb 28, Mar 31, Apr 8).

    Days 1-45 are paid, so the cap is crossed inside February: Feb owns days
    32-59, of which 46-59 (14 days) are unpaid. March and April are wholly
    past the cap. 14 + 31 + 8 = 53 = 98 − 45.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_98', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-04-08', 98)

    months = {ym: _unpaid(conn, ym, eid)
              for ym in ('2026-01', '2026-02', '2026-03', '2026-04')}

    assert months == {'2026-01': 0, '2026-02': 14, '2026-03': 31, '2026-04': 8}
    assert round(sum(months.values()), 4) == 53


def test_maternity_cap_counts_across_a_year_boundary(tmp_db_conn_hr_clean):
    """15 Nov 2026 – 20 Feb 2027 = 98 days. The count must NOT restart at
    1 January: December crosses the cap (days 46-47), and January 2027 is
    wholly unpaid."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_XY', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-11-15', '2027-02-20', 98)

    assert _unpaid(conn, '2026-11', eid) == 0     # days 1-16
    assert _unpaid(conn, '2026-12', eid) == 2     # days 17-47 → 46,47 unpaid
    assert _unpaid(conn, '2027-01', eid) == 31    # days 48-78


def test_two_separate_maternity_leaves_each_get_their_own_cap(
        tmp_db_conn_hr_clean):
    """Two DISTINCT maternity leaves in one calendar year must not share a cap.

    46 days in January and another 46 in September is one unpaid day each — the
    second pregnancy is not paid out of the first one's 45 days. Sharing the cap
    across the year underpaid the second leave by 45 days of salary (measured:
    47 unpaid days instead of 2).

    Episodes are separated by the gap between consecutive maternity rows
    (Put, 2026-08-10: >30 days apart = a new leave). Thai maternity leave is 98
    days per pregnancy and two pregnancies are ~9 months apart, so the threshold
    only has to beat the largest gap inside one leave and lose to the smallest
    gap between two.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_TWO', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-02-15', 46)
    _add_leave(conn, eid, 'MATERNITY', '2026-09-01', '2026-10-16', 46)

    months = {ym: _unpaid(conn, ym, eid)
              for ym in ('2026-01', '2026-02', '2026-09', '2026-10')}

    assert months == {'2026-01': 0, '2026-02': 1, '2026-09': 0, '2026-10': 1}
    assert sum(months.values()) == 2, "one unpaid day per leave, not 47"


def test_one_leave_split_across_rows_still_shares_one_cap(
        tmp_db_conn_hr_clean):
    """The case the gap rule must NOT break: a single 98-day maternity leave
    entered month by month is still ONE episode with ONE 45-day cap.

    Per-row caps would give every row its own 45 and nothing would ever be
    unpaid — the original defect in a new form.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_SPLIT', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-01-31', 31)
    _add_leave(conn, eid, 'MATERNITY', '2026-02-01', '2026-02-28', 28)
    _add_leave(conn, eid, 'MATERNITY', '2026-03-01', '2026-03-31', 31)
    _add_leave(conn, eid, 'MATERNITY', '2026-04-01', '2026-04-08', 8)

    months = {ym: _unpaid(conn, ym, eid)
              for ym in ('2026-01', '2026-02', '2026-03', '2026-04')}

    assert months == {'2026-01': 0, '2026-02': 14, '2026-03': 31, '2026-04': 8}
    assert round(sum(months.values()), 4) == 53, "= 98 - 45, one shared cap"


def test_maternity_episode_still_spans_a_year_boundary(tmp_db_conn_hr_clean):
    """The gap rule must not resurrect the year-boundary bug: two adjacent rows
    either side of 31 December are still one episode."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_XYS', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-11-15', '2026-12-31', 47)
    _add_leave(conn, eid, 'MATERNITY', '2027-01-01', '2027-02-20', 51)

    assert _unpaid(conn, '2026-11', eid) == 0     # days 1-16
    assert _unpaid(conn, '2026-12', eid) == 2     # days 17-47 → 46,47 unpaid
    assert _unpaid(conn, '2027-01', eid) == 31    # wholly past the cap


def test_maternity_regeneration_is_stable(tmp_db_conn_hr_clean):
    """Regenerating a draft must not accumulate — the allocation is derived
    from the leave rows, not from the previous run."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'M_RE', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-04-08', 98)

    first = [_unpaid(conn, ym, eid) for ym in ('2026-02', '2026-03')]
    again = [_unpaid(conn, ym, eid) for ym in ('2026-02', '2026-03')]
    third = [_unpaid(conn, ym, eid) for ym in ('2026-02', '2026-03')]

    assert first == again == third == [14, 31]


# ── Annual quota: the excess is charged once, chronologically ────────────────

def test_two_month_sick_excess_is_charged_once(tmp_db_conn_hr_clean):
    """20 sick days in January + 20 in February against a 30-day quota.

    The 30-day allowance is consumed by January (20) and the first 10 of
    February, so only February's last 10 days are unpaid. The total across
    both months must be 10 — the old code charged the full annual `over` of 10
    to BOTH months (20 days deducted for a 10-day excess).
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_2M', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-01-05', '2026-01-24', 20)
    _add_leave(conn, eid, 'SICK', '2026-02-05', '2026-02-24', 20)

    jan = _unpaid(conn, '2026-01', eid)
    feb = _unpaid(conn, '2026-02', eid)

    assert jan == 0, "January is inside the quota — nothing unpaid"
    assert feb == 10, "February carries the 10 days that crossed the quota"
    assert jan + feb == 10, "the excess must be charged exactly once"


def test_two_month_excess_regeneration_is_stable(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_RE', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-01-05', '2026-01-24', 20)
    _add_leave(conn, eid, 'SICK', '2026-02-05', '2026-02-24', 20)

    first = (_unpaid(conn, '2026-01', eid), _unpaid(conn, '2026-02', eid))
    again = (_unpaid(conn, '2026-01', eid), _unpaid(conn, '2026-02', eid))

    assert first == again == (0, 10)


def test_year_total_equals_leave_balance_over(tmp_db_conn_hr_clean):
    """The reconciling invariant: what the year's payroll months deduct between
    them must equal the single `over` figure /hr shows for that year.

    The hard case is a leave straddling 31 December: 25 sick days in November
    (inside the 30-day quota) plus a 20-day leave running 20 Dec – 8 Jan. The
    quota is crossed inside that second leave, so 7 unpaid days fall in
    December and the remaining 8 fall in January — in the months they were
    actually taken, even though January belongs to the next calendar year.

    Two ways to get this wrong, both measured on the way here:
      - population = requests STARTING in the payroll year → January 2027 never
        sees the December-started request, so 8 of the 15 days are silently
        never deducted by anyone;
      - folding that spill back onto December → the total is right but 8 days
        are docked from a month in which the employee was still inside quota.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_SPILL', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-11-01', '2026-11-25', 25)   # inside 30
    _add_leave(conn, eid, 'SICK', '2026-12-20', '2027-01-08', 20)   # crosses NY

    over = hr.leave_balance(eid, 2026, conn=conn)['SICK']['over']
    assert over == 15, "45 used against a 30-day quota"

    nov = _unpaid(conn, '2026-11', eid)
    dec = _unpaid(conn, '2026-12', eid)
    jan = _unpaid(conn, '2027-01', eid)

    assert (nov, dec, jan) == (0, 7, 8), (
        f"expected the excess in the months it was taken, got "
        f"nov={nov} dec={dec} jan={jan}")
    assert nov + dec + jan == over, (
        f"the cohort deducted {nov + dec + jan} of its {over}-day excess")
    assert _unpaid(conn, '2027-02', eid) == 0, "and nothing may leak further"


def test_next_year_quota_is_not_consumed_by_the_straddling_leave(
        tmp_db_conn_hr_clean):
    """The other half of the cohort rule: a leave that started last year is
    judged against LAST year's quota, so it must not eat into this year's.

    ⚠ The 2027 leave is 25 days, not 20, and that is what makes this test able
    to fail. The straddler puts 8 days into January 2027. If those wrongly
    counted against 2027's 30-day quota, 25 + 8 = 33 → 3 unpaid days in March.
    At 20 days the sum would be 28, inside the quota either way, so the test
    would pass under a design that leaks and under one that does not.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_COHORT', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-11-01', '2026-11-25', 25)
    _add_leave(conn, eid, 'SICK', '2026-12-20', '2027-01-08', 20)
    # A fresh 2027 leave: inside 2027's own quota (25 <= 30) but NOT inside it
    # once the straddler's 8 January days are wrongly added.
    _add_leave(conn, eid, 'SICK', '2027-03-01', '2027-03-25', 25)

    assert hr.leave_balance(eid, 2027, conn=conn)['SICK']['used'] == 25
    assert _unpaid(conn, '2027-03', eid) == 0, (
        "2027's own quota is untouched by the 2026 cohort")


def test_exact_quota_produces_no_phantom_deduction(tmp_db_conn_hr_clean):
    """Using EXACTLY the quota must deduct nothing.

    Per-month portions are rounded to 4dp, so a request straddling months can
    sum to quota + 2e-4 and trip a bare `excess > 0`. Measured before the
    epsilon: 0.0001 unpaid days, ฿0.05 deducted, and a payslip note reading
    "PERSONAL เกินสิทธิ 0.0001 วัน".
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_EXACT', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-01', '2026-03-04', 6)  # quota 6

    for ym in ('2026-01', '2026-02', '2026-03'):
        assert _unpaid(conn, ym, eid) == 0, f"{ym} invented a deduction"
        run = hr.generate_run(ym, 1, created_by=1, conn=conn)
        note = conn.execute(
            "SELECT note FROM payroll_items WHERE run_id=? AND employee_id=?",
            (run['id'], eid)).fetchone()['note']
        assert not note or 'เกินสิทธิ' not in note, f"{ym} note: {note}"


def test_malformed_legacy_dates_do_not_break_payroll_generation(
        tmp_db_conn_hr_clean):
    """A row with a non-ISO date raises inside `_to_date`, so payroll
    generation for the WHOLE company dies unless it is caught. The write layer
    refuses such rows now, but rows predating it (or written directly) exist.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_BAD', '2024-01-01')
    conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, '10/03/2026', '2026-13-40', 3, 'approved')""",
        (eid, _leave_type_id(conn, 'SICK')))
    conn.commit()

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)

    assert run is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM payroll_items WHERE run_id=?", (run['id'],)
    ).fetchone()[0] > 0, "every other employee must still be paid"
    assert _unpaid(conn, '2026-03', eid) == 0


def test_quota_resets_at_the_year_boundary(tmp_db_conn_hr_clean):
    """20 sick days in December 2026 + 20 in January 2027 is 20 against each
    year's own 30-day quota — no excess in either month."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_YB', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-12-05', '2026-12-24', 20)
    _add_leave(conn, eid, 'SICK', '2027-01-05', '2027-01-24', 20)

    assert _unpaid(conn, '2026-12', eid) == 0
    assert _unpaid(conn, '2027-01', eid) == 0


def test_excess_note_reports_the_month_share_not_the_annual_total(
        tmp_db_conn_hr_clean):
    """The payslip note must describe what THIS month was actually deducted.

    January is the discriminating case: the employee IS over quota for the year
    (annual `over` = 10), but January itself deducted nothing — so January's
    payslip must not carry an over-quota note at all.
    """
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_NOTE', '2024-01-01')
    _add_leave(conn, eid, 'SICK', '2026-01-05', '2026-01-24', 20)
    _add_leave(conn, eid, 'SICK', '2026-02-05', '2026-02-24', 20)

    def _note(ym):
        run = hr.generate_run(ym, 1, created_by=1, conn=conn)
        return conn.execute(
            "SELECT note FROM payroll_items WHERE run_id=? AND employee_id=?",
            (run['id'], eid),
        ).fetchone()['note']

    jan = _note('2026-01')
    assert not jan or 'เกินสิทธิ' not in jan, (
        "January deducted nothing — it must not claim an over-quota deduction")

    feb = _note('2026-02')
    assert feb is not None and 'เกินสิทธิ' in feb
    assert '10' in feb


def test_single_month_excess_is_unchanged(tmp_db_conn_hr_clean):
    """Regression guard: the ordinary case (all the leave in one month) must
    keep deducting exactly the excess in that month."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_1M', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-03-01', '2026-03-08', 8)   # quota 6

    assert _unpaid(conn, '2026-03', eid) == 2


def test_unpaid_type_leave_still_deducts_in_its_own_month(
        tmp_db_conn_hr_clean):
    """Regression guard: UNPAID-type leave is unconditionally unpaid and is not
    routed through the allowance walk at all."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'Q_UNP', '2024-01-01')
    _add_leave(conn, eid, 'UNPAID', '2026-03-10', '2026-03-11', 2)

    assert _unpaid(conn, '2026-03', eid) == 2
    assert _unpaid(conn, '2026-04', eid) == 0
