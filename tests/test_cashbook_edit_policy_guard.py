"""The edit route must enforce the SAME category policy as the create route.

Found 2026-08-05 while cleaning up a month of orphaned advance rows: /cashbook/new
hard-blocks the salary-family categories and write-backs the advance category to
`salary_advances`, but `txn_edit` did neither. `_reject_if_advance_edit` only
rejects rows that are ALREADY linked, so an ordinary unlinked row could be edited
INTO `เงินเดือน (เบิกล่วงหน้า)` and would sit there with `salary_advance_id` NULL
— invisible to payroll, never deducted. That is exactly how บอล's ฿1,000 went
un-deducted in run 6.

Edit does not (and must not) write back: /cashbook/new is the SOLE live writer of
salary_advances (plan.md C5c / finding #4). So the fix is to REFUSE the edit and
point the user at delete + re-add, mirroring how an already-linked row behaves.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

ADVANCE_CATEGORY = 'เงินเดือน (เบิกล่วงหน้า)'
SALARY_CATEGORY = 'เงินเดือน'
COMMISSION_CATEGORY = 'จ่ายค่าคอมมิชชั่น'


@pytest.fixture
def migrated_db(tmp_db):
    database.init_db()
    return tmp_db


def _admin_client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['display_name'] = 'Test Admin'
        sess['role'] = 'admin'
    return c


def _plain_row(db):
    """An ordinary manual expense row: no payroll/advance/commission link."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    acct = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0"
        " ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount, description)"
        " VALUES (?,?, 'expense', 'อื่นๆ', NULL, 250, 'ค่าใช้จ่ายทั่วไป')",
        (acct, '2026-08-04'),
    ).lastrowid
    conn.commit()
    conn.close()
    return acct, txn_id


def _post_edit(client, acct, txn_id, category, user_category="หลุย"):
    """Follows the redirect so the response body carries the flash — a refusal
    the operator cannot SEE is barely better than no refusal, and the status
    code alone cannot tell refusal from success (both are 302 before the
    redirect is followed)."""
    return client.post(
        f"/cashbook/txn/{txn_id}/edit",
        data={
            "account_id": str(acct), "txn_date": "2026-08-04", "direction": "expense",
            "category": category, "user_category": user_category, "amount": "250",
            "description": "ค่าใช้จ่ายทั่วไป", "note": "",
        },
        follow_redirects=True,
    )


def _read(db, txn_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT category, salary_advance_id FROM cashbook_transactions WHERE id=?",
        (txn_id,),
    ).fetchone()
    n_adv = conn.execute("SELECT COUNT(*) FROM salary_advances").fetchone()[0]
    conn.close()
    return row, n_adv


def test_edit_into_advance_category_is_refused(migrated_db):
    """The orphan-maker. Editing an ordinary row into the advance category must
    NOT succeed — it cannot write back, so it would create a row payroll never
    deducts."""
    acct, txn_id = _plain_row(migrated_db)
    _, adv_before = _read(migrated_db, txn_id)

    resp = _post_edit(_admin_client(), acct, txn_id, ADVANCE_CATEGORY)

    row, adv_after = _read(migrated_db, txn_id)
    assert row["category"] == 'อื่นๆ', "category must be unchanged"
    assert row["salary_advance_id"] is None
    assert adv_after == adv_before, "no salary_advances row may be created by an edit"
    # The operator must SEE why. Distinctive fragment — 'เบิกล่วงหน้า' alone
    # appears ~14 times on this page and could never fail.
    assert "ให้ลบรายการนี้แล้ว" in resp.get_data(as_text=True)


def test_edit_into_manual_salary_category_is_refused(migrated_db):
    """`เงินเดือน` is hard-blocked on create (sourced in HR payroll). Edit must
    match, or the block is one URL away from being bypassed."""
    acct, txn_id = _plain_row(migrated_db)

    resp = _post_edit(_admin_client(), acct, txn_id, SALARY_CATEGORY)

    row, _ = _read(migrated_db, txn_id)
    assert row["category"] == 'อื่นๆ', "category must be unchanged"
    assert "เงินเดือนบันทึกที่หน้าเงินเดือน" in resp.get_data(as_text=True)


def test_edit_into_in_engine_commission_is_refused(migrated_db):
    """Hybrid block: an in-engine salesperson's commission is sourced at
    /commission. Same reasoning as salary."""
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    rep = conn.execute(
        "SELECT code, name FROM salespersons WHERE is_active=1 ORDER BY code LIMIT 1"
    ).fetchone()
    conn.close()
    if rep is None:
        pytest.skip("no active salesperson in this DB")

    acct, txn_id = _plain_row(migrated_db)
    resp = _post_edit(_admin_client(), acct, txn_id, COMMISSION_CATEGORY,
                      user_category=rep["name"])

    row, _ = _read(migrated_db, txn_id)
    assert row["category"] == 'อื่นๆ', "category must be unchanged"
    assert "คอมมิชชั่นของเซลส์ในระบบบันทึก" in resp.get_data(as_text=True)


def test_ordinary_edit_still_works(migrated_db):
    """Regression guard: the block must not break normal edits."""
    acct, txn_id = _plain_row(migrated_db)

    resp = _post_edit(_admin_client(), acct, txn_id, 'ค่าน้ำมัน')

    row, _ = _read(migrated_db, txn_id)
    assert row["category"] == 'ค่าน้ำมัน', "an ordinary category change must still apply"
    assert resp.status_code == 200
    assert "ให้ลบรายการนี้แล้ว" not in resp.get_data(as_text=True)
