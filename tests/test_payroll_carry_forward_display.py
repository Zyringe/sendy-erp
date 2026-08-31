"""TDD — payroll carry-forward DISPLAY surfaces (P1c:
`templates/hr/payslip.html`, `templates/hr/payroll_detail.html`,
`blueprints/hr.py::payroll_export` (CSV), `hr_queries.py::get_payroll_runs` +
`get_employee_payslips`, `templates/hr/payroll.html`,
`templates/hr/dashboard.html`, `templates/me/payslip_list.html`.

Trigger + design: projects/payroll-carry-forward/plan.md, section "### P1c".
P1a (engine: `carried_in`/`carried_out` derivation + net clamping) and P1b
(finalize confirm guard) are ALREADY DONE and are not re-tested here — this
file plants payroll_items rows DIRECTLY with explicit carried_in/carried_out
values (same shape as test_payroll_carry_forward_finalize.py's
`_plant_finalized_run`) because P1c is about rendering those two columns
correctly, not about deriving them.

Every test seeds its own state — `tmp_db_conn` / `tmp_db` clone the LIVE dev
DB WITH its data (`.claude/rules/erp-engineering-discipline.md`); nothing is
inherited. year_months use a 2098/2099 range with VALID months (01-12) —
mirrors test_hr_phase6.py's own 2099-xx convention to dodge the
UNIQUE(year_month, company_id) constraint against real finalized runs on the
cloned dev DB; kept 01-12 because templates/me/payslip_list.html indexes a
13-slot month-name list by `ym[1]|int` and a fake month like '2099-13' would
throw IndexError there.

⚠ Test-quality gates (plan.md P1c + `.claude/rules/verification-discipline.md`):
  - Assert on the ELEMENT (`'>ยกยอดมาจากเดือนก่อน<'`) or a rendered VALUE
    (via the app's own `_fmt_baht`, the independent oracle) — never a bare
    Thai substring that could match unrelated static chrome.
  - The "renders WITHOUT a carry" tests assert a CONTROL first (the seeded
    item/run is actually visible in the html) before asserting the carry
    content is absent — payroll_detail.html's items table has an EMPTY
    branch (`ไม่มีรายการพนักงาน`) that would make the absence assertion
    vacuously true if the seeded row never rendered at all.
  - The multi-run list pages (payroll.html / dashboard.html) have NO
    per-run WHERE clause, so a bare "carry-total not in html" assertion on
    the FULL page would pass if a DIFFERENT run planted by another test on
    the shared cloned DB happened not to carry — it says nothing about
    THIS run's row. `_row_html()` isolates the exact `<tr>` for the run
    under test via its detail-link href before asserting presence/absence.
  - CSV: parsed with `csv.reader` (never string-split — verification-
    discipline's "verify with a real parser" rule), header COUNT asserted
    before the specific row's carry values, values compared as float (same
    convention as test_hr_wht_ui.py's own CSV test) not exact string.
"""
import csv
import io
import sqlite3

import pytest

import hr_queries as hrq
from blueprints.hr import _fmt_baht


# ── shared helpers ────────────────────────────────────────────────────────
# Duplicated-per-file is the existing convention in this suite (see
# _add_advance in test_hr_payroll.py / _plant_finalized_run in
# test_payroll_carry_forward_finalize.py) rather than a cross-file import.

