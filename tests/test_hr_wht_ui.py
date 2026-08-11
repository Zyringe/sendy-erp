"""P2 — WHT (ภาษีหัก ณ ที่จ่าย) UI surfaces read/write `payroll_items.wht_amount`
and `employee_wht_history` (P1's engine, see hr.py::resolve_wht).

Covers: the ภาษี column + totals row on payroll_detail.html, the item-edit
form/route round-trip, the CSV export column, the ภาษี line on payslip.html
(both the admin route and the employee self-service /me route), and the
employee-detail WHT-history view + add-row route.

Render assertions target an ELEMENT or a VALUE tied to a distinctive seeded
number — never a bare Thai substring (see .claude/rules/verification-discipline.md
— 'ภาษี' alone is not even a substring trap here since it doesn't appear
anywhere on these pages pre-change, but the discipline is followed anyway by
asserting on formatted amounts / regex-extracted attribute values).
"""
import csv
import io
import os
import re
import sqlite3

import pytest


# ── helpers (mirrors tests/test_payroll_item_run_ownership.py) ──────────────

def _client_as(role, user_id=1):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _seed_run_with_items(conn):
    """Draft run 9601 (company 1) with 2 items on two distinct employees.

    wht_amount 32 and 10 (sum 42) are chosen to be distinctive — no other
    summed column on this run is 0 except these two, so a bare '42.00' /
    '32.00' hit in the totals row can only come from wht_amount.
    """
    a = conn.execute("SELECT id FROM employees WHERE emp_code='EMP001'").fetchone()[0]
    b = conn.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    conn.executescript(f"""
        INSERT INTO payroll_runs(id,year_month,company_id,status) VALUES
            (9601,'2099-09',1,'draft');
        INSERT INTO payroll_items
            (id,run_id,employee_id,salary_rate,base_amount,bonus,
             other_additions,other_deductions,wht_amount,sso_employee,
             gross,net_pay) VALUES
            (96011,9601,{a},20000,20000,0,0,0,32.0,0,20000,19968),
            (96012,9601,{b},15000,15000,0,0,0,10.0,0,15000,14990);
    """)
    conn.commit()
    return 9601, 96011, 96012


def _seed_finalized_item(conn):
    """Finalized run 9602 / item 96021, wht_amount=43.21 (a value distinctive
    enough that no other rendered figure on the page could coincidentally
    match it)."""
    a = conn.execute("SELECT id FROM employees WHERE emp_code='EMP001'").fetchone()[0]
    conn.executescript(f"""
        INSERT INTO payroll_runs(id,year_month,company_id,status) VALUES
            (9602,'2099-10',1,'finalized');
        INSERT INTO payroll_items
            (id,run_id,employee_id,salary_rate,base_amount,bonus,
             other_additions,other_deductions,wht_amount,sso_employee,
             gross,net_pay) VALUES
            (96021,9602,{a},20000,20000,0,0,0,43.21,0,20000,19956.79);
    """)
    conn.commit()
    return 9602, 96021


# ── payroll_detail.html — ภาษี column ────────────────────────────────────────

