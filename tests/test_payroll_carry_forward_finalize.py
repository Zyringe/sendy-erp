"""TDD — payroll carry-forward finalize guard (P1b:
`hr.py::finalize_run`'s `confirm_carry` + `CarryForwardWarning`,
`blueprints/hr.py::payroll_finalize`'s route wiring, and the confirm control
on `templates/hr/payroll_detail.html`).

Trigger + design: projects/payroll-carry-forward/plan.md, section "### P1b".
Put's ruling: a negative net carries forward AUTOMATICALLY (no write-off
path exists at all — plan.md "ไม่มีการยกหนี้ให้"), but `finalize_run` must
STOP and ask for an explicit confirmation before committing to that. Not
silent, not a hard block — same shape as `reopen_run`'s existing
`confirm_roster_change` / `RosterDriftWarning` precedent.

Every test seeds its own state — `tmp_db_conn` / `tmp_db` clone the LIVE dev
DB WITH its data (`.claude/rules/erp-engineering-discipline.md`); nothing is
inherited, everything asserted is planted by the test itself.
"""
import sqlite3

import pytest

import hr


# ── shared helpers ────────────────────────────────────────────────────────
# Same shape as test_payroll_carry_forward.py's _mk_employee/_plant_finalized_
# run/_item. Duplicated per test file is the existing convention in this
# suite (see _add_advance in test_hr_payroll.py / test_hr_salary_advance.py /
# test_payroll_carry_forward.py) rather than a cross-file import.

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


def _plant_finalized_run(conn, employee_id, year_month, carried_out,
                         company_id=1):
    """Directly plant a FINALIZED prior run carrying `carried_out` — the
    SOURCE row _build_item's carried_in query reads (same helper as
    test_payroll_carry_forward.py)."""
    cur = conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, finalized_at)
           VALUES (?, ?, 'finalized', date('now'), datetime('now'))""",
        (year_month, company_id),
    )
    run_id = cur.lastrowid
    conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_out)
           VALUES (?, ?, 0, 0, 0, 0, ?)""",
        (run_id, employee_id, carried_out),
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
# Engine — hr.py::finalize_run(confirm_carry=...), CarryForwardWarning,
# pending_carry_forward, carry_forward_note
# ═══════════════════════════════════════════════════════════════════════════

def test_finalize_refuses_without_confirm_when_carry_pending(tmp_db_conn):
    """⚠ The finalize route reads no editable fields, so the usual "post a
    control field and check it landed" trick does not work (plan.md P1b) —
    assert the STATE TRIPLE instead: still draft, finalized_at still NULL,
    and (a REAL advance planted to prove it) the advance stays unstamped.

    The advance is planted BEFORE generate_run (not after) so it is already
    inside salary_advance_deduction and pending_advance_stamp reports 0 —
    otherwise PendingAdvanceStampWarning would fire first and this test
    would never reach the carry-forward guard it means to pin."""
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_FIN1', 'no confirm', '2025-01-01',
                       monthly_salary=15000.0)
    # carried_in (15200) exceeds gross (15000) -> this run's item clamps to 0
    # and carries a remainder — exactly what must trip the guard.
    _plant_finalized_run(c, eid, '2025-12', 15200.0)
    c.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name) "
        "VALUES (?, '2026-01-05', 100.0, 'test')", (eid,))
    c.commit()
    adv_id = c.execute("SELECT MAX(id) FROM salary_advances").fetchone()[0]

    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    it = _item(c, run['id'], eid)
    # gross 15000 - carried_in 15200 - advance 100 = -300 -> carried_out 300
    assert it['carried_out'] == 300.0, "control: this run must actually carry"
    assert hr.pending_advance_stamp(run['id'], conn=c) == (0, 0.0), \
        "control: the advance must already be absorbed into the deduction"

    with pytest.raises(hr.CarryForwardWarning):
        hr.finalize_run(run['id'], conn=c)

    row = c.execute(
        "SELECT status, finalized_at FROM payroll_runs WHERE id=?",
        (run['id'],)).fetchone()
    assert row['status'] == 'draft', "refused before any mutation"
    assert row['finalized_at'] is None
    assert c.execute("SELECT deducted_in_run_id FROM salary_advances WHERE id=?",
                     (adv_id,)).fetchone()[0] is None, \
        "the refusal must leave advances untouched"


def test_finalize_succeeds_with_confirm_carry(tmp_db_conn):
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_FIN2', 'confirmed', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(c, eid, '2025-12', 15200.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    it = _item(c, run['id'], eid)
    assert it['carried_out'] == 200.0, "control: this run must actually carry"

    hr.finalize_run(run['id'], conn=c, confirm_carry=True)

    row = c.execute(
        "SELECT status, finalized_at FROM payroll_runs WHERE id=?",
        (run['id'],)).fetchone()
    assert row['status'] == 'finalized'
    assert row['finalized_at'] is not None
    after = _item(c, run['id'], eid)
    assert after['carried_out'] == 200.0, "carried_out must survive finalize unchanged"


