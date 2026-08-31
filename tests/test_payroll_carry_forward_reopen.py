"""TDD — payroll carry-forward reopen invalidation guard (P1d:
`hr.py::_build_item`'s CHRONOLOGICAL FINALIZE invariant / CarryChronologyError,
`hr.py::reopen_run`'s `confirm_carry_break` + CarryConsumedWarning,
`blueprints/hr.py::payroll_reopen`'s route wiring, and the departing-employee
warning on `blueprints/hr.py::employee_edit`).

Trigger + design: projects/payroll-carry-forward/plan.md, section "### P1d"
— "the Codex blocker". Three independent pieces:

1. CHRONOLOGICAL FINALIZE invariant (`_build_item`): refuses — never
   silently falls back — when a run for an employee exists strictly between
   the carry source month and the target month and is NOT finalized. This is
   the rule that prevents a debt from being collected twice.
2. `reopen_run(confirm_carry_break=...)`: refuses to reopen a run whose
   carried_out a LATER finalized run already consumed, unless confirmed.
3. Departing-employee warning on `/hr/employees/<id>/edit`: a warning only
   (no collection logic) when an employee is deactivated / given an end_date
   while still owing money.

Every test seeds its own state — `tmp_db_conn_hr_clean` wipes payroll_runs/
payroll_items/salary_advances/leave_requests from the live-DB clone first
(`.claude/rules/erp-engineering-discipline.md` — tmp_db_conn/tmp_db clone the
LIVE dev DB WITH data, and generate_run builds items for EVERY active
employee of the company, including real ones like พุธ/id 1 whose real
history would otherwise collide with these exact chronology checks).
"""
import sqlite3

import pytest

import hr


# ── shared helpers ────────────────────────────────────────────────────────

def _mk_employee(conn, emp_code, full_name, start_date,
                 monthly_salary=15000.0, sso_enrolled=0, company_id=1):
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


def _plant_run(conn, employee_id, year_month, status, carried_out=0.0,
              carried_in=0.0, company_id=1):
    """Plant ONE payroll_runs row (draft or finalized) with one item for
    `employee_id`. Returns the run id."""
    finalized_at = '2000-01-01 00:00:00' if status == 'finalized' else None
    cur = conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, finalized_at)
           VALUES (?, ?, ?, date('now'), ?)""",
        (year_month, company_id, status, finalized_at),
    )
    run_id = cur.lastrowid
    conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_in, carried_out)
           VALUES (?, ?, 0, 0, 0, 0, ?, ?)""",
        (run_id, employee_id, carried_in, carried_out),
    )
    conn.commit()
    return run_id


def _item(conn, run_id, employee_id):
    r = conn.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run_id, employee_id),
    ).fetchone()
    assert r is not None, "payroll_items row missing"
    return r


# ═══════════════════════════════════════════════════════════════════════════
# 1. CHRONOLOGICAL FINALIZE invariant — hr.py::_build_item / CarryChronologyError
# ═══════════════════════════════════════════════════════════════════════════

