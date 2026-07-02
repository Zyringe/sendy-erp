"""Phase 2 — Batch manual entry + edit/delete + roles (cashbook-manual-entry plan).

Covers the logic-bearing bits called out in the plan's Phase 2 test list:
  - batch happy path (blank rows skipped, created_by stamped)
  - all-or-nothing rollback on a bad row
  - new-category upsert (no duplicate on repeat)
  - edit/delete guard rejecting a salary row (payroll_item_id set)
  - role gate: manager/shareholder can POST; staff is blocked entirely
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


def _client_as(role, user_id=1):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role'] = role
    return c


def _active_account(tmp_db, exclude_transfer=True):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, code FROM cashbook_accounts WHERE is_active=1"
    if exclude_transfer:
        sql += " AND is_transfer=0"
    sql += " ORDER BY id LIMIT 1"
    row = conn.execute(sql).fetchone()
    conn.close()
    if row is None:
        pytest.skip("No active non-transfer cashbook account in live DB clone")
    return row["id"]


def _payroll_run_and_item(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, run_id FROM payroll_items LIMIT 1").fetchone()
    conn.close()
    if row is None:
        pytest.skip("No payroll_items in live DB clone")
    return row["run_id"], row["id"]


def _insert_manual_row(tmp_db, account_id, amount=100.0, category="ทดสอบ"):
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        """INSERT INTO cashbook_transactions
           (account_id, txn_date, direction, category, amount, created_by)
           VALUES (?, '2026-06-01', 'expense', ?, ?, 'seed')""",
        (account_id, category, amount),
    )
    conn.commit()
    txn_id = cur.lastrowid
    conn.close()
    return txn_id


def _insert_salary_row(tmp_db, account_id, run_id, item_id, amount=15000.0):
    conn = sqlite3.connect(tmp_db)
    cur = conn.execute(
        """INSERT INTO cashbook_transactions
           (account_id, txn_date, direction, category, amount, created_by,
            payroll_run_id, payroll_item_id)
           VALUES (?, '2026-06-01', 'expense', 'เงินเดือน', ?, 'seed', ?, ?)""",
        (account_id, amount, run_id, item_id),
    )
    conn.commit()
    txn_id = cur.lastrowid
    conn.close()
    return txn_id


def _batch_form(account_id, txn_date="2026-06-01", rows=None):
    """Build a rows-<i>-* form dict for POST /cashbook/new."""
    data = {"txn_date": txn_date, "account_id": str(account_id)}
    for i, row in enumerate(rows or []):
        data[f"rows-{i}-direction"] = row.get("direction", "expense")
        data[f"rows-{i}-category"] = row.get("category", "")
        data[f"rows-{i}-user_category"] = row.get("user_category", "")
        data[f"rows-{i}-amount"] = row.get("amount", "")
        data[f"rows-{i}-description"] = row.get("description", "")
        data[f"rows-{i}-note"] = row.get("note", "")
    return data


# ── Batch happy path ──────────────────────────────────────────────────────────

def test_batch_happy_path_inserts_and_stamps_created_by(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    form = _batch_form(account_id, rows=[
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": "100"},
        {"direction": "income",  "category": "ขายเศษเหล็ก", "amount": "250.50"},
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": ""},  # blank — skipped
    ])
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM cashbook_transactions"
        " WHERE account_id=? AND txn_date='2026-06-01' AND created_by='Test Admin'"
        " ORDER BY id", (account_id,),
    ).fetchall()
    conn.close()
    assert len(rows) == 2, "blank row must be skipped; exactly 2 rows inserted"
    assert all(r["created_by"] == "Test Admin" for r in rows)
    amounts = sorted(r["amount"] for r in rows)
    assert amounts == [100.0, 250.5]


# ── All-or-nothing ────────────────────────────────────────────────────────────

def test_all_or_nothing_bad_row_inserts_nothing(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    before = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
    ).fetchone()[0]

    form = _batch_form(account_id, rows=[
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": "100"},
        {"direction": "expense", "category": "ค่าไฟ",    "amount": "200"},
        {"direction": "expense", "category": "ของเสีย",   "amount": "0"},  # invalid: amount must be > 0
    ])
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200, "validation failure re-renders the form (no redirect)"
    assert "จำนวนเงินต้องมากกว่า 0" in resp.get_data(as_text=True)

    after = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
    ).fetchone()[0]
    assert after == before, "a single bad row must roll back the WHOLE batch"


# ── New-category upsert ───────────────────────────────────────────────────────

def test_new_category_upserted_once_no_duplicate_on_repeat(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    cat_name = "หมวดใหม่ทดสอบ_XYZ"

    form1 = _batch_form(account_id, rows=[
        {"direction": "expense", "category": cat_name, "amount": "50"},
    ])
    r1 = c.post("/cashbook/new", data=form1)
    assert r1.status_code == 302

    conn = sqlite3.connect(tmp_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories WHERE name=? AND direction='expense'",
        (cat_name,),
    ).fetchone()[0]
    conn.close()
    assert count == 1

    # Post again with the SAME category — must not create a duplicate row
    # (UNIQUE(name, direction) + INSERT OR IGNORE).
    form2 = _batch_form(account_id, rows=[
        {"direction": "expense", "category": cat_name, "amount": "75"},
    ])
    r2 = c.post("/cashbook/new", data=form2)
    assert r2.status_code == 302

    conn = sqlite3.connect(tmp_db)
    count2 = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories WHERE name=? AND direction='expense'",
        (cat_name,),
    ).fetchone()[0]
    conn.close()
    assert count2 == 1, "no duplicate category row on repeat use"


# ── Edit/delete guard: salary rows are locked ────────────────────────────────

def test_edit_rejects_salary_row(tmp_db):
    account_id = _active_account(tmp_db)
    run_id, item_id = _payroll_run_and_item(tmp_db)
    txn_id = _insert_salary_row(tmp_db, account_id, run_id, item_id)

    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-06-02",
        "direction": "expense", "category": "เงินเดือน", "amount": "99999",
    })
    assert resp.status_code == 403

    row = sqlite3.connect(tmp_db).execute(
        "SELECT amount, txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == 15000.0 and row[1] == "2026-06-01", "salary row must be unchanged"


def test_delete_rejects_salary_row(tmp_db):
    account_id = _active_account(tmp_db)
    run_id, item_id = _payroll_run_and_item(tmp_db)
    txn_id = _insert_salary_row(tmp_db, account_id, run_id, item_id)

    c = _client_as("manager")
    resp = c.post(f"/cashbook/txn/{txn_id}/delete", data={})
    assert resp.status_code == 403

    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row is not None, "salary row must still exist"


# ── Delete a manual row → audit_log DELETE row present ───────────────────────

def test_delete_manual_row_writes_audit_log(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _insert_manual_row(tmp_db, account_id)

    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/delete", data={}, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row is None, "row must be gone"

    audit = conn.execute(
        "SELECT * FROM audit_log WHERE table_name='cashbook_transactions'"
        " AND row_id=? AND action='DELETE'", (txn_id,),
    ).fetchall()
    conn.close()
    assert len(audit) >= 1
    assert any(a["user"] == "Test Admin" for a in audit), \
        "at least one DELETE audit_log row must attribute the actor"


def test_edit_manual_row_updates_and_writes_audit_log(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _insert_manual_row(tmp_db, account_id, amount=100.0)

    c = _client_as("manager")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-06-05",
        "direction": "expense", "category": "ค่าน้ำมัน", "amount": "321.50",
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row["amount"] == 321.5
    assert row["category"] == "ค่าน้ำมัน"

    audit = conn.execute(
        "SELECT * FROM audit_log WHERE table_name='cashbook_transactions'"
        " AND row_id=? AND action='UPDATE'", (txn_id,),
    ).fetchall()
    conn.close()
    assert any(a["user"] == "Test Manager" for a in audit)


# ── Role gate ─────────────────────────────────────────────────────────────────

def test_manager_can_post_new_transaction(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("manager")
    form = _batch_form(account_id, rows=[
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": "10"},
    ])
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 302


def test_shareholder_can_post_new_transaction(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("shareholder")
    form = _batch_form(account_id, rows=[
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": "10"},
    ])
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 302


def test_staff_get_new_transaction_redirected(tmp_db):
    c = _client_as("staff")
    resp = c.get("/cashbook/new", follow_redirects=False)
    assert resp.status_code == 302
    # before_request's cashbook.* staff-block redirects to the main dashboard,
    # never renders the form.
    assert "/cashbook" not in resp.headers.get("Location", "")


def test_staff_post_new_transaction_blocked(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("staff")
    form = _batch_form(account_id, rows=[
        {"direction": "expense", "category": "ค่าน้ำมัน", "amount": "10"},
    ])
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE account_id=?"
        " AND category='ค่าน้ำมัน' AND amount=10", (account_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 0, "staff POST must not insert anything"