def test_finalize_with_no_carry_needs_no_confirm(tmp_db_conn):
    """⚠ Control (plan.md P1b, Codex): `finalize_run` does not require items
    at all — an EMPTY run finalizes — so a test using an empty run would
    pass vacuously. Seed a REAL item with `carried_out = 0`."""
    c = tmp_db_conn
    _mk_employee(c, 'T_FIN3', 'no carry', '2025-01-01', monthly_salary=15000.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    items = c.execute("SELECT * FROM payroll_items WHERE run_id=?",
                      (run['id'],)).fetchall()
    assert len(items) == 1, "control: a real item must actually be generated"
    assert items[0]['carried_out'] == 0.0, "control: this item must not carry"

    hr.finalize_run(run['id'], conn=c)  # no confirm_carry passed at all

    assert c.execute("SELECT status FROM payroll_runs WHERE id=?",
                     (run['id'],)).fetchone()[0] == 'finalized'


def test_pending_carry_forward_names_and_totals(tmp_db_conn):
    """payroll_runs is UNIQUE(year_month, company_id) — one run per company
    per month holds every employee's item, so both employees' carry sources
    must live in the SAME '2025-12' run, not one _plant_finalized_run each."""
    c = tmp_db_conn
    e1 = _mk_employee(c, 'T_FIN4', 'carrier one', '2025-01-01', monthly_salary=15000.0)
    e2 = _mk_employee(c, 'T_FIN5', 'no carry two', '2025-01-01', monthly_salary=15000.0)
    src_run = c.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, finalized_at)
           VALUES ('2025-12', 1, 'finalized', date('now'), datetime('now'))"""
    ).lastrowid
    c.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_out)
           VALUES (?, ?, 0, 0, 0, 0, 15200.0)""", (src_run, e1))  # -> carries 200
    c.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              carried_out)
           VALUES (?, ?, 0, 0, 0, 0, 100.0)""", (src_run, e2))   # -> stays positive
    c.commit()

    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    n, total, rows = hr.pending_carry_forward(run['id'], conn=c)
    assert n == 1, "only the employee who actually clamped should be counted"
    assert total == 200.0
    assert rows == [('carrier one', 200.0)]


def test_carry_forward_note_none_when_no_carry(tmp_db_conn):
    c = tmp_db_conn
    _mk_employee(c, 'T_FIN6', 'clean', '2025-01-01', monthly_salary=15000.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    assert hr.carry_forward_note(run['id'], conn=c) is None


def test_carry_forward_note_present_and_names_who(tmp_db_conn):
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_FIN7', 'named in note', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(c, eid, '2025-12', 15200.0)
    run = hr.generate_run('2026-01', 1, created_by=1, conn=c)
    note = hr.carry_forward_note(run['id'], conn=c)
    assert note is not None
    assert 'named in note' in note
    assert '200' in note


# ═══════════════════════════════════════════════════════════════════════════
# Route — blueprints/hr.py::payroll_finalize + payroll_detail's confirm UI
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated (same shape as
    test_bp_hr_routes.py's own fixture — tmp_db must be pulled in first so
    config.DATABASE_PATH is monkeypatched before `from app import app`)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _make_carrying_draft_run(tmp_db):
    """A fresh DRAFT run whose one item carries a remainder into next month.
    Returns (run_id, employee_id)."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_FINROUTE','finalize-route','M',1,'2026-01-01',
                       90, 0, 0, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2026-01-01', 15000.0, 'initial')""", (eid,))
        # SOURCE: a finalized prior run this employee's Jan run will read
        # carried_in from (same shape as _plant_finalized_run above).
        run_id = conn.execute(
            """INSERT INTO payroll_runs
                 (year_month, company_id, status, run_date, finalized_at)
               VALUES ('2025-12', 1, 'finalized', date('now'), datetime('now'))"""
        ).lastrowid
        conn.execute(
            """INSERT INTO payroll_items
                 (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
                  carried_out)
               VALUES (?, ?, 0, 0, 0, 0, 15200.0)""", (run_id, eid))
        conn.commit()
        import hr as hr_mod
        run = hr_mod.generate_run('2026-01', 1, created_by=1, conn=conn)
        return run['id'], eid
    finally:
        conn.close()


def test_payroll_detail_shows_carry_forward_warning_and_confirm_box(admin_client, tmp_db):
    rid, eid = _make_carrying_draft_run(tmp_db)
    html = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="carry-forward"' in html
    assert 'name="confirm_carry"' in html
    assert 'finalize-route' in html, "the warning must name who carries"


def test_payroll_detail_no_carry_warning_when_clean(admin_client, tmp_db):
    """Control: an ordinary draft run with no carry must show neither."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        import hr as hr_mod
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_FINROUTE2','clean-route','M',1,'2026-01-01',
                       90, 0, 0, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2026-01-01', 15000.0, 'initial')""", (eid,))
        conn.commit()
        run = hr_mod.generate_run('2026-01', 1, created_by=1, conn=conn)
        rid = run['id']
        items = conn.execute("SELECT carried_out FROM payroll_items WHERE run_id=?",
                             (rid,)).fetchall()
        assert len(items) == 1 and items[0]['carried_out'] == 0.0, \
            "control: this run must actually be carry-free"
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{rid}').get_data(as_text=True)
    assert 'data-warn="carry-forward"' not in html
    assert 'name="confirm_carry"' not in html