def _mk_employee(conn, emp_code, full_name, company_id=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, 'M', ?, '2025-01-01', 90, 0, 0, 1)""",
        (emp_code, full_name, company_id),
    )
    conn.commit()
    return cur.lastrowid


def _plant_item(conn, year_month, employee_id, *, status='finalized',
                carried_in=0.0, carried_out=0.0, gross=15000.0,
                net_pay=None, salary_advance_deduction=0.0, company_id=1):
    """Plant one payroll_runs + payroll_items row with EXPLICIT carry
    columns (P1c is display — the arithmetic that derives these two values
    is already covered by test_payroll_carry_forward.py, P1a)."""
    if net_pay is None:
        net_pay = max(gross - carried_in - salary_advance_deduction, 0.0)
    finalized_sql = "datetime('now')" if status == 'finalized' else 'NULL'
    run_id = conn.execute(
        f"""INSERT INTO payroll_runs
              (year_month, company_id, status, run_date, finalized_at)
            VALUES (?, ?, ?, date('now'), {finalized_sql})""",
        (year_month, company_id, status),
    ).lastrowid
    item_id = conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay,
              salary_advance_deduction, carried_in, carried_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, employee_id, gross, gross, gross, net_pay,
         salary_advance_deduction, carried_in, carried_out),
    ).lastrowid
    conn.commit()
    return run_id, item_id


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client, admin session pre-populated (same shape as
    test_payroll_carry_forward_finalize.py's own fixture — tmp_db must be
    pulled in first so config.DATABASE_PATH is monkeypatched before the
    app import)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


@pytest.fixture
def emp_client(tmp_db):
    """Test client logged in AS the planted employee (role='staff', linked
    via employees.user_id) — for the /me/payslip self-service surface."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 555
        sess['username'] = 'test-staff'
        sess['role'] = 'staff'
    return c


def _link_user(conn, employee_id, user_id):
    conn.execute("UPDATE employees SET user_id=? WHERE id=?", (user_id, employee_id))
    conn.commit()


def _row_html(html, anchor):
    """Isolate the single `<tr>...</tr>` that contains `anchor` (a unique
    per-row href like '/hr/payroll/123"'). Both payroll.html and
    dashboard.html render a MULTI-run table with no per-run filter, so a
    bare substring check against the whole page cannot tell "this run has
    no carry" from "some OTHER run on the shared cloned DB has no carry"."""
    assert anchor in html, f"control: {anchor!r} must actually render"
    idx = html.index(anchor)
    start = html.rindex('<tr>', 0, idx)
    end = html.index('</tr>', idx) + len('</tr>')
    return html[start:end]


# ═══════════════════════════════════════════════════════════════════════════
# hr_queries.py — get_employee_payslips / get_payroll_runs
# ═══════════════════════════════════════════════════════════════════════════

def test_get_employee_payslips_includes_carry_columns(tmp_db_conn):
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_DISP01', 'payslip carry query')
    _plant_item(c, '2099-01', eid, carried_in=200.0, carried_out=950.0)
    rows = hrq.get_employee_payslips(eid, conn=c)
    assert len(rows) == 1, "control: exactly the one finalized item must return"
    assert rows[0]['carried_in'] == 200.0
    assert rows[0]['carried_out'] == 950.0


def test_get_payroll_runs_includes_carry_total(tmp_db_conn):
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_DISP02', 'run total carry')
    run_id, _item_id = _plant_item(c, '2099-02', eid, carried_out=950.0)
    runs = hrq.get_payroll_runs(conn=c)
    match = [r for r in runs if r['id'] == run_id]
    assert len(match) == 1, "control: the planted run must actually appear"
    assert match[0]['total_carried_out'] == 950.0


def test_get_payroll_runs_carry_total_zero_when_clean(tmp_db_conn):
    """Control counterpart: a run with no carry must total exactly 0."""
    c = tmp_db_conn
    eid = _mk_employee(c, 'T_DISP03', 'clean run')
    run_id, _item_id = _plant_item(c, '2099-03', eid, carried_out=0.0)
    runs = hrq.get_payroll_runs(conn=c)
    match = [r for r in runs if r['id'] == run_id]
    assert len(match) == 1
    assert match[0]['total_carried_out'] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# templates/hr/payslip.html
# ═══════════════════════════════════════════════════════════════════════════