def test_double_collection_path_is_refused_at_generate(tmp_db_conn_hr_clean):
    """The traced hazard (plan.md P1d step 3): Aug finalized carried_out=950
    -> Sep generated (carried_in=950) but left DRAFT -> generating Oct must
    REFUSE, not silently fall back to Aug and collect the ฿950 a second time."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CHR1', 'double collect', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_run(c, eid, '2026-08', 'finalized', carried_out=950.0)

    sep = hr.generate_run('2026-09', 1, created_by=1, conn=c)
    sep_item = _item(c, sep['id'], eid)
    assert sep_item['carried_in'] == 950.0, "control: Sep must actually carry the debt in"
    assert sep['status'] == 'draft', "control: Sep is left un-finalized — the hazard shape"

    with pytest.raises(hr.CarryChronologyError):
        hr.generate_run('2026-10', 1, created_by=1, conn=c)


def test_mirror_case_reopened_source_blocks_regenerate(tmp_db_conn_hr_clean):
    """Mirror of the double-collection path: Jul finalized (an OLDER source),
    Aug finalized carried_out=200 then REOPENED back to draft, Sep still draft
    with carried_in previously read from Aug -> regenerating Sep must refuse
    rather than silently fall back past Aug to Jul or to 0."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CHR2', 'mirror case', '2025-01-01',
                       monthly_salary=15000.0)
    # big enough to push Aug negative too, so Aug carries forward its own remainder
    _plant_run(c, eid, '2026-07', 'finalized', carried_out=15200.0)

    aug = hr.generate_run('2026-08', 1, created_by=1, conn=c)
    aug_item = _item(c, aug['id'], eid)
    assert aug_item['carried_in'] == 15200.0, "control: Aug must actually read Jul's carry"
    assert aug_item['carried_out'] == 200.0, "control: Aug must actually carry forward too"
    hr.finalize_run(aug['id'], conn=c, confirm_carry=True)

    sep = hr.generate_run('2026-09', 1, created_by=1, conn=c)
    sep_item = _item(c, sep['id'], eid)
    assert sep_item['carried_in'] == 200.0, "control: Sep correctly reads Aug's carry"
    assert sep['status'] == 'draft', "control: Sep left un-finalized"

    # Sep is still draft (not finalized), so nothing has consumed Aug's carry
    # yet — reopening Aug needs no carry-consumed confirmation at this point.
    assert hr.carry_consumed_by(aug['id'], conn=c) == []
    hr.reopen_run(aug['id'], reason='แก้ไข', actor='t', conn=c)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (aug['id'],)).fetchone()[0] == 'draft', \
        "control: Aug must actually be back to draft"

    with pytest.raises(hr.CarryChronologyError):
        hr.generate_run('2026-09', 1, created_by=1, conn=c)


def test_consecutive_chronological_finalize_never_raises(tmp_db_conn_hr_clean):
    """Regression control: the ordinary case (every month finalized in
    order, no gaps) must never trip this guard — pinned separately from
    test_payroll_carry_forward.py's P1a tests since those predate P1d."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CHR3', 'clean chain', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_run(c, eid, '2025-12', 'finalized', carried_out=950.0)

    jan = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    hr.finalize_run(jan['id'], conn=c, confirm_carry=True)
    feb = hr.generate_run('2026-02', 1, created_by=1, conn=c)  # must not raise
    feb_item = _item(c, feb['id'], eid)
    assert feb_item['carried_in'] == _item(c, jan['id'], eid)['carried_out']


# ═══════════════════════════════════════════════════════════════════════════
# 2. reopen_run(confirm_carry_break=...) — CarryConsumedWarning
# ═══════════════════════════════════════════════════════════════════════════

def test_reopen_refuses_when_a_later_run_already_consumed_the_carry(tmp_db_conn_hr_clean):
    """⚠ State triple, same reasoning as the finalize guard: assert the
    refusal left the run untouched, not merely that something raised."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CC1', 'consumed', '2025-01-01', monthly_salary=15000.0)
    dec_run = _plant_run(c, eid, '2025-12', 'finalized', carried_out=15200.0)

    jan = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    jan_item = _item(c, jan['id'], eid)
    assert jan_item['carried_in'] == 15200.0, "control: Jan must actually read Dec's carry"
    hr.finalize_run(jan['id'], conn=c, confirm_carry=True)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (jan['id'],)).fetchone()[0] == 'finalized', \
        "control: Jan (the consumer) must actually be finalized"

    consumers = hr.carry_consumed_by(dec_run, conn=c)
    assert len(consumers) == 1
    assert consumers[0]['run_id'] == jan['id']
    assert consumers[0]['year_month'] == '2026-01'
    assert consumers[0]['employee_id'] == eid

    with pytest.raises(hr.CarryConsumedWarning):
        hr.reopen_run(dec_run, reason='แก้ไข', actor='t', conn=c)

    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (dec_run,)).fetchone()[0] == 'finalized', \
        "refused before any mutation"