def test_finalize_route_refuses_without_confirm_and_succeeds_with_it(admin_client, tmp_db):
    rid, eid = _make_carrying_draft_run(tmp_db)

    def status():
        return sqlite3.connect(tmp_db).execute(
            "SELECT status FROM payroll_runs WHERE id=?", (rid,)).fetchone()[0]

    resp = admin_client.post(f'/hr/payroll/{rid}/finalize', follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'draft', "unconfirmed finalize must not finalize"

    resp = admin_client.post(f'/hr/payroll/{rid}/finalize',
                             data={'confirm_carry': '1'}, follow_redirects=True)
    assert resp.status_code == 200
    assert status() == 'finalized', "confirmed finalize must proceed"


def test_finalize_route_needs_no_confirm_when_clean(admin_client, tmp_db):
    """Control (mirrors the reopen-route control in test_bp_hr_routes.py):
    the demand must be tied to money actually being at stake."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        import hr as hr_mod
        eid = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES ('T_FINROUTE3','clean-route-2','M',1,'2026-01-01',
                       90, 0, 0, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason)
               VALUES (?, '2026-01-01', 15000.0, 'initial')""", (eid,))
        conn.commit()
        run = hr_mod.generate_run('2026-01', 1, created_by=1, conn=conn)
        rid = run['id']
    finally:
        conn.close()

    resp = admin_client.post(f'/hr/payroll/{rid}/finalize', follow_redirects=True)
    assert resp.status_code == 200
    status = sqlite3.connect(tmp_db).execute(
        "SELECT status FROM payroll_runs WHERE id=?", (rid,)).fetchone()[0]
    assert status == 'finalized', "an ordinary finalize must not need confirm_carry"


# ── staleness guard (added by /scrutinize 2026-08-31) ─────────────────────
# `_assert_carry_chronology` is deliberately scoped to runs that TOUCH carry
# money, which leaves one residual case it cannot see: Sep is generated while
# Aug drafts at 0, Aug is then finalized carrying 950, and Sep is finalized
# WITHOUT a regenerate — Sep's stored carried_in still says 0 and the ฿950 is
# collected from nobody. `stale_carry_in` closes it at the finalize boundary,
# the same philosophy as `pending_advance_stamp`.

def test_stale_carried_in_refuses_finalize(tmp_db_conn):
    eid = _mk_employee(tmp_db_conn, 'T_STALE1', 'stale carry', '2025-01-01',
                       monthly_salary=15000.0)
    src_run = _plant_finalized_run(tmp_db_conn, eid, '2029-07', 950.0)

    aug = hr.generate_run('2029-08', 1, created_by=1, conn=tmp_db_conn)
    assert _item(tmp_db_conn, aug['id'], eid)['carried_in'] == 950.0, \
        "control: the run must actually have read the 950 at generate time"

    # The source moves AFTER Aug was generated (Jul reopened, edited, re-finalized).
    tmp_db_conn.execute(
        "UPDATE payroll_items SET carried_out = 1500.0 WHERE run_id = ? AND employee_id = ?",
        (src_run, eid))
    tmp_db_conn.commit()

    stale = hr.stale_carry_in(aug['id'], conn=tmp_db_conn)
    assert len(stale) == 1, f"expected exactly one stale row, got {stale}"
    assert stale[0][1] == 950.0 and stale[0][2] == 1500.0

    with pytest.raises(hr.StaleCarryInError):
        hr.finalize_run(aug['id'], conn=tmp_db_conn, confirm_carry=True)
    assert tmp_db_conn.execute(
        "SELECT status FROM payroll_runs WHERE id=?", (aug['id'],)).fetchone()[0] == 'draft', \
        "the run must still be draft — a refusal that finalized anyway is no refusal"


def test_stale_guard_does_not_fire_on_a_fresh_run(tmp_db_conn):
    """CONTROL — without this, the test above passes even if the guard fires
    on everything and finalize is simply broken for all runs."""
    eid = _mk_employee(tmp_db_conn, 'T_STALE2', 'fresh carry', '2025-01-01',
                       monthly_salary=15000.0)
    _plant_finalized_run(tmp_db_conn, eid, '2029-07', 950.0)
    aug = hr.generate_run('2029-08', 1, created_by=1, conn=tmp_db_conn)

    assert hr.stale_carry_in(aug['id'], conn=tmp_db_conn) == []
    hr.finalize_run(aug['id'], conn=tmp_db_conn, confirm_carry=True)
    assert tmp_db_conn.execute(
        "SELECT status FROM payroll_runs WHERE id=?", (aug['id'],)).fetchone()[0] == 'finalized'
