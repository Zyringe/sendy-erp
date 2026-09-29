"""อนุโลม — a waived (pay_waived = 1) approved leave request is not deducted.

Spec: projects leave-pay-waiver (decision log 2026-09-28, spec v2 2026-09-29).
Only the SALARY deduction is forgiven. The request still consumes its
allowance share in date order, still shows in `leave_balance`, and still
forfeits เบี้ยขยัน when its type has `affects_diligence = 1`.

These pin the allocator in `hr._compute_unpaid_days`: waived units still
advance `used`, only non-waived units add excess to the month, and the waiver
note states the days forgiven IN THIS MONTH.
"""
import pytest

import hr


# ── helpers (duplicated per file, the suite's convention) ────────────────────

def _leave_type_id(conn, code):
    return conn.execute(
        "SELECT id FROM leave_types WHERE code=?", (code,)
    ).fetchone()[0]


def _mk_employee(conn, emp_code, start_date, monthly_salary=15000.0,
                 diligence_allowance=0, gender='F'):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, ?, 1, ?, 90, 0, ?, 1)""",
        (emp_code, emp_code, gender, start_date, diligence_allowance),
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


def _add_leave(conn, employee_id, code, start, end, days, waived=0):
    cur = conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status,
              pay_waived)
           VALUES (?, ?, ?, ?, ?, 'approved', ?)""",
        (employee_id, _leave_type_id(conn, code), start, end, days, waived),
    )
    conn.commit()
    return cur.lastrowid


def _set_waived(conn, req_id, on):
    conn.execute("UPDATE leave_requests SET pay_waived=? WHERE id=?",
                 (1 if on else 0, req_id))
    conn.commit()


def _calc(conn, eid, ym):
    """(unpaid_days, notes) straight from the engine, no run needed."""
    start, end = hr._month_bounds(ym)
    return hr._compute_unpaid_days(conn, eid, ym, start, end)


def _unpaid(conn, eid, ym):
    return _calc(conn, eid, ym)[0]


def _waiver_notes(notes):
    return [n for n in notes if n.startswith('อนุโลม')]


# ── 1. over-quota ANNUAL, waived ─────────────────────────────────────────────

