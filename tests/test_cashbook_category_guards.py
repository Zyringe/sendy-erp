"""Issue #532 §1 — cashbook category guards.

Decided with Put 2026-09-15 (cashbook review): the entry form used to accept
any typed category and silently upsert it. Now:
  - New entry: a brand-new category name needs explicit confirmation before
    it's created — warn-then-confirm, same shape as the duplicate-row guard
    (test_cashbook_new_bulk_and_defaults.py). Nothing saves unconfirmed.
  - New entry: a category that EXISTS but is retired (is_active=0) is
    refused outright, on entry AND in bulk mode (blocks the whole batch,
    matching every other basic-validation failure).
  - Edit: the category must be ACTIVE, or the row's current (category,
    direction) left UNCHANGED (a row already sitting in a retired category
    stays editable on its other fields). Edit never creates a category.
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


def _active_account(tmp_db):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0"
        " ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("No active non-transfer cashbook account in live DB clone")
    return row[0]


def _seed_retired_category(tmp_db, name, direction="expense"):
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT OR REPLACE INTO cashbook_categories(name, direction, is_active)"
        " VALUES (?, ?, 0)",
        (name, direction),
    )
    conn.commit()
    conn.close()


def _plain_row(tmp_db, account_id, category="อื่นๆ", direction="expense", amount=250.0):
    """An ordinary manual row, category 'อื่นๆ' (a real active category —
    verified against the live DB clone)."""
    conn = sqlite3.connect(tmp_db)
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount)"
        " VALUES (?, '2026-08-02', ?, ?, NULL, ?)",
        (account_id, direction, category, amount),
    ).lastrowid
    conn.commit()
    conn.close()
    return txn_id


# ── New entry: new-category confirm ──────────────────────────────────────────

def test_new_category_first_submit_shows_confirm_and_saves_nothing(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    cat_name = "หมวดยืนยันใหม่ 532"
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-01", "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "40",
    })
    assert resp.status_code == 200, "first submit re-renders asking to confirm, no redirect"
    html = resp.get_data(as_text=True)
    assert cat_name in html
    assert 'name="confirm_new_categories"' in html

    conn = sqlite3.connect(tmp_db)
    n_txn = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE category=?", (cat_name,)
    ).fetchone()[0]
    n_cat = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories WHERE name=?", (cat_name,)
    ).fetchone()[0]
    conn.close()
    assert n_txn == 0, "nothing saved before confirmation"
    assert n_cat == 0, "category must not be created before confirmation"


def test_new_category_confirmed_saves_row_and_creates_category(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    cat_name = "หมวดยืนยันใหม่ 532b"
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-01", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "40",
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    n_txn = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE category=? AND amount=40", (cat_name,)
    ).fetchone()[0]
    cat = conn.execute(
        "SELECT is_active FROM cashbook_categories WHERE name=? AND direction='expense'",
        (cat_name,),
    ).fetchone()
    conn.close()
    assert n_txn == 1
    assert cat is not None and cat[0] == 1


def test_new_category_repeat_use_no_duplicate_category_row(tmp_db):
    """Confirming a new category, then using it again later, must not
    require re-confirming (the SECOND use is an existing category)."""
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    cat_name = "หมวดยืนยันใหม่ 532c"
    r1 = c.post("/cashbook/new", data={
        "txn_date": "2026-08-01", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "10",
    }, follow_redirects=False)
    assert r1.status_code == 302

    r2 = c.post("/cashbook/new", data={
        "txn_date": "2026-08-02", "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "20",
    }, follow_redirects=False)
    assert r2.status_code == 302, "an already-confirmed category must not re-trigger the confirm screen"

    conn = sqlite3.connect(tmp_db)
    n_cat = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories WHERE name=? AND direction='expense'",
        (cat_name,),
    ).fetchone()[0]
    conn.close()
    assert n_cat == 1, "no duplicate category row"


# ── New entry: inactive category refused ─────────────────────────────────────

def test_new_entry_inactive_category_refused_nothing_saved(tmp_db):
    account_id = _active_account(tmp_db)
    cat_name = "หมวดปิดใช้งาน 532"
    _seed_retired_category(tmp_db, cat_name)
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-01", "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "40",
    })
    assert resp.status_code == 200, "inactive category re-renders, no redirect"
    assert "ปิดใช้งาน" in resp.get_data(as_text=True)

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE category=?", (cat_name,)
    ).fetchone()[0]
    still_inactive = conn.execute(
        "SELECT is_active FROM cashbook_categories WHERE name=?", (cat_name,)
    ).fetchone()[0]
    conn.close()
    assert n == 0
    assert still_inactive == 0, "category stays inactive — never silently saved"


def test_new_entry_bulk_one_row_inactive_category_blocks_whole_batch(tmp_db):
    """AC: 'nothing saved' — an inactive-category row blocks the WHOLE batch,
    even an otherwise-valid sibling row, matching every other
    basic-validation failure (row_errors)."""
    account_id = _active_account(tmp_db)
    cat_name = "หมวดปิดใช้งาน batch 532"
    _seed_retired_category(tmp_db, cat_name)
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-01", "account_id": str(account_id), "bulk_mode": "1",
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "40",
        "rows-1-direction": "expense", "rows-1-category": "ค่าน้ำมัน", "rows-1-amount": "20",
    })
    assert resp.status_code == 200

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE (category=? AND amount=40) OR (category='ค่าน้ำมัน' AND amount=20)",
        (cat_name,),
    ).fetchone()[0]
    conn.close()
    assert n == 0, "an inactive-category row blocks the WHOLE batch, even the valid sibling"


# ── Edit: active-or-unchanged, never creates ─────────────────────────────────

def test_edit_into_nonexistent_category_refused_no_category_created(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _plain_row(tmp_db, account_id)
    cat_name = "หมวดไม่มีจริง 532"
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-02", "direction": "expense",
        "category": cat_name, "amount": "250",
    }, follow_redirects=True)
    assert "ไม่มีอยู่หรือถูกปิดใช้งาน" in resp.get_data(as_text=True)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT category FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    n_cat = conn.execute(
        "SELECT COUNT(*) FROM cashbook_categories WHERE name=?", (cat_name,)
    ).fetchone()[0]
    conn.close()
    assert row[0] == "อื่นๆ", "category must be unchanged"
    assert n_cat == 0, "edit must never create a category"


def test_edit_into_inactive_category_refused(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _plain_row(tmp_db, account_id)
    cat_name = "หมวดปิดใช้งานแก้ไข 532"
    _seed_retired_category(tmp_db, cat_name)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-02", "direction": "expense",
        "category": cat_name, "amount": "250",
    }, follow_redirects=True)
    assert "ไม่มีอยู่หรือถูกปิดใช้งาน" in resp.get_data(as_text=True)

    row = sqlite3.connect(tmp_db).execute(
        "SELECT category FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "อื่นๆ", "category must be unchanged"


def test_edit_row_sitting_in_inactive_category_other_fields_still_editable(tmp_db):
    """Rows 723/664/665 (issue #532) sit in retired categories on purpose —
    leaving the category untouched while editing another field must still
    work."""
    account_id = _active_account(tmp_db)
    cat_name = "หมวดปิดใช้งานเดิม 532"
    _seed_retired_category(tmp_db, cat_name)
    txn_id = _plain_row(tmp_db, account_id, category=cat_name, amount=100.0)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-02", "direction": "expense",
        "category": cat_name, "amount": "999",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT category, amount FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == cat_name, "category untouched even though it's retired"
    assert row[1] == 999.0, "the amount edit must still apply"


def test_edit_into_active_category_succeeds(tmp_db):
    account_id = _active_account(tmp_db)
    txn_id = _plain_row(tmp_db, account_id)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-02", "direction": "expense",
        "category": "ค่าน้ำมัน", "amount": "250",
    }, follow_redirects=False)
    assert resp.status_code == 302
    row = sqlite3.connect(tmp_db).execute(
        "SELECT category FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "ค่าน้ำมัน"