def test_payroll_detail_shows_wht_column_header(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    html = _client_as('admin').get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert '>ภาษี<' in html, "must be its own column header, not merged into รายการหัก"


def test_payroll_detail_wht_cell_shows_the_amount(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    html = _client_as('admin').get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert '-฿32.00' in html
    assert '-฿10.00' in html
    # net_pay must still reconcile (P1's money math, unmoved by P2's UI change)
    assert '฿19,968.00' in html
    assert '฿14,990.00' in html


def test_payroll_detail_totals_row_sums_wht(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    html = _client_as('admin').get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    assert '-฿42.00' in html, "totals row must sum wht_amount (32 + 10)"


def test_payroll_detail_edit_modal_has_wht_input_prefilled(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    html = _client_as('admin').get(f'/hr/payroll/{run_id}').get_data(as_text=True)
    m = re.search(r'name="wht_amount"[^>]*value="([\d.]+)"', html)
    assert m, "the edit modal must have a wht_amount input"
    assert float(m.group(1)) == 32.0


# ── item-edit route round-trips wht_amount ───────────────────────────────────

def test_payroll_item_edit_updates_wht_amount(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    resp = _client_as('admin').post(
        f'/hr/payroll/{run_id}/item/{i1}', data={'wht_amount': '99.50'})
    assert resp.status_code == 302
    row = dict(tmp_db_conn.execute(
        "SELECT wht_amount, gross, net_pay FROM payroll_items WHERE id=?", (i1,)
    ).fetchone())
    assert row['wht_amount'] == 99.5
    assert row['net_pay'] == 20000 - 99.5, "net_pay must recompute from the new wht_amount"


# ── CSV export ────────────────────────────────────────────────────────────────

def test_csv_export_has_wht_column(tmp_db, tmp_db_conn):
    run_id, i1, i2 = _seed_run_with_items(tmp_db_conn)
    resp = _client_as('admin').get(f'/hr/payroll/{run_id}/export.csv')
    text = resp.get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    assert 'ภาษี' in header, "CSV header row must carry a WHT column"
    idx = header.index('ภาษี')
    data_values = {float(r[idx]) for r in rows[1:] if r}
    assert data_values == {32.0, 10.0}


# ── payslip.html (admin route) ───────────────────────────────────────────────

def test_admin_payslip_shows_wht_line(tmp_db, tmp_db_conn):
    run_id, item_id = _seed_finalized_item(tmp_db_conn)
    html = _client_as('admin').get(
        f'/hr/payroll/{run_id}/payslip/{item_id}').get_data(as_text=True)
    assert '-฿43.21' in html
    assert '฿19,956.79' in html, "net pay must reconcile on the payslip"


# ── /me/payslip/<id> — same template, employee self-service route ───────────
# Uses the REAL known-good fixture (item 38, run 3, EMP001/user_id 1,
# wht_amount=32, gross 35000, net 34968) already in this worktree's DB —
# no seeding needed, and it doubles as the acceptance oracle for P1+P2 tying
# together on data that predates this branch.

def test_me_payslip_shows_wht_line_on_real_fixture(tmp_db, tmp_db_conn):
    row = tmp_db_conn.execute(
        "SELECT wht_amount, net_pay FROM payroll_items WHERE id=38").fetchone()
    assert row['wht_amount'] == 32.0, "fixture assumption drifted — see plan.md"

    html = _client_as('staff', user_id=1).get(
        '/me/payslip/38').get_data(as_text=True)
    assert '-฿32.00' in html
    assert '฿34,968.00' in html


# ── employee_detail.html — WHT history view + add route ─────────────────────

def test_employee_detail_shows_wht_history(tmp_db, tmp_db_conn):
    # employee_id 1 already carries a migration-seeded WHT history row
    # (2026-05-01, ฿32.00, initial) — see p1-progress.md.
    html = _client_as('admin').get('/hr/employees/1').get_data(as_text=True)
    assert 'ประวัติภาษีหัก ณ ที่จ่าย' in html
    assert '฿32.00' in html


def test_employee_wht_add_route_writes_history_row(tmp_db, tmp_db_conn):
    resp = _client_as('admin').post(
        '/hr/employees/1/wht',
        data={'effective_date': '2099-01-01', 'monthly_wht': '55',
              'reason': 'adjust', 'note': 'test'})
    assert resp.status_code == 302
    row = tmp_db_conn.execute(
        """SELECT monthly_wht, reason, note FROM employee_wht_history
            WHERE employee_id=1 AND effective_date='2099-01-01'"""
    ).fetchone()
    assert row is not None
    assert row['monthly_wht'] == 55.0
    assert row['reason'] == 'adjust'
    assert row['note'] == 'test'


# ── guards that were NOT pinned until Codex round 3 (2026-08-11) ────────────
# Both of these were removable with the whole suite still green, because a
# NEIGHBOUR happened to produce the same outcome: the DB CHECK rejected the
# negative value, and the global before_request gate rejected the non-admin
# POST. Shape #6 in .claude/rules/verification-discipline.md — "the test
# exercises a neighbour, not the subject".

def test_negative_wht_post_is_rejected_by_the_ROUTE_not_the_db(
        tmp_db, tmp_db_conn, monkeypatch):
    """The route's own `wht_value < 0` check must reject BEFORE the write layer.
    Asserting only "no row appeared" cannot distinguish the route's friendly
    refusal from the DB CHECK firing underneath it, so this stubs the write
    helper and asserts it is never reached — and checks the specific flash."""
    import blueprints.hr as bp_hr
    calls = []
    monkeypatch.setattr(bp_hr.hrq, 'add_wht_history',
                        lambda *a, **k: calls.append(a))

    c = _client_as('admin')
    resp = c.post('/hr/employees/1/wht',
                  data={'effective_date': '2098-01-01', 'monthly_wht': '-32',
                        'reason': 'adjust', 'note': ''},
                  follow_redirects=True)
    assert resp.status_code == 200
    assert calls == [], "route reached the write layer with a negative value"
    assert 'จำนวนภาษีติดลบไม่ได้' in resp.get_data(as_text=True)
    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM employee_wht_history WHERE effective_date='2098-01-01'"
    ).fetchone()[0] == 0

    # CONTROL: the same route with a valid value DOES reach the write layer,
    # so the assertion above is about the sign, not about the stub.
    c.post('/hr/employees/1/wht',
           data={'effective_date': '2098-02-01', 'monthly_wht': '32',
                 'reason': 'adjust', 'note': ''})
    assert len(calls) == 1, "control: a valid POST should have called add_wht_history"


def test_employee_wht_add_route_has_its_own_admin_check(tmp_db, tmp_db_conn):
    """Defense in depth: the view itself must refuse a non-admin even when the
    global request gate is bypassed. Calling the view function directly inside a
    request context is what removes the neighbour from the picture — going
    through the test client only proves the global gate works."""
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    import blueprints.hr as bp_hr
    from werkzeug.exceptions import HTTPException

    before = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM employee_wht_history").fetchone()[0]
    with a.test_request_context(
            '/hr/employees/1/wht', method='POST',
            data={'effective_date': '2097-01-01', 'monthly_wht': '32',
                  'reason': 'adjust', 'note': ''}):
        from flask import session
        session['user_id'] = 2
        session['username'] = 'staffer'
        session['role'] = 'staff'
        with pytest.raises(HTTPException) as e:
            bp_hr.employee_wht_add(1)
        assert e.value.code == 403

    assert tmp_db_conn.execute(
        "SELECT COUNT(*) FROM employee_wht_history").fetchone()[0] == before