def test_waived_over_quota_annual_deducts_nothing_but_keeps_balance_and_diligence(
        tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    # The migrations seed affects_diligence = 0 on some types; force it here so
    # the diligence half of the test can fail.
    conn.execute("UPDATE leave_types SET affects_diligence=1 WHERE code='ANNUAL'")
    conn.commit()
    eid = _mk_employee(conn, 'W_ANN', '2024-01-01', diligence_allowance=500)
    assert hr.leave_balance(eid, 2026, conn=conn)['ANNUAL']['entitlement'] == 6
    _add_leave(conn, eid, 'ANNUAL', '2026-01-05', '2026-01-10', 6)
    rid = _add_leave(conn, eid, 'ANNUAL', '2026-03-02', '2026-03-02', 1)

    # Control: without the waiver the day past quota is deducted.
    assert _unpaid(conn, eid, '2026-03') == 1

    _set_waived(conn, rid, True)
    days, notes = _calc(conn, eid, '2026-03')
    assert days == 0
    assert _waiver_notes(notes) == ['อนุโลม ANNUAL 02/03 ไม่หัก 1 วัน']
    assert not any('เกินสิทธิ' in n for n in notes)

    assert hr.leave_balance(eid, 2026, conn=conn)['ANNUAL']['over'] == 1

    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    item = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchone()
    assert item['unpaid_leave_days'] == 0
    assert item['unpaid_leave_deduction'] == 0
    assert item['diligence_forfeited'] == 1
    assert 'อนุโลม ANNUAL 02/03 ไม่หัก 1 วัน' in (item['note'] or '')


# ── 2. waived UNPAID-type request ────────────────────────────────────────────

def test_waived_unpaid_type_request_deducts_nothing(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_UNP', '2024-01-01')
    _add_leave(conn, eid, 'UNPAID', '2026-03-10', '2026-03-11', 2, waived=1)
    _add_leave(conn, eid, 'UNPAID', '2026-03-20', '2026-03-20', 1)

    days, notes = _calc(conn, eid, '2026-03')
    assert days == 1, "only the non-waived UNPAID day is deducted"
    assert _waiver_notes(notes) == ['อนุโลม UNPAID 10/03 ไม่หัก 2 วัน']


# ── 3. waived inside the allowance, then a non-waived crosser ────────────────

def test_waived_inside_allowance_does_not_change_the_later_crosser(
        tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_CHR', '2024-01-01')
    rid = _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-08', 4)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-02', '2026-02-04', 3)

    before = (_calc(conn, eid, '2026-01'), _calc(conn, eid, '2026-02'))
    assert before[1][0] == 1

    _set_waived(conn, rid, True)
    after = (_calc(conn, eid, '2026-01'), _calc(conn, eid, '2026-02'))
    assert after == before, "a waiver with nothing past quota changes nothing"


# ── 4. waived crosser, then a non-waived request ─────────────────────────────

def test_waived_crosser_forgives_only_itself(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_CRS', '2024-01-01')
    rid = _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-11', 7)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-02', '2026-02-03', 2)

    assert (_unpaid(conn, eid, '2026-01'), _unpaid(conn, eid, '2026-02')) == (1, 2)

    _set_waived(conn, rid, True)
    jan_days, jan_notes = _calc(conn, eid, '2026-01')
    feb_days, feb_notes = _calc(conn, eid, '2026-02')
    assert (jan_days, feb_days) == (0, 2)
    assert _waiver_notes(jan_notes) == ['อนุโลม PERSONAL 05/01 ไม่หัก 1 วัน']
    assert _waiver_notes(feb_notes) == []
    assert any('เกินสิทธิ 2 วัน' in n for n in feb_notes)


# ── 5. MATERNITY cap, episode split across rows ──────────────────────────────

def test_waived_maternity_row_inside_a_split_episode(tmp_db_conn_hr_clean):
    """One 80-day leave in three rows, sharing ONE 45-day cap. The middle row
    is waived: it still eats the cap, and its past-cap days are forgiven."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_MAT', '2024-01-01')
    _add_leave(conn, eid, 'MATERNITY', '2026-01-01', '2026-01-31', 31)
    mid = _add_leave(conn, eid, 'MATERNITY', '2026-02-01', '2026-03-10', 38)
    _add_leave(conn, eid, 'MATERNITY', '2026-03-11', '2026-03-20', 10)

    plain = {ym: _unpaid(conn, eid, ym) for ym in ('2026-01', '2026-02', '2026-03')}
    assert plain == {'2026-01': 0, '2026-02': 14, '2026-03': 20}

    _set_waived(conn, mid, True)
    waived = {ym: _calc(conn, eid, ym) for ym in ('2026-01', '2026-02', '2026-03')}
    assert {ym: v[0] for ym, v in waived.items()} == {
        '2026-01': 0, '2026-02': 0, '2026-03': 10}
    assert _waiver_notes(waived['2026-02'][1]) == ['อนุโลม MATERNITY 01/02 ไม่หัก 14 วัน']
    assert _waiver_notes(waived['2026-03'][1]) == ['อนุโลม MATERNITY 01/02 ไม่หัก 10 วัน']


# ── 6. half days ─────────────────────────────────────────────────────────────

def test_half_day_waived_crosser(tmp_db_conn_hr_clean):
    """5.5 used, then a waived 1-day request: 0.5 fits, 0.5 is forgiven. A
    later non-waived half-day is wholly past quota and deducted."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_HALF', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-09', 5)
    _add_leave(conn, eid, 'PERSONAL', '2026-01-12', '2026-01-12', 0.5)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-02', '2026-02-02', 1, waived=1)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-03', '2026-02-03', 0.5)

    days, notes = _calc(conn, eid, '2026-02')
    assert days == 0.5
    assert _waiver_notes(notes) == ['อนุโลม PERSONAL 02/02 ไม่หัก 0.5 วัน']


def test_waived_half_day_still_consumes_quota(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_HALF2', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-09', 5)
    _add_leave(conn, eid, 'PERSONAL', '2026-01-12', '2026-01-12', 0.5)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-02', '2026-02-02', 0.5, waived=1)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-03', '2026-02-03', 0.5)

    days, notes = _calc(conn, eid, '2026-02')
    assert days == 0.5, "the waived half-day used the last of the quota"
    assert _waiver_notes(notes) == [], "nothing of the waived half-day was past quota"


