"""อนุโลม write paths: hr.set_pay_waiver, POST /hr/leave/<id>/waive, and the
rule that a generic edit of who/what/when clears the flag.

Every refusal asserts the DB is unchanged; every allowed call asserts a change
only the route could make (a 302 alone proves nothing).
"""
import json
import os
import sqlite3

import pytest

import hr


# ── helpers ──────────────────────────────────────────────────────────────────

def _connect(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _mk_employee(c, emp_code, company_id=1):
    cur = c.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'F', ?, '2024-01-01', 90, 0, 0, 1)""",
        (emp_code, emp_code, company_id),
    )
    eid = cur.lastrowid
    c.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2024-01-01', 15000, 'initial')""", (eid,))
    c.commit()
    return eid


def _add_leave(c, eid, start='2026-03-02', end='2026-03-02', days=1,
               status='approved', code='PERSONAL', waived=0):
    lt = c.execute("SELECT id FROM leave_types WHERE code=?", (code,)).fetchone()[0]
    cur = c.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days, status,
              reason, pay_waived)
           VALUES (?, ?, ?, ?, ?, ?, 'r', ?)""",
        (eid, lt, start, end, days, status, waived))
    c.commit()
    return cur.lastrowid


def _plant_run(c, year_month, status, company_id=1):
    cur = c.execute(
        """INSERT INTO payroll_runs (year_month, company_id, status, run_date)
           VALUES (?, ?, ?, date('now'))""", (year_month, company_id, status))
    c.commit()
    return cur.lastrowid


def _waived(db, rid):
    c = _connect(db)
    try:
        return c.execute("SELECT pay_waived FROM leave_requests WHERE id=?",
                         (rid,)).fetchone()[0]
    finally:
        c.close()


def _max_audit(db):
    c = _connect(db)
    try:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
    finally:
        c.close()


def _audit_since(db, since, rid):
    c = _connect(db)
    try:
        return [(json.loads(r["changed_fields"]), r["user"]) for r in c.execute(
            "SELECT changed_fields, user FROM audit_log WHERE id > ? AND "
            "table_name='leave_requests' AND row_id=? ORDER BY id", (since, rid))]
    finally:
        c.close()


def _client(role, db, user_id=99):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    cl = a.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return cl


def _flashes(cl):
    with cl.session_transaction() as s:
        return [m for _cat, m in s.get('_flashes', [])]


@pytest.fixture
def db(tmp_db):
    c = _connect(tmp_db)
    # Force the state: no payroll runs for the months these tests touch.
    c.execute("DELETE FROM payroll_items WHERE run_id IN (SELECT id FROM "
              "payroll_runs WHERE year_month IN ('2026-02','2026-03','2026-04'))")
    c.execute("DELETE FROM payroll_runs WHERE year_month IN "
              "('2026-02','2026-03','2026-04')")
    c.commit()
    c.close()
    return tmp_db


# ── 11. domain refusals ──────────────────────────────────────────────────────

def test_set_pay_waiver_turns_on_and_off_with_actor_audit(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WT_ON')
    rid = _add_leave(c, eid)
    c.close()

    since = _max_audit(db)
    hr.set_pay_waiver(rid, True, actor='boss')
    assert _waived(db, rid) == 1
    rows = _audit_since(db, since, rid)
    assert ({'pay_waived': [0, 1]}, None) in rows, "the trigger saw it"
    assert any(u == 'boss' for _f, u in rows), "and the actor is recorded"

    hr.set_pay_waiver(rid, False, actor='boss')
    assert _waived(db, rid) == 0


@pytest.mark.parametrize('status', ['pending', 'rejected', 'cancelled'])
def test_toggle_refused_unless_approved(db, status):
    c = _connect(db)
    eid = _mk_employee(c, f'WT_{status}')
    rid = _add_leave(c, eid, status=status)
    c.close()
    since = _max_audit(db)
    with pytest.raises(ValueError):
        hr.set_pay_waiver(rid, True, actor='boss')
    assert _waived(db, rid) == 0
    assert _audit_since(db, since, rid) == []


@pytest.mark.parametrize('closed_month', ['2026-02', '2026-03'])
def test_toggle_refused_on_a_finalized_month(db, closed_month):
    """A request straddling Feb/Mar is refused when EITHER month is closed —
    the end month too, not only the start month."""
    c = _connect(db)
    eid = _mk_employee(c, f'WT_FIN_{closed_month}')
    rid = _add_leave(c, eid, start='2026-02-27', end='2026-03-03', days=5)
    _plant_run(c, closed_month, 'finalized')
    c.close()
    since = _max_audit(db)
    with pytest.raises(ValueError, match='เดือนนี้ปิดรอบแล้ว ต้อง reopen ก่อน'):
        hr.set_pay_waiver(rid, True, actor='boss')
    assert _waived(db, rid) == 0
    assert _audit_since(db, since, rid) == []


def test_toggle_ignores_another_companys_finalized_run(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WT_CO')
    rid = _add_leave(c, eid)
    other = c.execute("SELECT id FROM companies WHERE id <> 1 LIMIT 1").fetchone()
    if other is None:
        c.close()
        pytest.skip("no second company in this DB")
    _plant_run(c, '2026-03', 'finalized', company_id=other[0])
    c.close()
    hr.set_pay_waiver(rid, True, actor='boss')
    assert _waived(db, rid) == 1


def test_toggle_on_a_draft_month_reports_it(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WT_DRAFT')
    rid = _add_leave(c, eid)
    _plant_run(c, '2026-03', 'draft')
    c.close()
    draft_months = hr.set_pay_waiver(rid, True, actor='boss')
    assert draft_months == ['2026-03']


# ── 10. route matrix ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('role', ['admin', 'manager'])
def test_route_allowed_roles_change_the_db(db, role):
    c = _connect(db)
    eid = _mk_employee(c, f'WR_{role}')
    rid = _add_leave(c, eid)
    c.close()
    cl = _client(role, db)
    r = cl.post(f'/hr/leave/{rid}/waive', data={'on': '1'})
    assert r.status_code == 302
    assert _waived(db, rid) == 1
    r = cl.post(f'/hr/leave/{rid}/waive', data={'on': '0'})
    assert r.status_code == 302
    assert _waived(db, rid) == 0


def test_route_flash_on_a_draft_month_says_regenerate(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WR_DRAFT')
    rid = _add_leave(c, eid)
    _plant_run(c, '2026-03', 'draft')
    c.close()
    cl = _client('admin', db)
    cl.post(f'/hr/leave/{rid}/waive', data={'on': '1'})
    assert any('สร้างรอบใหม่' in m for m in _flashes(cl)), _flashes(cl)


def test_route_refusal_is_flashed_not_crashed(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WR_PEND')
    rid = _add_leave(c, eid, status='pending')
    c.close()
    cl = _client('admin', db)
    r = cl.post(f'/hr/leave/{rid}/waive', data={'on': '1'})
    assert r.status_code == 302
    assert _waived(db, rid) == 0
    assert _flashes(cl), "the refusal must say something"


# Recorded 2026-09-29 against the real gate (permissions.py + require_login);
# identical to what hr.leave_approve answers the same roles.
REFUSED = {
    'staff': (302, 'ไม่มีสิทธิ์เข้าถึงระบบบุคลากร'),
    'general': (302, None),         # bounced to /m/stock without a word (SILENT)
    'shareholder': (403, None),     # deny=FORBID
}


@pytest.mark.parametrize('role', sorted(REFUSED))
def test_route_refused_roles_exact_answer(db, role):
    c = _connect(db)
    eid = _mk_employee(c, f'WX_{role}')
    rid = _add_leave(c, eid)
    c.close()
    cl = _client(role, db)
    r = cl.post(f'/hr/leave/{rid}/waive', data={'on': '1'})
    assert (r.status_code, (_flashes(cl) or [None])[0]) == REFUSED[role]
    assert _waived(db, rid) == 0


# ── 12. a generic edit clears the flag ───────────────────────────────────────

def _edit_form(c, rid, **over):
    r = c.execute("SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone()
    form = {
        'employee_id': str(r['employee_id']),
        'leave_type_id': str(r['leave_type_id']),
        'start_date': r['start_date'], 'end_date': r['end_date'],
        'days': str(r['days']), 'status': r['status'],
        'has_medical_cert': str(r['has_medical_cert']),
        'reason': r['reason'] or '',
    }
    form.update(over)
    return form


@pytest.mark.parametrize('field,value', [
    ('start_date', '2026-03-01'),
    ('end_date', '2026-03-03'),
    ('days', '0.5'),
    ('leave_type_id', 'ANNUAL'),
])
def test_admin_edit_of_who_what_when_clears_the_flag(db, field, value):
    c = _connect(db)
    eid = _mk_employee(c, f'WE_{field}')
    rid = _add_leave(c, eid, waived=1)
    if value == 'ANNUAL':
        value = str(c.execute(
            "SELECT id FROM leave_types WHERE code='ANNUAL'").fetchone()[0])
    form = _edit_form(c, rid, **{field: value})
    c.close()
    since = _max_audit(db)
    cl = _client('admin', db)
    r = cl.post(f'/hr/leave/{rid}/edit', data=form)
    assert r.status_code == 302
    assert _waived(db, rid) == 0
    assert any(f.get('pay_waived') == [1, 0]
               for f, _u in _audit_since(db, since, rid))


def test_edit_of_employee_clears_the_flag(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WE_EMP')
    other = _mk_employee(c, 'WE_EMP2')
    rid = _add_leave(c, eid, waived=1)
    data = dict(_edit_form(c, rid), employee_id=str(other))
    c.close()
    import hr_queries
    hr_queries.update_leave_request(rid, data)
    assert _waived(db, rid) == 0


def test_admin_edit_of_reason_only_keeps_the_flag(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WE_KEEP')
    rid = _add_leave(c, eid, waived=1)
    form = _edit_form(c, rid, reason='changed words only')
    c.close()
    cl = _client('admin', db)
    r = cl.post(f'/hr/leave/{rid}/edit', data=form)
    assert r.status_code == 302
    assert _waived(db, rid) == 1
    c = _connect(db)
    assert c.execute("SELECT reason FROM leave_requests WHERE id=?",
                     (rid,)).fetchone()[0] == 'changed words only'
    c.close()


# ── UI: badge + toggle on /hr/leave, read-only state on the edit form ────────

def _leave_page(db, role, eid):
    cl = _client(role, db)
    r = cl.get(f'/hr/leave?month=&employee_id={eid}')
    return r.status_code, r.get_data(as_text=True)


def _waive_form(rid):
    return f'action="/hr/leave/{rid}/waive"'


@pytest.mark.parametrize('role', ['admin', 'manager'])
def test_leave_page_shows_toggle_to_approvers(db, role):
    c = _connect(db)
    eid = _mk_employee(c, f'WU_{role}')
    rid = _add_leave(c, eid, waived=1)
    pending = _add_leave(c, eid, start='2026-03-09', end='2026-03-09',
                         status='pending')
    c.close()
    status, html = _leave_page(db, role, eid)
    assert status == 200
    assert _waive_form(rid) in html
    assert _waive_form(pending) not in html, "approved rows only"
    assert 'data-badge="waived"' in html


@pytest.mark.parametrize('role', ['shareholder', 'staff', 'general'])
def test_leave_page_hides_toggle_from_everyone_else(db, role):
    c = _connect(db)
    eid = _mk_employee(c, f'WV_{role}')
    rid = _add_leave(c, eid, waived=1)
    c.close()
    status, html = _leave_page(db, role, eid)
    assert _waive_form(rid) not in html
    if role == 'shareholder':
        assert status == 200, "shareholder reads the page, the control is what is gated"
        assert 'data-badge="waived"' in html


def test_admin_edit_form_shows_state_read_only_with_hint(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WF_ADM')
    rid = _add_leave(c, eid, waived=1)
    c.close()
    status, html = _leave_page(db, 'admin', eid)
    assert status == 200
    assert 'data-waiver-state="1"' in html
    assert 'จะยกเลิกอนุโลมอัตโนมัติ' in html
    assert 'name="pay_waived"' not in html, "the edit form must not write the flag"


def test_me_leave_shows_no_badge(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WM_ME')
    c.execute("UPDATE employees SET user_id=4242 WHERE id=?", (eid,))
    _add_leave(c, eid, waived=1)
    c.commit()
    c.close()
    cl = _client('general', db, user_id=4242)
    r = cl.get('/me/leave')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert '03/2569' in html or '2026-03-02' in html or '02/03' in html, \
        "control: the waived request is on the page"
    assert 'อนุโลม' not in html


# ── status changes clear it too (Put: approved-only; a waiver never returns) ──

@pytest.mark.parametrize('new_status', ['pending', 'rejected', 'cancelled'])
def test_admin_edit_of_status_clears_the_flag(db, new_status):
    c = _connect(db)
    eid = _mk_employee(c, f'WS_{new_status}')
    rid = _add_leave(c, eid, waived=1)
    form = _edit_form(c, rid, status=new_status)
    c.close()
    since = _max_audit(db)
    cl = _client('admin', db)
    assert cl.post(f'/hr/leave/{rid}/edit', data=form).status_code == 302
    assert _waived(db, rid) == 0
    assert any(f.get('pay_waived') == [1, 0]
               for f, _u in _audit_since(db, since, rid))


def test_partial_status_write_clears_the_flag(db):
    """The partial branch (approve/reject/cancel) as a generic writer."""
    c = _connect(db)
    eid = _mk_employee(c, 'WS_PART')
    rid = _add_leave(c, eid, waived=1)
    c.close()
    since = _max_audit(db)
    import hr_queries
    hr_queries.update_leave_request(rid, {'status': 'cancelled'})
    assert _waived(db, rid) == 0
    assert any(f.get('pay_waived') == [1, 0]
               for f, _u in _audit_since(db, since, rid))


def test_partial_write_without_status_change_keeps_the_flag(db):
    c = _connect(db)
    eid = _mk_employee(c, 'WS_SAME')
    rid = _add_leave(c, eid, waived=1)
    c.close()
    import hr_queries
    hr_queries.update_leave_request(rid, {'status': 'approved', 'approved_by': 'x'})
    assert _waived(db, rid) == 1
