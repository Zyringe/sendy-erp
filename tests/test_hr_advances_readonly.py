"""Phase 2 — /hr/advances is a READ-ONLY mirror (plan.md decision C5c,
finding #4). The cashbook (/cashbook/new, category เงินเดือน (เบิกล่วงหน้า)) is
now the sole live writer of salary_advances; the HR add/edit/delete routes are
removed so no second, unlinked entry path can reintroduce drift. Payroll
deduct/undeduct (deducted_in_run_id) still writes — that is not a user edit.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

ADVANCE_CATEGORY = 'เงินเดือน (เบิกล่วงหน้า)'


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _client(role, user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _seed_advance(db):
    conn = sqlite3.connect(db)
    emp = conn.execute("SELECT id FROM employees WHERE is_active=1 LIMIT 1").fetchone()[0]
    aid = conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount) VALUES (?, '2026-07-01', 640)",
        (emp,),
    ).lastrowid
    conn.commit()
    conn.close()
    return aid


def test_advance_new_route_removed(migrated_db):
    emp = sqlite3.connect(migrated_db).execute(
        "SELECT id FROM employees WHERE is_active=1 LIMIT 1"
    ).fetchone()[0]
    r = _client('manager').post('/hr/advances/new', data={
        'employee_id': str(emp), 'advance_date': '2099-02-02', 'amount': '64064',
    })
    assert r.status_code in (302, 404, 405), "HR add-advance route must be gone (deny/redirect or 404)"
    n = sqlite3.connect(migrated_db).execute(
        "SELECT COUNT(*) FROM salary_advances WHERE amount=64064"
    ).fetchone()[0]
    assert n == 0, "no advance created via the removed HR route"


def test_advance_edit_route_removed(migrated_db):
    aid = _seed_advance(migrated_db)
    r = _client('manager').post(f'/hr/advances/{aid}/edit', data={'amount': '9999'})
    assert r.status_code in (302, 404, 405)
    amt = sqlite3.connect(migrated_db).execute(
        "SELECT amount FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0]
    assert amt == 640, "unchanged — the HR edit route is gone"


def test_advance_delete_route_removed(migrated_db):
    aid = _seed_advance(migrated_db)
    r = _client('manager').post(f'/hr/advances/{aid}/delete')
    assert r.status_code in (302, 404, 405)
    n = sqlite3.connect(migrated_db).execute(
        "SELECT COUNT(*) FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0]
    assert n == 1, "still present — the HR delete route is gone"


def test_advances_list_is_readonly_and_points_to_cashbook(migrated_db):
    html = _client('admin').get('/hr/advances').get_data(as_text=True)
    assert '/hr/advances/new' not in html, "no HR add-advance button remains"
    assert '/cashbook/new' in html, "read-only list must point users to the cashbook"