def test_reopen_succeeds_with_confirm_carry_break(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CC2', 'confirmed break', '2025-01-01',
                       monthly_salary=15000.0)
    dec_run = _plant_run(c, eid, '2025-12', 'finalized', carried_out=15200.0)
    jan = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    hr.finalize_run(jan['id'], conn=c, confirm_carry=True)

    hr.reopen_run(dec_run, reason='แก้ไข', actor='t', conn=c,
                  confirm_carry_break=True)
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (dec_run,)).fetchone()[0] == 'draft'


def test_reopen_with_no_downstream_consumer_needs_no_confirmation(tmp_db_conn_hr_clean):
    """⚠ Control (plan.md P1d test list): a REAL later finalized run whose
    carried_in is 0 must NOT be treated as a consumer."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CC3', 'no consumer', '2025-01-01',
                       monthly_salary=15000.0)
    dec_run = _plant_run(c, eid, '2025-12', 'finalized', carried_out=0.0)

    jan = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    jan_item = _item(c, jan['id'], eid)
    assert jan_item['carried_in'] == 0.0, "control: Jan really carries nothing in"
    hr.finalize_run(jan['id'], conn=c)  # no confirm needed — no carry at all

    assert hr.carry_consumed_by(dec_run, conn=c) == []
    hr.reopen_run(dec_run, reason='แก้ไข', actor='t', conn=c)  # must NOT raise
    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (dec_run,)).fetchone()[0] == 'draft'


def test_carry_consumed_note_none_then_present(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_CC4', 'note test', '2025-01-01', monthly_salary=15000.0)
    dec_run = _plant_run(c, eid, '2025-12', 'finalized', carried_out=15200.0)
    assert hr.carry_consumed_note(dec_run, conn=c) is None, \
        "control: before Jan is even generated, nothing has consumed it"

    jan = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    hr.finalize_run(jan['id'], conn=c, confirm_carry=True)

    note = hr.carry_consumed_note(dec_run, conn=c)
    assert note is not None
    assert 'note test' in note
    assert '2026-01' in note


# ── route: blueprints/hr.py::payroll_reopen + payroll_detail's confirm UI ──

@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _make_consumed_run(tmp_db):
    """A finalized Dec run whose carry a finalized Jan run has already
    consumed. Returns (dec_run_id, employee_id)."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_CCROUTE','consumed-route','M',1,'2025-01-01',
                       90, 0, 0, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2025-01-01', 15000.0, 'initial')""", (eid,))
        dec_run = conn.execute(
            """INSERT INTO payroll_runs
                 (year_month, company_id, status, run_date, finalized_at)
               VALUES ('2025-12', 1, 'finalized', date('now'), datetime('now'))"""
        ).lastrowid
        conn.execute(
            """INSERT INTO payroll_items
                 (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
                  carried_out)
               VALUES (?, ?, 0, 0, 0, 0, 15200.0)""", (dec_run, eid))
        conn.commit()
        import hr as hr_mod
        jan = hr_mod.generate_run('2026-01', 1, created_by=1, conn=conn)
        hr_mod.finalize_run(jan['id'], conn=conn, confirm_carry=True)
        return dec_run, eid
    finally:
        conn.close()


def test_payroll_detail_shows_carry_consumed_warning_and_confirm_box(admin_client, tmp_db):
    dec_run, eid = _make_consumed_run(tmp_db)
    html = admin_client.get(f'/hr/payroll/{dec_run}').get_data(as_text=True)
    assert 'data-warn="carry-consumed"' in html
    assert 'name="confirm_carry_break"' in html
    assert 'consumed-route' in html
    assert '2026-01' in html, "must name the downstream month"


