"""Route-level integration tests for bp_hr.

Uses tmp_db so route + models + templates execute against a live-DB clone
and never touch the real DB. Logs in as admin via session pre-population
because the hr before_request middleware in app.py blocks staff entirely
from hr.* endpoints.

Covers 3 GET endpoints: dashboard, employee detail, and leave list.
Payroll-list is also a candidate but the dashboard already exercises the
hrq.get_payroll_runs() call path, so leave_list (different hrq query
surface) gives broader coverage.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated. tmp_db
    must be pulled in first so config.DATABASE_PATH is monkeypatched
    before `from app import app` runs."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _first_employee_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No employees in live DB clone")
    return row[0]


def test_hr_dashboard_renders(admin_client):
    """Headcount + on-leave + probation-ending + over-quota alerts —
    iterates over all active employees and computes leave balances, so
    this is the broadest hr-module smoke test."""
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200, resp.data[:500]


def test_hr_employee_detail_renders(admin_client, tmp_db):
    """Per-employee card with salary history + leave balance."""
    eid = _first_employee_id(tmp_db)
    resp = admin_client.get(f'/hr/employees/{eid}')
    assert resp.status_code == 200, resp.data[:500]


def test_hr_leave_list_renders(admin_client):
    """Leave-request list with employee/month/type filter dropdowns."""
    resp = admin_client.get('/hr/leave')
    assert resp.status_code == 200, resp.data[:500]


# ── stale-draft banner on /hr/ dashboard ─────────────────────────────────

def test_hr_dashboard_shows_stale_draft_banner(admin_client, tmp_db):
    """Insert a draft payroll run for a past month → banner copy renders.
    Use a year_month that is unambiguously past (2024-01) regardless of
    when the test runs."""
    sqlite3.connect(tmp_db).execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, created_by)
           VALUES ('2024-01', 1, 'draft', '2024-01-31', 1)"""
    ).connection.commit()
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200
    assert b'payroll run' in resp.data and 'draft' in resp.data.decode('utf-8')
    # Banner-specific copy
    assert 'ค้าง draft' in resp.data.decode('utf-8'), \
        "stale-draft banner copy missing from dashboard"


def test_hr_dashboard_no_banner_when_only_current_month_draft(admin_client, tmp_db):
    """A draft for the CURRENT month is normal mid-prep, must NOT trigger
    the banner. Use a date-derived year_month so the test is date-stable."""
    from datetime import date
    this_ym = date.today().strftime("%Y-%m")
    # Clean slate first so live-DB clone state can't pollute
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM payroll_runs")
    conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, created_by)
           VALUES (?, 1, 'draft', date('now'), 1)""", (this_ym,)
    )
    conn.commit()
    conn.close()
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200
    assert 'ค้าง draft' not in resp.data.decode('utf-8'), \
        "current-month draft should NOT trigger stale-banner"


# ── /hr/payroll/<id>/reopen — POST route on a finalized run ───────────────

def _make_finalized_run(tmp_db) -> int:
    """Create a fresh finalized run in the live-DB clone and return its id."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        import hr as hr_mod
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_RREO','reopen-route','M',1,'2026-01-01',
                       90, 0, 0, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2026-01-01', 15000.0, 'initial')""", (eid,)
        )
        conn.commit()
        run = hr_mod.generate_run('2026-09', 1, created_by=1, conn=conn)
        hr_mod.finalize_run(run['id'], conn=conn)
        return run['id']
    finally:
        conn.close()


