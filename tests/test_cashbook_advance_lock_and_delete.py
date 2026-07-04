"""Phase 2 — advance row lock + cascade delete (plan.md decision A / C5d,
finding #2). Under the delete+re-add model:

  - editing an advance-linked cashbook row is REJECTED (403) — corrections are
    delete + re-add (before deduction);
  - deleting an advance row that has NOT been deducted cascades: both the
    cashbook row and its salary_advances row go, atomically;
  - deleting an advance that a payroll run already deducted is REJECTED (403),
    re-checked at write time (the gunicorn -w 2 race) with both rows preserved.
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


def _ids(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    acct = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    emp = conn.execute("SELECT id FROM employees WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()
    return acct, emp


def _make_linked_advance(db, account_id, emp_id, amount=500, adate="2026-07-10",
                         deducted_run_id=None):
    """Insert a salary_advances row + a linked cashbook row (bypasses the route,
    for guard/delete tests). `deducted_run_id` set (via a FK-off connection) marks
    it as already deducted."""
    conn = sqlite3.connect(db)               # raw conn: FK not enforced
    adv_id = conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id, deducted_in_run_id)"
        " VALUES (?,?,?,?,?)",
        (emp_id, adate, amount, account_id, deducted_run_id),
    ).lastrowid
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, amount, salary_advance_id, created_by)"
        " VALUES (?,?, 'expense', ?, ?, ?, 'seed')",
        (account_id, adate, ADVANCE_CATEGORY, amount, adv_id),
    ).lastrowid
    conn.commit()
    conn.close()
    return txn_id, adv_id


def test_edit_advance_row_rejected_403(migrated_db):
    account_id, emp_id = _ids(migrated_db)
    txn_id, adv_id = _make_linked_advance(migrated_db, account_id, emp_id)
    c = _client_as_user(1, "admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-07-11",
        "direction": "expense", "category": ADVANCE_CATEGORY, "amount": "999",
    })
    assert resp.status_code == 403, "advance rows are not editable in-place"

    conn = sqlite3.connect(migrated_db)
    amt = conn.execute("SELECT amount FROM cashbook_transactions WHERE id=?", (txn_id,)).fetchone()[0]
    conn.close()
    assert amt == 500, "the row must be unchanged"


def test_delete_advance_not_deducted_cascades(migrated_db):
    account_id, emp_id = _ids(migrated_db)
    txn_id, adv_id = _make_linked_advance(migrated_db, account_id, emp_id)
    c = _client_as_user(1, "admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/delete", follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    n_cb = conn.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE id=?", (txn_id,)).fetchone()[0]
    n_adv = conn.execute("SELECT COUNT(*) FROM salary_advances WHERE id=?", (adv_id,)).fetchone()[0]
    conn.close()
    assert n_cb == 0, "cashbook row deleted"
    assert n_adv == 0, "linked salary_advances row cascade-deleted"


def test_account_ledger_advance_row_shows_lock_not_edit(migrated_db):
    """An advance-linked row in the ledger must NOT offer an edit modal (advances
    aren't editable in place — plan.md A/C5c); it shows a lock hint. Delete stays
    available (the pre-deduction correction path)."""
    account_id, emp_id = _ids(migrated_db)
    txn_id, adv_id = _make_linked_advance(migrated_db, account_id, emp_id, adate="2099-07-10")
    # scope to the seed month so the row is on page 1 (busy accounts paginate)
    html = _client_as_user(1, "admin").get(
        f"/cashbook/account/{account_id}?month=2099-07"
    ).get_data(as_text=True)
    assert f"editTxnModal{txn_id}" not in html, "no edit modal for an advance row"
    assert f"/cashbook/txn/{txn_id}/delete" in html, "delete stays available pre-deduction"
    assert "เบิกล่วงหน้า" in html, "advance lock hint present"


def test_delete_advance_deducted_rejected_403(migrated_db):
    account_id, emp_id = _ids(migrated_db)
    txn_id, adv_id = _make_linked_advance(
        migrated_db, account_id, emp_id, deducted_run_id=1  # simulate deducted
    )
    c = _client_as_user(1, "admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/delete", follow_redirects=False)
    assert resp.status_code == 403, "a deducted advance cannot be deleted"

    conn = sqlite3.connect(migrated_db)
    n_cb = conn.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE id=?", (txn_id,)).fetchone()[0]
    n_adv = conn.execute("SELECT COUNT(*) FROM salary_advances WHERE id=?", (adv_id,)).fetchone()[0]
    conn.close()
    assert n_cb == 1 and n_adv == 1, "both rows preserved when locked"