def test_reopen_route_refuses_without_confirm_and_succeeds_with_it(admin_client, tmp_db):
    dec_run, eid = _make_consumed_run(tmp_db)

    def status():
        return sqlite3.connect(tmp_db).execute(
            "SELECT status FROM payroll_runs WHERE id=?", (dec_run,)).fetchone()[0]

    resp = admin_client.post(f'/hr/payroll/{dec_run}/reopen',
                             data={'reason': 'แก้ไข'}, follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'finalized', "unconfirmed reopen must not un-finalize"

    resp = admin_client.post(
        f'/hr/payroll/{dec_run}/reopen',
        data={'reason': 'แก้ไข', 'confirm_carry_break': '1'},
        follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'draft', "confirmed reopen must proceed"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Departing-employee warning — hr.py + blueprints/hr.py::employee_edit
# ═══════════════════════════════════════════════════════════════════════════

def test_departing_employee_outstanding_carried_out(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_DEP1', 'departing carry', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_run(c, eid, '2025-12', 'finalized', carried_out=15200.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    item = _item(c, run['id'], eid)
    assert item['carried_out'] == 200.0, "control: this run actually carries"

    carried_out, advance_total = hr.departing_employee_outstanding(eid, conn=c)
    assert carried_out == 200.0
    assert advance_total == 0.0


def test_departing_employee_outstanding_advances(tmp_db_conn_hr_clean):
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_DEP2', 'departing advance', '2025-01-01',
                       monthly_salary=15000.0)
    c.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name) "
        "VALUES (?, '2026-01-05', 500.0, 'test')", (eid,))
    c.commit()

    carried_out, advance_total = hr.departing_employee_outstanding(eid, conn=c)
    assert carried_out == 0.0
    assert advance_total == 500.0


def test_departing_employee_outstanding_clean(tmp_db_conn_hr_clean):
    """Control: an employee with a real payroll item and no debt reports
    (0, 0), not merely because nothing was ever generated for them."""
    c = tmp_db_conn_hr_clean
    eid = _mk_employee(c, 'T_DEP3', 'clean leaver', '2025-01-01',
                       monthly_salary=15000.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    items = c.execute(
        "SELECT * FROM payroll_items WHERE run_id=? AND employee_id=?",
        (run['id'], eid)).fetchall()
    assert len(items) == 1, "control: a real item must actually be generated"

    assert hr.departing_employee_outstanding(eid, conn=c) == (0.0, 0.0)


# ── route: blueprints/hr.py::employee_edit ───────────────────────────────

def _mk_route_employee(tmp_db, emp_code, full_name, monthly_salary=15000.0):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES (?, ?, 'M', 1, '2025-01-01', 90, 0, 0, 1)""",
            (emp_code, full_name)).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2025-01-01', ?, 'initial')""", (eid, monthly_salary))
        conn.commit()
        return eid
    finally:
        conn.close()


def _form_from_employee(row):
    """A COMPLETE form covering every field hrq.update_employee's UPDATE
    writes, sourced from the row's current values — a partial POST would
    otherwise null out unrelated columns (emp_code/full_name are NOT NULL
    with no Python-side default in update_employee)."""
    return {
        'emp_code': row['emp_code'] or '',
        'full_name': row['full_name'] or '',
        'nickname': row['nickname'] or '',
        'national_id': row['national_id'] or '',
        'gender': row['gender'] or '',
        'phone': row['phone'] or '',
        'address': row['address'] or '',
        'position': row['position'] or '',
        'company_id': str(row['company_id'] or ''),
        'employment_type': row['employment_type'] or 'monthly',
        'start_date': row['start_date'] or '',
        'probation_days': str(row['probation_days'] or 90),
        'probation_end_date': row['probation_end_date'] or '',
        'end_date': row['end_date'] or '',
        'sso_enrolled': str(row['sso_enrolled'] or 0),
        'diligence_allowance': str(row['diligence_allowance'] or 0),
        'bank_name': row['bank_name'] or '',
        'bank_branch': row['bank_branch'] or '',
        'bank_account_no': row['bank_account_no'] or '',
        'bank_account_name': row['bank_account_name'] or '',
        'salesperson_code': row['salesperson_code'] or '',
        'is_active': str(row['is_active']),
        'on_payroll': str(row['on_payroll']),
        'note': row['note'] or '',
    }


