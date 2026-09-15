"""Issue #532 §2 — ผู้ใช้ tag mapping to the employee's SYSTEM name.

Decided with Put 2026-09-15: a person's tag is the name the system itself
posts (the employee's HR nickname, or full_name when blank). Manual rows
used to tag people by their real name, drifting from what payroll/advances/
commission post.
  - On save (new entry, and an edit that changed the tag): a typed real
    first-name or full-name that unambiguously names one employee saves as
    that employee's system name, with a visible notice.
  - An ambiguous match (two employees sharing the same first name) saves as
    typed.
  - Editing a row without touching its tag never changes it.
  - The tag suggestions list shows employees' system names first.
  - Salesperson real-name aliases (ADR 0008) are a separate, out-of-scope
    concept — never matched here.
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


def _seed_employee(tmp_db, full_name, nickname=None, emp_code=None, is_active=1):
    conn = sqlite3.connect(tmp_db)
    emp_code = emp_code or f"TST{abs(hash(full_name)) % 100000}"
    cur = conn.execute(
        "INSERT INTO employees(emp_code, full_name, nickname, is_active)"
        " VALUES (?, ?, ?, ?)",
        (emp_code, full_name, nickname, is_active),
    )
    conn.commit()
    emp_id = cur.lastrowid
    conn.close()
    return emp_id


def _plain_row(tmp_db, account_id, user_category=None, category="อื่นๆ", amount=100.0):
    conn = sqlite3.connect(tmp_db)
    txn_id = conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount)"
        " VALUES (?, '2026-08-05', 'expense', ?, ?, ?)",
        (account_id, category, user_category, amount),
    ).lastrowid
    conn.commit()
    conn.close()
    return txn_id


# ── New entry: unambiguous match maps to system name ─────────────────────────

def test_new_entry_first_name_maps_to_nickname_with_notice(tmp_db):
    # First word must not collide with any REAL employee's first name in the
    # live DB clone (tmp_db carries live data) — a collision makes the match
    # ambiguous (2+ employees) and this test would exercise the WRONG branch.
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "วีณาทดสอบห้าสามสอง ขมสันเทียะ", nickname="หลุยทดสอบ532")
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-05", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": "วีณาทดสอบห้าสามสอง", "rows-0-amount": "50",
    }, follow_redirects=True)
    assert "เปลี่ยนป้าย" in resp.get_data(as_text=True), "the resolution must flash a visible notice"

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE amount=50 AND category='ค่าน้ำมัน'"
    ).fetchone()
    assert row[0] == "หลุยทดสอบ532", "typed first name must be resolved to the system nickname"


def test_new_entry_full_name_maps_to_system_name(tmp_db):
    account_id = _active_account(tmp_db)
    full = "สมชาย ใจดีทดสอบ532"
    _seed_employee(tmp_db, full, nickname="ชายทดสอบ532")
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-05", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": full, "rows-0-amount": "51",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE amount=51 AND category='ค่าน้ำมัน'"
    ).fetchone()
    assert row[0] == "ชายทดสอบ532"


def test_new_entry_no_nickname_maps_to_full_name(tmp_db):
    """Put's rule: nickname, OR full_name when blank."""
    account_id = _active_account(tmp_db)
    full = "ประเสริฐ มั่นคงทดสอบ532"
    _seed_employee(tmp_db, full, nickname=None)
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-05", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": "ประเสริฐ", "rows-0-amount": "52",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE amount=52 AND category='ค่าน้ำมัน'"
    ).fetchone()
    assert row[0] == full


# ── Ambiguous match saves as typed ────────────────────────────────────────────

def test_new_entry_ambiguous_first_name_saves_as_typed(tmp_db):
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "อารีย์ หนึ่งทดสอบ532", nickname="อ้น1_532", emp_code="AMB1_532")
    _seed_employee(tmp_db, "อารีย์ สองทดสอบ532", nickname="อ้น2_532", emp_code="AMB2_532")
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-05", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": "อารีย์", "rows-0-amount": "53",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE amount=53 AND category='ค่าน้ำมัน'"
    ).fetchone()
    assert row[0] == "อารีย์", "an ambiguous first name must save as TYPED, not silently pick one"