def test_payslip_shows_carried_in_deduction_row(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP04', 'carried-in-visible')
        run_id, item_id = _plant_item(conn, '2099-04', eid, carried_in=200.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}/payslip/{item_id}').get_data(as_text=True)
    assert 'carried-in-visible' in html, "control: the payslip must render for this item"
    assert '>ยกยอดมาจากเดือนก่อน<' in html
    assert f'-{_fmt_baht(200.0)}' in html


def test_payslip_hides_carried_in_row_when_absent(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP05', 'no-carry-in-payslip')
        run_id, item_id = _plant_item(conn, '2099-05', eid, carried_in=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}/payslip/{item_id}').get_data(as_text=True)
    assert 'no-carry-in-payslip' in html, "control: the payslip must render for this item"
    assert '>ยกยอดมาจากเดือนก่อน<' not in html


def test_payslip_shows_carried_out_note(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP06', 'carried-out-visible')
        run_id, item_id = _plant_item(conn, '2099-06', eid,
                                      carried_out=950.0, net_pay=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}/payslip/{item_id}').get_data(as_text=True)
    assert 'carried-out-visible' in html, "control: the payslip must render for this item"
    assert 'data-note="carried-out"' in html
    assert f'ยกไปหักรอบหน้า {_fmt_baht(950.0)}' in html


def test_payslip_hides_carried_out_note_when_absent(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP07', 'no-carry-out-payslip')
        run_id, item_id = _plant_item(conn, '2099-07', eid, carried_out=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}/payslip/{item_id}').get_data(as_text=True)
    assert 'no-carry-out-payslip' in html, "control: the payslip must render for this item"
    assert 'data-note="carried-out"' not in html


# ═══════════════════════════════════════════════════════════════════════════
# templates/hr/payroll_detail.html — items table + run summary
# ═══════════════════════════════════════════════════════════════════════════

def test_payroll_detail_table_shows_carry_values(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP08', 'detail-table-carry')
        run_id, _item_id = _plant_item(conn, '2099-08', eid,
                                       carried_in=200.0, carried_out=950.0,
                                       net_pay=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert 'detail-table-carry' in html, "control: the seeded item must actually render"
    assert (f'<td class="text-end small text-danger" data-col="carried-in">'
            f'-{_fmt_baht(200.0)}</td>') in html
    assert (f'<td class="text-end small text-warning" data-col="carried-out">'
            f'{_fmt_baht(950.0)}</td>') in html


def test_payroll_detail_table_shows_dash_when_no_carry(admin_client, tmp_db):
    """⚠ plan.md P1c / Codex: the empty-items branch (payroll_detail.html's
    `ไม่มีรายการพนักงาน`) would make a bare '–' assertion pass vacuously if
    the row never rendered — control-first: assert the seeded row IS
    visible before asserting the carry cells show the dash."""
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP09', 'detail-table-clean')
        run_id, _item_id = _plant_item(conn, '2099-09', eid,
                                       carried_in=0.0, carried_out=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert 'detail-table-clean' in html, "control: the seeded item must actually render"
    assert '<td class="text-end small " data-col="carried-in">–</td>' in html
    assert '<td class="text-end small " data-col="carried-out">–</td>' in html


def test_payroll_detail_summary_shows_carry_total(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP10', 'summary-carry')
        run_id, _item_id = _plant_item(conn, '2099-10', eid,
                                       carried_out=950.0, net_pay=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert 'summary-carry' in html, "control: the seeded item must actually render"
    assert 'data-note="carry-total"' in html
    assert f'ยกไปหักรอบหน้ารวม {_fmt_baht(950.0)}' in html


def test_payroll_detail_summary_hides_carry_total_when_clean(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP11', 'summary-clean')
        run_id, _item_id = _plant_item(conn, '2098-11', eid, carried_out=0.0)
    finally:
        conn.close()

    html = admin_client.get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert 'summary-clean' in html, "control: the seeded item must actually render"
    assert 'data-note="carry-total"' not in html


# ═══════════════════════════════════════════════════════════════════════════
# CSV export — blueprints/hr.py::payroll_export
# ═══════════════════════════════════════════════════════════════════════════

def test_csv_export_includes_carry_columns_and_values(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP12', 'csv carry row')
        run_id, _item_id = _plant_item(conn, '2098-12', eid,
                                       carried_in=200.0, carried_out=950.0,
                                       net_pay=0.0)
    finally:
        conn.close()

    resp = admin_client.get(f'/hr/payroll/{run_id}/export.csv')
    assert resp.status_code == 200
    # Real CSV parser — never string-split a generated artifact
    # (verification-discipline: "verify with a real parser").
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    header, data_rows = rows[0], rows[1:]
    assert len(header) == 18, "control: header count must reflect the +2 new columns"
    assert 'ยกมาจากเดือนก่อน' in header
    assert 'ยกไปหักรอบหน้า' in header

    matching = [r for r in data_rows if r and r[0] == 'T_DISP12']
    assert len(matching) == 1, "control: the seeded employee's row must be in the export"
    row = matching[0]
    carried_in_idx = header.index('ยกมาจากเดือนก่อน')
    carried_out_idx = header.index('ยกไปหักรอบหน้า')
    assert float(row[carried_in_idx]) == 200.0
    assert float(row[carried_out_idx]) == 950.0


# ═══════════════════════════════════════════════════════════════════════════
# templates/hr/payroll.html — run list
# ═══════════════════════════════════════════════════════════════════════════

def test_payroll_list_shows_carry_total_for_carrying_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP13', 'list-carry')
        run_id, _item_id = _plant_item(conn, '2098-01', eid,
                                       carried_out=950.0, net_pay=0.0)
    finally:
        conn.close()

    html = admin_client.get('/hr/payroll').get_data(as_text=True)
    row_html = _row_html(html, f'/hr/payroll/{run_id}"')
    assert f'data-note="carry-total">ยกไป {_fmt_baht(950.0)}' in row_html


def test_payroll_list_no_carry_total_for_clean_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP14', 'list-clean')
        run_id, _item_id = _plant_item(conn, '2098-02', eid, carried_out=0.0)
    finally:
        conn.close()

    html = admin_client.get('/hr/payroll').get_data(as_text=True)
    row_html = _row_html(html, f'/hr/payroll/{run_id}"')
    assert 'data-note="carry-total"' not in row_html


# ═══════════════════════════════════════════════════════════════════════════
# templates/hr/dashboard.html — mini payroll status table
# ═══════════════════════════════════════════════════════════════════════════

def test_dashboard_shows_carry_total_for_carrying_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP15', 'dash-carry')
        run_id, _item_id = _plant_item(conn, '2098-03', eid,
                                       carried_out=950.0, net_pay=0.0)
    finally:
        conn.close()

    html = admin_client.get('/hr/').get_data(as_text=True)
    row_html = _row_html(html, f'/hr/payroll/{run_id}"')
    assert f'data-note="carry-total">ยกไป {_fmt_baht(950.0)}' in row_html


def test_dashboard_no_carry_total_for_clean_run(admin_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP16', 'dash-clean')
        run_id, _item_id = _plant_item(conn, '2098-04', eid, carried_out=0.0)
    finally:
        conn.close()

    html = admin_client.get('/hr/').get_data(as_text=True)
    row_html = _row_html(html, f'/hr/payroll/{run_id}"')
    assert 'data-note="carry-total"' not in row_html


# ═══════════════════════════════════════════════════════════════════════════
# templates/me/payslip_list.html — employee self-service list
# ═══════════════════════════════════════════════════════════════════════════

def test_payslip_list_shows_carry_note_for_employee(emp_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP17', 'me-carry')
        _link_user(conn, eid, 555)
        _run_id, item_id = _plant_item(conn, '2098-05', eid,
                                       carried_out=950.0, net_pay=0.0)
    finally:
        conn.close()

    html = emp_client.get('/me/payslip').get_data(as_text=True)
    row_html = _row_html(html, f'/me/payslip/{item_id}"')
    assert 'data-note="carried-out"' in row_html
    assert f'ยกไป ฿{950.0:,.2f}' in row_html


def test_payslip_list_no_carry_note_when_clean(emp_client, tmp_db):
    conn = sqlite3.connect(tmp_db, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        eid = _mk_employee(conn, 'T_DISP18', 'me-clean')
        _link_user(conn, eid, 555)
        _run_id, item_id = _plant_item(conn, '2098-06', eid, carried_out=0.0)
    finally:
        conn.close()

    html = emp_client.get('/me/payslip').get_data(as_text=True)
    row_html = _row_html(html, f'/me/payslip/{item_id}"')
    assert 'data-note="carried-out"' not in row_html
