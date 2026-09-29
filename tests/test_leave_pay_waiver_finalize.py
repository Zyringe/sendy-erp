"""อนุโลม at the money boundary: finalize_run's two guards, and the EMP005
cutover end to end.

1. Stale unpaid leave — a HARD refusal. `unpaid_leave_days` is computed at
   generate time; a waiver toggled (or leave approved) afterwards leaves the
   draft wrong. Only a regenerate fixes it, so there is no confirm.
2. Double pay — a confirmable warning. The same forgiveness keyed twice: a
   hand-typed `other_additions` noted อนุโลม AND a waived request.
"""
import math
from datetime import date, timedelta

import pytest

import hr


def _leave_type_id(conn, code):
    return conn.execute("SELECT id FROM leave_types WHERE code=?",
                        (code,)).fetchone()[0]


def _mk_employee(conn, emp_code, monthly_salary=15000.0):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, nickname, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, ?, 'F', 1, '2024-01-01', 90, 0, 0, 1)""",
        (emp_code, emp_code, emp_code),
    )
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2024-01-01', ?, 'initial')""", (eid, monthly_salary))
    conn.commit()
    return eid


def _add_leave(conn, eid, code, start, end, days, status='approved'):
    cur = conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (eid, _leave_type_id(conn, code), start, end, days, status))
    conn.commit()
    return cur.lastrowid


def _item(conn, run_id, eid):
    return conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, eid)).fetchone()


def _status(conn, run_id):
    return conn.execute("SELECT status FROM payroll_runs WHERE id=?",
                        (run_id,)).fetchone()[0]


def _over_quota_setup(conn, code='W_STALE'):
    """PERSONAL quota 6 used, then a 1-day request past it in March."""
    eid = _mk_employee(conn, code)
    _add_leave(conn, eid, 'PERSONAL', '2026-01-05', '2026-01-10', 6)
    rid = _add_leave(conn, eid, 'PERSONAL', '2026-03-02', '2026-03-02', 1)
    return eid, rid


# ── 13. stale unpaid leave ───────────────────────────────────────────────────