# ── Non-employee / place tags pass through untouched ──────────────────────────

def test_new_entry_non_employee_tag_saved_as_typed(tmp_db):
    account_id = _active_account(tmp_db)
    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-05", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": "โกดัง Lion", "rows-0-amount": "54",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE amount=54 AND category='ค่าน้ำมัน'"
    ).fetchone()
    assert row[0] == "โกดัง Lion"


# ── Edit: only when the tag field was actually changed ───────────────────────

def test_edit_without_touching_tag_never_changes_it(tmp_db):
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "วิภา ขมสันเทียะทดสอบ532b", nickname="หลุยทดสอบ532b")
    # Stored tag is already the real first name — an edit that resubmits the
    # SAME text (because the UI never touched that field) must NOT resolve it.
    txn_id = _plain_row(tmp_db, account_id, user_category="วิภา", amount=60.0)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-05", "direction": "expense",
        "category": "อื่นๆ", "user_category": "วิภา", "amount": "999",
    }, follow_redirects=False)
    assert resp.status_code == 302

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category, amount FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "วิภา", "tag must be untouched when the field itself wasn't changed"
    assert row[1] == 999.0


def test_edit_changing_tag_to_real_name_resolves_to_system_name(tmp_db):
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "วีณาทดสอบห้าสามสองc ขมสันเทียะ", nickname="หลุยทดสอบ532c")
    txn_id = _plain_row(tmp_db, account_id, user_category="เดิม", amount=61.0)
    c = _client_as("admin")
    resp = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-08-05", "direction": "expense",
        "category": "อื่นๆ", "user_category": "วีณาทดสอบห้าสามสองc", "amount": "61",
    }, follow_redirects=True)
    assert "เปลี่ยนป้าย" in resp.get_data(as_text=True)

    row = sqlite3.connect(tmp_db).execute(
        "SELECT user_category FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] == "หลุยทดสอบ532c", "changing the tag to a real name must resolve it"


# ── Suggestions list: employees first ─────────────────────────────────────────
#
# The combo component's suggestions are wired into the page as a `tojson`
# JS literal, which Flask escapes Thai text to \uXXXX sequences (see
# test_cashbook_advance_writeback.py's comment on the same trap) — so the
# ORDER contract is verified at the function level, an independent signal
# from the HTTP/JSON encoding round-trip.

def test_known_tags_lists_employee_system_names_first(tmp_db):
    from blueprints.cashbook import _get_known_user_tags

    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "ทดสอบ ชื่อจริง532", nickname="นิคทดสอบ532")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    # A non-employee tag already in use.
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount)"
        " VALUES (?, '2026-08-05', 'expense', 'อื่นๆ', 'โกดังทดสอบ532', 10)",
        (account_id,),
    )
    conn.commit()

    tags = _get_known_user_tags(conn)
    conn.close()
    assert "นิคทดสอบ532" in tags and "โกดังทดสอบ532" in tags
    assert tags.index("นิคทดสอบ532") < tags.index("โกดังทดสอบ532"), \
        "employee system names must be listed before other tags"


def test_new_entry_get_renders_known_tags_without_error(tmp_db):
    """Route-level smoke check: the GET still renders 200 with the
    (tojson-escaped) suggestions wired in — the escaping itself is not a bug,
    see the function-level test above for the actual order contract."""
    _seed_employee(tmp_db, "ทดสอบ ชื่อจริง532b", nickname="นิคทดสอบ532b")
    c = _client_as("admin")
    resp = c.get("/cashbook/new")
    assert resp.status_code == 200
    assert "user_tags" in resp.get_data(as_text=True)
