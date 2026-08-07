"""A payroll item may only be edited through its OWN run, and only while that
run is a draft.

`payroll_item_edit` validated the URL's run_id (must be a draft) but never
loaded the item, so ANY draft run id in the URL unlocked ANY item — including
items on a finalized run. `update_payroll_item` compounded it by updating
purely by item id, so a non-route caller bypassed the lock entirely.

Money path: the fields reachable this way (bonus / other_additions /
other_deductions / late) all feed `_recompute_totals`, so a successful bypass
rewrites `gross` and `net_pay` on an issued payslip — and possibly disagrees
with a cashbook salary payment already posted against it.

The pay/unpay routes already carry the missing ownership check
(`item['run_id'] != run_id → abort(404)`), which is the boundary this restores.
"""
import os
import sqlite3

import pytest

import hr


# ── seed ──────────────────────────────────────────────────────────────────────

def _seed(conn):
    """Finalized run 951 (item 9501) + draft run 952 (item 9502), same company.

    2099-xx year_months avoid the UNIQUE(year_month, company_id) collision with
    the real runs carried by the live-DB snapshot.

    `sso_employee` is seeded so the stored gross/net already equal what
    `_recompute_totals` derives — otherwise an edit's "after" figures differ
    from the seed for a reason that has nothing to do with the guard.
    """
    a = conn.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    b = conn.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    # Plain INSERT, not INSERT OR IGNORE: if these ids ever collide the seed
    # must fail loudly rather than silently leaving rows in some other state
    # and letting the assertions pass for the wrong reason.
    conn.executescript(f"""
        INSERT INTO payroll_runs(id,year_month,company_id,status) VALUES
            (951,'2099-07',1,'finalized'), (952,'2099-08',1,'draft');
        INSERT INTO payroll_items
            (id,run_id,employee_id,salary_rate,base_amount,bonus,
             other_additions,other_deductions,sso_employee,gross,net_pay) VALUES
            (9501,951,{a},20000,20000,0,0,0,1000,20000,19000),
            (9502,952,{b},30000,30000,0,0,0,2000,30000,28000);
    """)
    conn.commit()


def _seed_path(db):
    conn = sqlite3.connect(db)
    try:
        _seed(conn)
    finally:
        conn.close()


def _row(conn, item_id):
    conn.row_factory = sqlite3.Row
    return dict(conn.execute(
        "SELECT * FROM payroll_items WHERE id=?", (item_id,)).fetchone())


def _row_path(db, item_id):
    conn = sqlite3.connect(db)
    try:
        return _row(conn, item_id)
    finally:
        conn.close()


def _admin_client():
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


# ── route boundary ────────────────────────────────────────────────────────────

def test_route_refuses_item_from_another_run(tmp_db):
    """The bypass itself: a DRAFT run id in the URL + a FINALIZED run's item id.

    The route saw only run 952 (draft → allowed) and passed item 9501 straight
    to the engine.
    """
    _seed_path(tmp_db)
    before = _row_path(tmp_db, 9501)

    resp = _admin_client().post(
        '/hr/payroll/952/item/9501', data={'bonus': '5000'})

    assert resp.status_code == 404, (
        "editing an item through a run it does not belong to must 404")
    assert _row_path(tmp_db, 9501) == before, \
        "the rejected request must not mutate the item"


def test_route_refuses_item_from_another_draft_run(tmp_db):
    """Ownership is checked regardless of status — a draft item reached through
    a different draft run is still the wrong run."""
    _seed_path(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE payroll_runs SET status='draft' WHERE id=951")
    conn.commit(); conn.close()
    before = _row_path(tmp_db, 9501)

    resp = _admin_client().post(
        '/hr/payroll/952/item/9501', data={'bonus': '5000'})

    assert resp.status_code == 404
    assert _row_path(tmp_db, 9501) == before


def test_route_refuses_unknown_item(tmp_db):
    _seed_path(tmp_db)
    resp = _admin_client().post(
        '/hr/payroll/952/item/999999', data={'bonus': '5000'})
    assert resp.status_code == 404


def test_route_still_edits_an_item_on_its_own_draft_run(tmp_db):
    """Regression guard: the legitimate edit must keep working."""
    _seed_path(tmp_db)

    resp = _admin_client().post(
        '/hr/payroll/952/item/9502', data={'bonus': '5000'})

    assert resp.status_code == 302
    after = _row_path(tmp_db, 9502)
    assert after['bonus'] == 5000
    assert after['gross'] == 35000
    assert after['net_pay'] == 33000


# ── engine boundary (non-route callers) ───────────────────────────────────────

def test_engine_refuses_item_on_a_finalized_run(tmp_db_conn):
    """`update_payroll_item` must re-assert the draft-parent invariant itself:
    the route is not the only caller, and the finalized lock is what keeps an
    issued payslip agreeing with the approved payroll."""
    _seed(tmp_db_conn)
    before = _row(tmp_db_conn, 9501)

    with pytest.raises(ValueError) as exc:
        hr.update_payroll_item(9501, bonus=5000.0, conn=tmp_db_conn)

    assert 'finalized' in str(exc.value).lower() or 'ไม่สามารถแก้ไข' in str(exc.value)
    assert _row(tmp_db_conn, 9501) == before, \
        "a refused engine edit must leave the row untouched"


def test_engine_refuses_late_toggle_on_a_finalized_run(tmp_db_conn):
    """`late` takes a different branch than the money fields — it must be
    locked too (it flips diligence, which changes gross and net_pay)."""
    _seed(tmp_db_conn)
    before = _row(tmp_db_conn, 9501)

    with pytest.raises(ValueError):
        hr.update_payroll_item(9501, late=True, conn=tmp_db_conn)

    assert _row(tmp_db_conn, 9501) == before


def test_engine_still_edits_an_item_on_a_draft_run(tmp_db_conn):
    """Regression guard for the engine half."""
    _seed(tmp_db_conn)

    row = hr.update_payroll_item(9502, bonus=5000.0, conn=tmp_db_conn)

    assert row['bonus'] == 5000
    assert row['gross'] == 35000
    assert row['net_pay'] == 33000