def test_finalize_refuses_after_a_toggle_without_regenerate(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid, rid = _over_quota_setup(conn)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    assert _item(conn, run['id'], eid)['unpaid_leave_days'] == 1

    hr.set_pay_waiver(rid, True, actor='boss', conn=conn)

    with pytest.raises(hr.StaleUnpaidLeaveError, match='ต้องสร้างรอบใหม่ก่อน') as e:
        hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert 'W_STALE' in str(e.value)
    assert _status(conn, run['id']) == 'draft'
    assert hr.unpaid_leave_stale_note(run['id'], conn=conn) is not None

    hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    assert hr.unpaid_leave_stale_note(run['id'], conn=conn) is None
    hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert _status(conn, run['id']) == 'finalized'
    assert _item(conn, run['id'], eid)['unpaid_leave_days'] == 0


def test_finalize_refuses_after_leave_approved_post_generate(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'W_LATE')
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    _add_leave(conn, eid, 'UNPAID', '2026-03-10', '2026-03-10', 1)
    with pytest.raises(hr.StaleUnpaidLeaveError):
        hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert _status(conn, run['id']) == 'draft'


def test_finalize_unaffected_when_nothing_moved(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    _over_quota_setup(conn, 'W_CALM')
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert _status(conn, run['id']) == 'finalized'


# ── 14. double pay ───────────────────────────────────────────────────────────

def _key_manual_waiver(conn, run_id, eid, amount=500.0):
    item = _item(conn, run_id, eid)
    hr.update_payroll_item(item['id'], other_additions=amount,
                           other_additions_note='อนุโลม ลา 02/03', conn=conn)


def test_double_pay_blocks_without_tick_and_passes_with_it(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid, rid = _over_quota_setup(conn, 'W_DOUBLE')
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    _key_manual_waiver(conn, run['id'], eid)
    hr.set_pay_waiver(rid, True, actor='boss', conn=conn)
    hr.generate_run('2026-03', 1, created_by=1, conn=conn)   # keeps the manual row

    note = hr.waiver_double_pay_note(run['id'], conn=conn)
    assert note is not None and 'W_DOUBLE' in note

    with pytest.raises(hr.WaiverDoublePayWarning) as e:
        hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert 'W_DOUBLE' in str(e.value)
    assert _status(conn, run['id']) == 'draft'

    hr.finalize_run(run['id'], conn=conn, confirm_carry=True,
                    confirm_waiver=True)
    assert _status(conn, run['id']) == 'finalized'


def test_double_pay_silent_without_overlap(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    # A hand-keyed อนุโลม with no waived request …
    eid, _rid = _over_quota_setup(conn, 'W_HANDONLY')
    # … and a waived request with no hand-keyed อนุโลม.
    eid2, rid2 = _over_quota_setup(conn, 'W_FLAGONLY')
    hr.set_pay_waiver(rid2, True, actor='boss', conn=conn)
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    _key_manual_waiver(conn, run['id'], eid)

    assert hr.waiver_double_pay_note(run['id'], conn=conn) is None
    hr.finalize_run(run['id'], conn=conn, confirm_carry=True)
    assert _status(conn, run['id']) == 'finalized'


# ── 15. EMP005 cutover end to end ────────────────────────────────────────────

def test_emp005_cutover_on_a_draft(tmp_db_conn_hr_clean):
    """Put's 2026-09 case, rebuilt from the real EMP005 row: one ANNUAL day
    past quota, forgiven by hand as other_additions ฿433.33. Cutover: clear the
    hand-keyed row, waive the request, regenerate. Net must not move and the
    deduction itself must be 0 — asserted directly, so the net clamp and the
    advance clamp cannot mask a wrong deduction."""
    conn = tmp_db_conn_hr_clean
    emp = conn.execute(
        "SELECT id, sso_enrolled FROM employees WHERE emp_code='EMP005'").fetchone()
    if emp is None:
        pytest.skip("EMP005 not in this DB")
    eid = emp['id']
    conn.execute("UPDATE leave_types SET affects_diligence=1 WHERE code='ANNUAL'")
    conn.commit()

    ent = hr.leave_balance(eid, 2026, conn=conn)['ANNUAL']['entitlement']
    if ent > 0:
        start = date(2026, 8, 3)
        end = start + timedelta(days=math.ceil(ent) - 1)
        _add_leave(conn, eid, 'ANNUAL', start.isoformat(), end.isoformat(), ent)
    rid = _add_leave(conn, eid, 'ANNUAL', '2026-09-26', '2026-09-26', 1)
    conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name)"
        " VALUES (?, '2026-09-05', 5000, 'test')", (eid,))
    conn.commit()

    # The state Put is in today.
    run = hr.generate_run('2026-09', 1, created_by=1, conn=conn)
    item = _item(conn, run['id'], eid)
    assert item['unpaid_leave_deduction'] == 433.33
    hr.update_payroll_item(item['id'], other_additions=433.33,
                           other_additions_note='อนุโลม ลาพักร้อน 26/09',
                           conn=conn)
    net_before = _item(conn, run['id'], eid)['net_pay']
    assert net_before == 7350.00

    # 1. clear the hand-keyed row  2. waive  3. regenerate
    hr.update_payroll_item(item['id'], other_additions=0,
                           other_additions_note='', conn=conn)
    hr.set_pay_waiver(rid, True, actor='put', conn=conn)
    hr.generate_run('2026-09', 1, created_by=1, conn=conn)

    # 4. check
    after = _item(conn, run['id'], eid)
    assert after['unpaid_leave_deduction'] == 0
    assert after['other_additions'] == 0
    assert after['net_pay'] == 7350.00
    assert 'อนุโลม ANNUAL 26/09 ไม่หัก 1 วัน' in (after['note'] or '')
    assert hr.waiver_double_pay_note(run['id'], conn=conn) is None
    assert hr.unpaid_leave_stale_note(run['id'], conn=conn) is None


# ── route + run page wiring ──────────────────────────────────────────────────

def _admin_client():
    import os
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    cl = a.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return cl


def test_run_page_and_finalize_route_carry_both_guards(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid, rid = _over_quota_setup(conn, 'W_ROUTE')
    run = hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    _key_manual_waiver(conn, run['id'], eid)
    hr.set_pay_waiver(rid, True, actor='boss', conn=conn)
    cl = _admin_client()

    # Stale: banner on the page, hard refusal at the route.
    html = cl.get(f"/hr/payroll/{run['id']}").get_data(as_text=True)
    assert 'data-warn="stale-unpaid-leave"' in html
    cl.post(f"/hr/payroll/{run['id']}/finalize",
            data={'confirm_carry': '1', 'confirm_waiver': '1'})
    assert _status(conn, run['id']) == 'draft'

    hr.generate_run('2026-03', 1, created_by=1, conn=conn)
    html = cl.get(f"/hr/payroll/{run['id']}").get_data(as_text=True)
    assert 'data-warn="stale-unpaid-leave"' not in html
    assert 'data-warn="waiver-double-pay"' in html
    assert 'name="confirm_waiver"' in html

    cl.post(f"/hr/payroll/{run['id']}/finalize", data={'confirm_carry': '1'})
    assert _status(conn, run['id']) == 'draft', "no tick, no finalize"
    cl.post(f"/hr/payroll/{run['id']}/finalize",
            data={'confirm_carry': '1', 'confirm_waiver': '1'})
    assert _status(conn, run['id']) == 'finalized'
