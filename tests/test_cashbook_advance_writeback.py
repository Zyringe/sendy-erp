"""Phase 2 — cashbook-sourced advance write-back (plan.md decision C5,
finding #2). Saving a `เงินเดือน (เบิกล่วงหน้า)` row on /cashbook/new must
insert BOTH a salary_advances row and a linked cashbook_transactions row in ONE
commit; a missing/invalid employee rejects the row and inserts nothing.

Relies on mig 128 (salary_advance_id + the seeded category). Tests apply it via
database.init_db() on the tmp_db clone (SKIP_DB_INIT=1 keeps import cheap).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

ADVANCE_CATEGORY = 'เงินเดือน (เบิกล่วงหน้า)'


@pytest.fixture
def migrated_db(tmp_db):
    """tmp_db with mig 128 applied (the live-clone predates Phase 2)."""
    database.init_db()
    return tmp_db


def _client_as_user(user_id, role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _active_account(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("no active non-transfer cashbook account")
    return row["id"]


def _active_employee(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, COALESCE(nickname, full_name) AS display FROM employees"
        " WHERE is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("no active employee")
    return row["id"], row["display"]


# ── front-end contract: /cashbook/new renders the advance employee picker ─────

def test_new_page_renders_advance_employee_picker(migrated_db):
    html = _client_as_user(1, "admin").get("/cashbook/new").get_data(as_text=True)
    assert "advance-emp-select" in html, "employee picker rendered"
    assert "rows-0-employee_id" in html, "per-row employee field name present"
    assert "advanceHistoryModal" in html, "ดูประวัติ modal present"
    # advance_category is wired into the page JS (the Thai string itself is
    # \u-escaped by tojson, so assert on the JS var + a category option count).
    assert "var ADVANCE_CAT" in html, "advance category var wired into the page JS"


# ── happy path: both rows written + linked ────────────────────────────────────

def test_advance_row_writes_both_rows_linked(migrated_db):
    account_id = _active_account(migrated_db)
    emp_id, emp_display = _active_employee(migrated_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-07-02",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(emp_id),
        "rows-0-amount": "500",
        "rows-0-note": "เบิกล่วงหน้า ก.ค.",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:800]

    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    adv = conn.execute(
        "SELECT * FROM salary_advances WHERE employee_id=? AND advance_date='2026-07-02'"
        " AND amount=500 ORDER BY id DESC LIMIT 1",
        (emp_id,),
    ).fetchone()
    assert adv is not None, "salary_advances row must be written"
    assert adv["from_account_id"] == account_id

    cb = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE salary_advance_id=?", (adv["id"],)
    ).fetchone()
    conn.close()
    assert cb is not None, "cashbook row must be linked to the advance"
    assert cb["account_id"] == account_id
    assert cb["direction"] == "expense"
    assert cb["category"] == ADVANCE_CATEGORY
    assert cb["amount"] == 500
    assert cb["user_category"] == emp_display, "ผู้ใช้ tag auto-filled from the employee"


# ── validation: no employee -> reject, insert nothing ─────────────────────────

def test_advance_row_missing_employee_rejected_nothing_inserted(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-07-03",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-amount": "700",
        # no employee_id
    }
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200, "missing employee re-renders the form"

    conn = sqlite3.connect(migrated_db)
    n_cb = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-03' AND amount=700"
    ).fetchone()[0]
    n_adv = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE advance_date='2026-07-03' AND amount=700"
    ).fetchone()[0]
    conn.close()
    assert n_cb == 0 and n_adv == 0, "no rows written when the advance employee is missing"


def test_advance_row_invalid_employee_rejected(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-07-04",
        "account_id": str(account_id),
        "rows-0-direction": "expense",
        "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": "999999",   # nonexistent
        "rows-0-amount": "800",
    }
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200

    conn = sqlite3.connect(migrated_db)
    n_adv = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE advance_date='2026-07-04' AND amount=800"
    ).fetchone()[0]
    conn.close()
    assert n_adv == 0


# ── atomicity: an unconfirmed duplicate elsewhere in the batch aborts the whole
#    save, so the advance's salary_advances row is NOT written either ──────────

def test_batch_aborts_before_advance_writeback_on_unconfirmed_duplicate(migrated_db):
    account_id = _active_account(migrated_db)
    emp_id, _ = _active_employee(migrated_db)
    # seed an existing plain row that row-1 will duplicate
    conn = sqlite3.connect(migrated_db)
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount, created_by)"
        " VALUES (?, '2026-07-05', 'expense', 'ค่าน้ำมัน', '', 123, 'seed')",
        (account_id,),
    )
    conn.commit()
    conn.close()

    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-07-05",
        "account_id": str(account_id),
        "bulk_mode": "1",
        # row 0: a valid advance
        "rows-0-direction": "expense", "rows-0-category": ADVANCE_CATEGORY,
        "rows-0-employee_id": str(emp_id), "rows-0-amount": "900",
        # row 1: an unconfirmed duplicate of the seeded row
        "rows-1-direction": "expense", "rows-1-category": "ค่าน้ำมัน",
        "rows-1-amount": "123",
    }
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200, "unconfirmed duplicate re-renders, no save"

    conn = sqlite3.connect(migrated_db)
    n_adv = conn.execute(
        "SELECT COUNT(*) FROM salary_advances WHERE employee_id=? AND amount=900",
        (emp_id,),
    ).fetchone()[0]
    conn.close()
    assert n_adv == 0, "advance write-back must not happen when the batch is rejected"