def test_employee_edit_warns_when_deactivating_a_leaver_with_outstanding_carry(
        admin_client, tmp_db):
    eid = _mk_route_employee(tmp_db, 'T_DEPR1', 'route departing carry')
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        import hr as hr_mod
        dec_run = conn.execute(
            """INSERT INTO payroll_runs
                 (year_month, company_id, status, run_date, finalized_at)
               VALUES ('2025-12', 1, 'finalized', date('now'), datetime('now'))"""
        ).lastrowid
        conn.execute(
            """INSERT INTO payroll_items
                 (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
                  carried_out)
               VALUES (?, ?, 0, 0, 0, 0, 15200.0)""", (dec_run, eid))
        conn.commit()
        run = hr_mod.generate_run('2026-01', 1, created_by=1, conn=conn)
        item = conn.execute(
            "SELECT carried_out FROM payroll_items WHERE run_id=? AND employee_id=?",
            (run['id'], eid)).fetchone()
        assert item['carried_out'] == 200.0, "control: this employee actually carries"
        row = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    finally:
        conn.close()

    form = _form_from_employee(row)
    form['is_active'] = '0'
    resp = admin_client.post(f'/hr/employees/{eid}/edit', data=form,
                             follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ยังมี' in html and '200' in html, "the outstanding-balance warning must show"
    is_active = sqlite3.connect(tmp_db).execute(
        "SELECT is_active FROM employees WHERE id=?", (eid,)).fetchone()[0]
    assert is_active == 0, "control: the deactivation must have actually landed"


def test_employee_edit_warns_when_setting_end_date_with_outstanding_advance(
        admin_client, tmp_db):
    eid = _mk_route_employee(tmp_db, 'T_DEPR2', 'route end date advance')
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name) "
        "VALUES (?, '2026-01-05', 500.0, 'test')", (eid,))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    conn.close()

    form = _form_from_employee(row)
    form['end_date'] = '2026-01-31'
    resp = admin_client.post(f'/hr/employees/{eid}/edit', data=form,
                             follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ยังมี' in html and '500' in html
    end_date = sqlite3.connect(tmp_db).execute(
        "SELECT end_date FROM employees WHERE id=?", (eid,)).fetchone()[0]
    assert end_date == '2026-01-31', "control: the end_date must have actually landed"


def test_employee_edit_no_warning_when_leaver_owes_nothing(admin_client, tmp_db):
    """⚠ Control: the departure transition alone must not warn — only a
    REAL outstanding balance does."""
    eid = _mk_route_employee(tmp_db, 'T_DEPR3', 'route clean leaver')
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    conn.close()

    form = _form_from_employee(row)
    form['is_active'] = '0'
    resp = admin_client.post(f'/hr/employees/{eid}/edit', data=form,
                             follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ยังมี' not in html, "no outstanding balance -> no warning"
    is_active = sqlite3.connect(tmp_db).execute(
        "SELECT is_active FROM employees WHERE id=?", (eid,)).fetchone()[0]
    assert is_active == 0, \
        "control: the deactivation must have actually landed (proves the " \
        "transition path was really reached, not skipped upstream)"


def test_employee_edit_no_warning_when_no_departure_transition(admin_client, tmp_db):
    """⚠ Control: an outstanding balance ALONE must not trigger the warning
    — only a genuine is_active/end_date TRANSITION does. Without this, the
    warning could fire (or the test could pass) for the wrong reason."""
    eid = _mk_route_employee(tmp_db, 'T_DEPR4', 'route unrelated edit')
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name) "
        "VALUES (?, '2026-01-05', 500.0, 'test')", (eid,))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    conn.close()

    form = _form_from_employee(row)   # is_active stays '1', end_date stays ''
    form['nickname'] = 'renamed'      # an unrelated edit
    resp = admin_client.post(f'/hr/employees/{eid}/edit', data=form,
                             follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ยังมี' not in html, "no departure transition -> no warning, despite owing money"
    nickname = sqlite3.connect(tmp_db).execute(
        "SELECT nickname FROM employees WHERE id=?", (eid,)).fetchone()[0]
    assert nickname == 'renamed', "control: the edit must have actually landed"