# ── 7. straddling 31 December ────────────────────────────────────────────────

def test_waived_new_year_straddler(tmp_db_conn_hr_clean):
    """Counts against 2026's quota; its excess falls in January 2027 and is
    forgiven there. 2027's own quota stays untouched."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_NY', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-03-02', '2026-03-07', 6)
    rid = _add_leave(conn, eid, 'PERSONAL', '2026-12-30', '2027-01-02', 4)
    _add_leave(conn, eid, 'PERSONAL', '2027-01-11', '2027-01-16', 6)

    assert (_unpaid(conn, eid, '2026-12'), _unpaid(conn, eid, '2027-01')) == (2, 2)

    _set_waived(conn, rid, True)
    dec_days, dec_notes = _calc(conn, eid, '2026-12')
    jan_days, jan_notes = _calc(conn, eid, '2027-01')
    assert (dec_days, jan_days) == (0, 0)
    assert _waiver_notes(dec_notes) == ['อนุโลม PERSONAL 30/12 ไม่หัก 2 วัน']
    assert _waiver_notes(jan_notes) == ['อนุโลม PERSONAL 30/12 ไม่หัก 2 วัน']
    assert hr.leave_balance(eid, 2026, conn=conn)['PERSONAL']['over'] == 4
    assert hr.leave_balance(eid, 2027, conn=conn)['PERSONAL']['over'] == 0


# ── 8. month straddler: each month's note carries its own days ───────────────

def test_month_straddler_note_per_month(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_MS', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-10', 6)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-25', '2026-03-04', 8, waived=1)

    feb_days, feb_notes = _calc(conn, eid, '2026-02')
    mar_days, mar_notes = _calc(conn, eid, '2026-03')
    assert (feb_days, mar_days) == (0, 0)
    assert _waiver_notes(feb_notes) == ['อนุโลม PERSONAL 25/02 ไม่หัก 4 วัน']
    assert _waiver_notes(mar_notes) == ['อนุโลม PERSONAL 25/02 ไม่หัก 4 วัน']


# ── 9. no 1e-4 residue ───────────────────────────────────────────────────────

def test_no_residue_from_a_fractional_waived_straddler(tmp_db_conn_hr_clean):
    """7 days over a 33-day span splits into 7*3/33, 7*28/33, 7*2/33 — none
    terminate. Subtracting a rounded waived part from a total would leave a
    1e-4 residue; the allocator must never compute it that way."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_RES', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-10', 6)
    _add_leave(conn, eid, 'PERSONAL', '2026-01-29', '2026-03-02', 7, waived=1)
    _add_leave(conn, eid, 'PERSONAL', '2026-03-10', '2026-03-10', 1)

    assert _unpaid(conn, eid, '2026-01') == 0
    assert _unpaid(conn, eid, '2026-02') == 0
    days, notes = _calc(conn, eid, '2026-03')
    assert days == 1
    assert not any('0.000' in n for n in notes), notes


def test_no_residue_when_waived_and_deducted_excess_share_a_month(
        tmp_db_conn_hr_clean):
    """Both excesses fractional, in the same month: the waived straddler puts
    1 * 1/10 = 0.1 past quota into March, the deducted one 3 * 23/32 =
    2.15625. "Charge everything, then subtract the rounded waived part"
    lands on 2.1563; charging only non-waived units gives round(69/32, 4)."""
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_RES2', '2024-01-01')
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-10', 6)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-20', '2026-03-01', 1, waived=1)
    _add_leave(conn, eid, 'PERSONAL', '2026-02-20', '2026-03-23', 3)

    days, notes = _calc(conn, eid, '2026-03')
    assert days == round(3 * 23 / 32, 4) == 2.1562
    assert _waiver_notes(notes) == ['อนุโลม PERSONAL 20/02 ไม่หัก 0.1 วัน']