def test_hr_payroll_reopen_admin_with_reason_un_finalizes(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    resp = admin_client.post(f'/hr/payroll/{rid}/reopen',
                             data={'reason': 'แก้ไข bonus'},
                             follow_redirects=False)
    assert resp.status_code in (302, 303), resp.data[:500]
    status = sqlite3.connect(tmp_db).execute(
        "SELECT status FROM payroll_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    assert status == 'draft'


def test_hr_payroll_reopen_admin_without_reason_no_mutation(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    resp = admin_client.post(f'/hr/payroll/{rid}/reopen',
                             data={'reason': '   '},
                             follow_redirects=False)
    assert resp.status_code in (302, 303), resp.data[:500]
    status = sqlite3.connect(tmp_db).execute(
        "SELECT status FROM payroll_runs WHERE id=?", (rid,)
    ).fetchone()[0]
    assert status == 'finalized'  # unchanged


def test_hr_payroll_reopen_missing_id_404(admin_client):
    resp = admin_client.post('/hr/payroll/999999/reopen',
                             data={'reason': 'x'})
    assert resp.status_code == 404


# ── roster-drift warning + confirmation in the reopen dialog ────────────────

def _add_employee_after_finalize(tmp_db, month_start='2026-09-01'):
    """A hire whose start_date lands inside an already-closed month, so the
    active set for that month now contains someone the run does not."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_DRIFT2','drift-route','M',1,?,90,0,0,1)""",
            (month_start,)).lastrowid
        conn.execute(
            "INSERT INTO employee_salary_history "
            "(employee_id, effective_date, monthly_salary, reason) "
            "VALUES (?, ?, 12000.0, 'initial')", (eid, month_start))
        conn.commit()
        return eid
    finally:
        conn.close()


def test_payroll_detail_shows_the_roster_warning_and_confirm_box(admin_client, tmp_db):
    """The page must say what will happen BEFORE the dialog is opened.

    reopen_run demands an acknowledgement when the roster drifted; without
    this the operator meets that demand only as a rejected submit.
    """
    rid = _make_finalized_run(tmp_db)
    clean = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="roster-drift"' not in clean, "no drift yet — no warning"
    assert 'name="confirm_roster_change"' not in clean

    _add_employee_after_finalize(tmp_db)
    drifted = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="roster-drift"' in drifted
    assert 'name="confirm_roster_change"' in drifted
    assert 'drift-route' in drifted, "the warning must name who would be added"


def test_reopen_needs_the_confirm_box_when_the_roster_drifted(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    _add_employee_after_finalize(tmp_db)

    def status():
        return sqlite3.connect(tmp_db).execute(
            "SELECT status FROM payroll_runs WHERE id=?", (rid,)).fetchone()[0]

    resp = admin_client.post(f'/hr/payroll/{rid}/reopen',
                             data={'reason': 'แก้รายคน'}, follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'finalized', "unconfirmed reopen must not un-finalize"

    resp = admin_client.post(
        f'/hr/payroll/{rid}/reopen',
        data={'reason': 'แก้รายคน', 'confirm_roster_change': '1'},
        follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'draft', "confirmed reopen must proceed"


def test_reopen_dialog_warns_about_advances_even_with_a_clean_roster(admin_client, tmp_db):
    """The advance hazard is independent of the roster one.

    Folding it into the roster warning would have hidden it on exactly the runs
    that look safest — a matching roster says nothing about whether a
    re-finalize would swallow a back-dated advance.
    """
    rid = _make_finalized_run(tmp_db)
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        emp = conn.execute(
            "SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
            (rid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO salary_advances (employee_id, advance_date, amount) "
            "VALUES (?, '2026-09-20', 640)", (emp,))
        conn.commit()
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="pending-advance-stamp"' in html
    assert '640' in html
    assert 'data-warn="roster-drift"' not in html, "roster is clean — only the advance warning"


def test_finalize_is_refused_while_an_advance_would_be_stamped_uncollected(admin_client, tmp_db):
    """End to end at the boundary that moves money.

    The reopen-time banner is gone by now (the run is draft), so this page is
    the operator's only warning — and the POST must REFUSE, not merely render
    a notice. There is no acknowledgement to give: the deduction is computed
    in _build_item during generate_run, which a reopened drifted run cannot
    run, so nothing the operator clicks can put this money into a payslip.
    """
    rid = _make_finalized_run(tmp_db)
    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        import hr as hr_mod
        conn.row_factory = sqlite3.Row
        hr_mod.reopen_run(rid, reason='แก้ตัวเลข', actor='t', conn=conn)
        emp = conn.execute(
            "SELECT employee_id FROM payroll_items WHERE run_id=? LIMIT 1",
            (rid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO salary_advances (employee_id, advance_date, amount) "
            "VALUES (?, '2026-09-18', 555)", (emp,))
        conn.commit()
    finally:
        conn.close()

    def status():
        return sqlite3.connect(tmp_db).execute(
            "SELECT status FROM payroll_runs WHERE id=?", (rid,)).fetchone()[0]
    assert status() == 'draft'

    page = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="pending-advance-stamp"' in page, "draft page must carry the warning"
    assert '555' in page

    admin_client.post(f'/hr/payroll/{rid}/finalize', data={}, follow_redirects=True)
    assert status() == 'draft', "finalize must not proceed"
    assert sqlite3.connect(tmp_db).execute(
        "SELECT deducted_in_run_id FROM salary_advances WHERE amount=555"
    ).fetchone()[0] is None, "and must not stamp the advance"

    # A crafted or stale POST carrying the old override field must NOT get
    # through: there is no override, because consent cannot make the money
    # move (Codex). The page shows no checkbox for the same reason.
    admin_client.post(f'/hr/payroll/{rid}/finalize',
                      data={'confirm_advance_stamp': '1'}, follow_redirects=True)
    assert status() == 'draft', "no override exists"
    assert 'name="confirm_advance_stamp"' not in page

    # Fixing the DATA is the way out: re-date the advance to an open month.
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.execute("UPDATE salary_advances SET advance_date='2026-11-05' WHERE amount=555")
    conn.commit(); conn.close()
    admin_client.post(f'/hr/payroll/{rid}/finalize', data={}, follow_redirects=True)
    assert status() == 'finalized'


# ── /hr/payroll/<id>/item/<id> — blank note means CLEAR, absent means KEEP ──
#
# The model contract is `None` preserves / `""` clears; these two pin the route
# boundary that has to carry that distinction through, in both directions. The
# pair is the point: a fix that clears on blank is only correct if it still
# preserves on an omitted key.


def test_payroll_item_edit_clears_explicitly_blank_notes(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    admin_client.post(
        f'/hr/payroll/{rid}/reopen',
        data={'reason': 'prepare note-clear route test'},
    )

    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? ORDER BY id LIMIT 1",
            (rid,),
        ).fetchone()[0]
        conn.execute(
            """UPDATE payroll_items
                  SET other_additions_note='temporary addition note',
                      other_deductions_note='temporary deduction note'
                WHERE id=?""",
            (item_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin_client.post(
        f'/hr/payroll/{rid}/item/{item_id}',
        data={
            'other_additions_note': '',
            'other_deductions_note': '',
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    try:
        notes = conn.execute(
            """SELECT other_additions_note, other_deductions_note
                 FROM payroll_items WHERE id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    assert notes == ('', '')


def test_payroll_item_edit_preserves_notes_when_keys_are_omitted(admin_client, tmp_db):
    rid = _make_finalized_run(tmp_db)
    admin_client.post(
        f'/hr/payroll/{rid}/reopen',
        data={'reason': 'prepare omitted-note route test'},
    )

    conn = sqlite3.connect(tmp_db, timeout=10)
    try:
        item_id = conn.execute(
            "SELECT id FROM payroll_items WHERE run_id=? ORDER BY id LIMIT 1",
            (rid,),
        ).fetchone()[0]
        conn.execute(
            """UPDATE payroll_items
                  SET other_additions_note='keep addition note',
                      other_deductions_note='keep deduction note'
                WHERE id=?""",
            (item_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin_client.post(
        f'/hr/payroll/{rid}/item/{item_id}',
        data={'bonus': '12345'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    conn = sqlite3.connect(tmp_db)
    try:
        bonus, *notes = conn.execute(
            """SELECT bonus, other_additions_note, other_deductions_note
                 FROM payroll_items WHERE id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    # CONTROL first: the route redirects (302) on its error path AND on the
    # finalized-run bail-out too, so "notes unchanged" alone is satisfied by a
    # request that never reached update_payroll_item. The bonus is the proof
    # that it did — verified by deleting the reopen POST above, which leaves
    # the run finalized and made this assertion the only one that went red.
    assert bonus == 12345.0
    assert tuple(notes) == ('keep addition note', 'keep deduction note')
