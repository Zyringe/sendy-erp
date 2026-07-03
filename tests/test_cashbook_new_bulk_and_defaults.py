"""Phase 1b — /cashbook/new per-user default account, single/bulk entry,
per-row date, duplicate-row guard (projects/cashbook-entry-reconcile/plan.md,
decisions A3, B1-B3, D2).

Builds on Phase 1a (mig 126 `users.default_cashbook_account_id`, shipped on
this same branch) and the existing batch-entry route
(`test_cashbook_manual_entry.py` covers the all-or-nothing / role-gate /
audit-log behavior — untouched by this phase, not re-tested here).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest


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


def _account_id_by_code(tmp_db, code):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT id FROM cashbook_accounts WHERE code=?", (code,)).fetchone()
    conn.close()
    if row is None:
        pytest.skip(f"No cashbook_accounts.code={code!r} in live DB clone")
    return row[0]


def _numbered_amount_fields(html):
    """Distinct numeric row indices actually rendered (excludes the inert
    `__IDX__` row-template placeholder)."""
    return re.findall(r'name="rows-(\d+)-amount"', html)


# ── GET defaults: single row, account pre-selection ──────────────────────────

def test_get_renders_exactly_one_row_by_default(tmp_db):
    c = _client_as_user(1, "admin")
    resp = c.get("/cashbook/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert len(_numbered_amount_fields(html)) == 1


def test_get_preselects_users_default_account(tmp_db):
    # mig 126 seeds admin (user id 1) -> cashbook_accounts.code '392'.
    acct_id = _account_id_by_code(tmp_db, "392")
    c = _client_as_user(1, "admin")
    resp = c.get("/cashbook/new")
    html = resp.get_data(as_text=True)
    assert f'value="{acct_id}" selected>' in html


def test_get_blank_account_for_user_without_default(tmp_db):
    # user id 4 ('ss', manager) has default_cashbook_account_id = NULL in the
    # live DB (verified against the seed) — no account option should render
    # `selected`, leaving the blank "— เลือกบัญชี —" option in effect.
    conn = sqlite3.connect(tmp_db)
    default_acct = conn.execute(
        "SELECT default_cashbook_account_id FROM users WHERE id=4"
    ).fetchone()[0]
    conn.close()
    if default_acct is not None:
        pytest.skip("user id 4 unexpectedly has a default account in this DB clone")

    c = _client_as_user(4, "manager")
    resp = c.get("/cashbook/new")
    html = resp.get_data(as_text=True)
    assert not re.search(r'value="\d+" selected>', html)


# ── Bulk POST: per-row dates ──────────────────────────────────────────────────

def test_bulk_post_two_rows_different_dates_saved_at_own_dates(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-06-01",
        "account_id": str(account_id),
        "bulk_mode": "1",
        "rows-0-direction": "expense", "rows-0-category": "ทดสอบ Bulk A",
        "rows-0-amount": "111", "rows-0-txn_date": "2026-06-02",
        "rows-1-direction": "expense", "rows-1-category": "ทดสอบ Bulk B",
        "rows-1-amount": "222", "rows-1-txn_date": "2026-06-03",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT category, txn_date, amount FROM cashbook_transactions"
        " WHERE account_id=? AND category IN (?,?) ORDER BY category",
        (account_id, "ทดสอบ Bulk A", "ทดสอบ Bulk B"),
    ).fetchall()
    conn.close()
    by_cat = {r["category"]: (r["txn_date"], r["amount"]) for r in rows}
    assert by_cat["ทดสอบ Bulk A"] == ("2026-06-02", 111.0)
    assert by_cat["ทดสอบ Bulk B"] == ("2026-06-03", 222.0)


def test_bulk_row_with_blank_date_falls_back_to_top_date(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-06-04",
        "account_id": str(account_id),
        "bulk_mode": "1",
        "rows-0-direction": "expense", "rows-0-category": "ทดสอบ Bulk Blank",
        "rows-0-amount": "33", "rows-0-txn_date": "",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT txn_date FROM cashbook_transactions"
        " WHERE account_id=? AND category='ทดสอบ Bulk Blank'",
        (account_id,),
    ).fetchone()
    assert row[0] == "2026-06-04"


# ── Single-mode POST: top date only ───────────────────────────────────────────

def test_single_mode_post_uses_top_date(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-06-05",
        "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": "ทดสอบ Single",
        "rows-0-amount": "50",
    }
    resp = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT txn_date FROM cashbook_transactions"
        " WHERE account_id=? AND category='ทดสอบ Single'",
        (account_id,),
    ).fetchone()
    assert row[0] == "2026-06-05"


# ── Duplicate-row guard (decision D2) ─────────────────────────────────────────

def test_duplicate_guard_blocks_then_confirm_saves(tmp_db):
    account_id = _active_account(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount, created_by)"
        " VALUES (?, '2026-06-10', 'expense', 'ทดสอบ Dup', '', 999, 'seed')",
        (account_id,),
    )
    conn.commit()
    conn.close()

    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-06-10",
        "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": "ทดสอบ Dup",
        "rows-0-amount": "999",
    }
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200, "unconfirmed duplicate re-renders the form, no redirect"
    html = resp.get_data(as_text=True)
    assert "ซ้ำ" in html

    count = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE account_id=? AND category='ทดสอบ Dup'",
        (account_id,),
    ).fetchone()[0]
    assert count == 1, "duplicate must NOT be inserted without confirmation"

    form["confirm_duplicates"] = "1"
    resp2 = c.post("/cashbook/new", data=form, follow_redirects=False)
    assert resp2.status_code == 302, resp2.get_data(as_text=True)[:500]

    count2 = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE account_id=? AND category='ทดสอบ Dup'",
        (account_id,),
    ).fetchone()[0]
    assert count2 == 2, "confirmed duplicate must be inserted"


def test_duplicate_guard_flags_in_batch_duplicate_pair(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as_user(1, "admin")
    form = {
        "txn_date": "2026-06-11",
        "account_id": str(account_id),
        "bulk_mode": "1",
        "rows-0-direction": "expense", "rows-0-category": "ทดสอบ DupBatch",
        "rows-0-amount": "77",
        "rows-1-direction": "expense", "rows-1-category": "ทดสอบ DupBatch",
        "rows-1-amount": "77",
    }
    resp = c.post("/cashbook/new", data=form)
    assert resp.status_code == 200
    assert "ซ้ำ" in resp.get_data(as_text=True)

    count = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE account_id=? AND category='ทดสอบ DupBatch'",
        (account_id,),
    ).fetchone()[0]
    assert count == 0, "unconfirmed in-batch duplicate pair must not be inserted"
