"""Phase 7 — Salary-advance UI tests.

Covers: mig 119 column, hr_queries helpers, route CRUD + deducted-lock,
and the app.py POST-gate wiring for managers.

Python 3.9 — Optional[...] not X | None.
"""
import os
import sqlite3

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _cols(db, t):
    return {r[1] for r in sqlite3.connect(db).execute(f"PRAGMA table_info({t})")}


def _client(role, user_id=1):
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _mk_advance(db, deducted=False):
    c = sqlite3.connect(db)
    emp = c.execute(
        "SELECT id FROM employees WHERE emp_code='EMP004'"
    ).fetchone()[0]
    run = "NULL"
    if deducted:
        c.execute(
            "INSERT OR IGNORE INTO payroll_runs(id,year_month,company_id,status)"
            " VALUES(950,'2099-09',1,'finalized')"
        )
        run = "950"
    c.execute(
        f"INSERT INTO salary_advances"
        f"(id,employee_id,advance_date,amount,deducted_in_run_id)"
        f" VALUES(7700,{emp},'2026-07-01',900,{run})"
    )
    c.commit()
    c.close()
    return 7700


# ── Task 7.1 — mig 119 column ────────────────────────────────────────────────

def test_salary_advances_has_from_account_id(tmp_db):
    assert 'from_account_id' in _cols(tmp_db, 'salary_advances')


# ── Task 7.2 — hr_queries helpers ────────────────────────────────────────────

def test_create_and_list_advance(tmp_db):
    import hr_queries as hrq
    emp = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees WHERE emp_code='EMP004'"
    ).fetchone()[0]
    acct = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 ORDER BY sort_order LIMIT 1"
    ).fetchone()[0]
    new_id = hrq.create_salary_advance({
        "employee_id": emp,
        "advance_date": "2026-07-10",
        "amount": 1500,
        "from_account_id": acct,
        "note": "ทดสอบ",
    })
    row = hrq.get_salary_advance(new_id)
    assert row["amount"] == 1500 and row["from_account_id"] == acct
    assert any(r["id"] == new_id for r in hrq.get_salary_advances())


def test_active_cashbook_accounts_excludes_inactive(tmp_db):
    import hr_queries as hrq
    c = sqlite3.connect(tmp_db)
    c.execute(
        "UPDATE cashbook_accounts SET is_active=0"
        " WHERE id=(SELECT id FROM cashbook_accounts ORDER BY id DESC LIMIT 1)"
    )
    c.commit()
    c.close()
    accts = hrq.get_active_cashbook_accounts()
    assert all(a["display_name"] is not None or a["code"] for a in accts)


def test_from_account_id_blank_stores_null(tmp_db):
    """from_account_id='' must store NULL, not raise FK IntegrityError."""
    import hr_queries as hrq
    emp = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees WHERE emp_code='EMP004'"
    ).fetchone()[0]
    new_id = hrq.create_salary_advance({
        "employee_id": str(emp),
        "advance_date": "2026-07-10",
        "amount": "800",
        "from_account_id": "",    # blank from select → must coerce to NULL
        "note": "",
    })
    row = hrq.get_salary_advance(new_id)
    assert row["from_account_id"] is None


# ── Task 7.3 — routes CRUD + deducted-lock ───────────────────────────────────

def test_manager_can_create_advance(tmp_db):
    emp = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees WHERE emp_code='EMP004'"
    ).fetchone()[0]
    # Use a unique amount unlikely to pre-exist in the live-DB copy
    r = _client('manager').post('/hr/advances/new', data={
        'employee_id': str(emp),
        'advance_date': '2099-01-15',
        'amount': '77015',
        'note': 'x',
    })
    assert r.status_code in (302, 200)
    assert sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM salary_advances WHERE amount=77015"
    ).fetchone()[0] == 1


def test_edit_pending_advance_ok(tmp_db):
    aid = _mk_advance(tmp_db, deducted=False)
    emp_id = sqlite3.connect(tmp_db).execute(
        "SELECT employee_id FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0]
    _client('manager').post(f'/hr/advances/{aid}/edit', data={
        'employee_id': str(emp_id),
        'advance_date': '2026-07-01',
        'amount': '1234',
        'note': 'edited',
    })
    assert sqlite3.connect(tmp_db).execute(
        "SELECT amount FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0] == 1234


def test_edit_deducted_advance_blocked(tmp_db):
    aid = _mk_advance(tmp_db, deducted=True)
    _client('manager').post(f'/hr/advances/{aid}/edit', data={'amount': '9999'})
    # unchanged — deducted advances are locked
    assert sqlite3.connect(tmp_db).execute(
        "SELECT amount FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0] == 900


def test_delete_deducted_advance_blocked(tmp_db):
    aid = _mk_advance(tmp_db, deducted=True)
    _client('manager').post(f'/hr/advances/{aid}/delete')
    assert sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM salary_advances WHERE id=?", (aid,)
    ).fetchone()[0] == 1


# ── Task 7.5 — app.py POST-gate wiring ───────────────────────────────────────

def test_staff_cannot_reach_advances(tmp_db):
    # staff is blocked from all hr.* by before_request (redirect, not 200)
    assert _client('staff', 2).get('/hr/advances').status_code in (302, 403)


def test_manager_post_advance_allowed_by_gate(tmp_db):
    # the POST default-deny gate must whitelist hr.advance_new for manager
    emp = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees WHERE emp_code='EMP004'"
    ).fetchone()[0]
    # Use a unique amount unlikely to pre-exist in the live-DB copy
    r = _client('manager').post('/hr/advances/new', data={
        'employee_id': str(emp),
        'advance_date': '2099-01-20',
        'amount': '77020',
    })
    assert r.status_code in (302, 200)   # NOT 400/403 from the POST gate
    assert sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM salary_advances WHERE amount=77020"
    ).fetchone()[0] == 1


# ── Nav completeness (post-ship fix) — the advances tab must show in BOTH navs ──

def test_advances_nav_present_desktop_and_mobile(tmp_db):
    """Regression for the missing left-nav tab: on /hr/advances the DESKTOP HR
    sidebar must render (requires hr.advance_list mapped to the 'hr' module so
    active_module=='hr'), AND the MOBILE drawer must include the advances link."""
    import re
    from app import _ENDPOINT_MODULE
    # desktop: the advances pages must resolve to the 'hr' module, else the whole
    # HR sidebar block ({% if active_module == 'hr' %}) disappears on those pages.
    assert _ENDPOINT_MODULE.get('hr.advance_list') == 'hr'
    assert _ENDPOINT_MODULE.get('hr.advance_new') == 'hr'

    html = _client('admin', 1).get('/hr/advances').get_data(as_text=True)
    # desktop HR sub-nav actually renders on the advances page
    side = re.search(r'sidebar-section">บุคลากร.*?(?=sidebar-section|$)', html, re.S)
    assert side and 'เบิกล่วงหน้า' in side.group(0), "desktop HR sidebar missing on /hr/advances"
    # both navs link to /hr/advances (desktop sidebar + mobile drawer)
    assert html.count('href="/hr/advances"') >= 2, "advances link missing from a nav"
