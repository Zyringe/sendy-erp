"""Regressions found by code review on issue #532's first implementation,
fixed in the same PR — kept as their own file so the review's reasoning
stays attached to its own tests rather than getting folded into the
feature tests it wasn't originally written against.

1. Tripping TWO confirm gates in one submission (new-category + duplicate,
   or new-category + advance-cap) could never be saved through the normal
   UI: each gate's screen only carried its OWN checkbox, so confirming gate
   A then hitting gate B's screen silently dropped A's confirmation, and
   confirming B then re-hit A. Fixed by carrying every confirm flag forward
   as a hidden field on every re-render.
2. ผู้ใช้ tag resolution ran BEFORE the in-engine commission double-book
   guard, so an employee whose name happened to match a salesperson's
   real-name alias (e.g. เจียรนัย = ต๋อ/06) would have their tag silently
   rewritten to their nickname before the guard ever saw the raw text,
   defeating it. Fixed by resolving tags AFTER policy blocks.
3. The "เปลี่ยนป้าย" resolution notice flashed unconditionally, before
   row_errors/form_errors were checked, so it could claim a tag was renamed
   on a page where nothing was saved. Fixed by flashing only after every
   hard block has already passed.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

COMMISSION_CATEGORY = "จ่ายค่าคอมมิชชั่น"


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
    conn.execute(
        "INSERT INTO employees(emp_code, full_name, nickname, is_active)"
        " VALUES (?, ?, ?, ?)",
        (emp_code, full_name, nickname, is_active),
    )
    conn.commit()
    conn.close()


# ── 1. Multi-gate ping-pong ───────────────────────────────────────────────────

def test_new_category_confirm_carries_forward_through_duplicate_gate(tmp_db):
    """A row that is BOTH a brand-new category AND a duplicate of an
    existing row must be reachable through the two gates in sequence
    without losing the first confirmation."""
    account_id = _active_account(tmp_db)
    cat_name = "หมวดปิงปอง 532"
    # Seed a row with this exact (date, direction, category, user_category,
    # amount) — a raw insert, so cashbook_categories never learns the name:
    # the SAME field values submitted next are simultaneously "new category"
    # AND "duplicate of this row".
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO cashbook_transactions"
        " (account_id, txn_date, direction, category, user_category, amount, created_by)"
        " VALUES (?, '2026-08-10', 'expense', ?, '', 555, 'seed')",
        (account_id, cat_name),
    )
    conn.commit()
    conn.close()

    c = _client_as("admin")
    base_form = {
        "txn_date": "2026-08-10", "account_id": str(account_id),
        "rows-0-direction": "expense", "rows-0-category": cat_name, "rows-0-amount": "555",
    }

    # Round 1: no confirms — new-category gate fires first.
    r1 = c.post("/cashbook/new", data=base_form)
    assert r1.status_code == 200
    html1 = r1.get_data(as_text=True)
    assert "หมวดหมู่ใหม่ที่ยังไม่มีในระบบ" in html1

    # Round 2: confirm the category — duplicate gate fires next. The
    # regression: this screen must STILL carry the new-category confirmation
    # forward (as a hidden field), or round 3 would bounce back to round 1.
    r2 = c.post("/cashbook/new", data={**base_form, "confirm_new_categories": "1"})
    assert r2.status_code == 200
    html2 = r2.get_data(as_text=True)
    assert "ซ้ำ" in html2, "must reach the duplicate gate, not save yet"
    assert 'name="confirm_new_categories" value="1"' in html2, \
        "the already-confirmed new-category gate must be carried forward as a hidden field"

    # Round 3: a real browser resubmits the hidden confirm_new_categories=1
    # PLUS the newly-ticked confirm_duplicates=1 — both together.
    r3 = c.post("/cashbook/new", data={
        **base_form, "confirm_new_categories": "1", "confirm_duplicates": "1",
    }, follow_redirects=False)
    assert r3.status_code == 302, "both gates confirmed together must finally save"

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE category=? AND amount=555",
        (cat_name,),
    ).fetchone()[0]
    n_cat = conn.execute(
        "SELECT is_active FROM cashbook_categories WHERE name=? AND direction='expense'",
        (cat_name,),
    ).fetchone()
    conn.close()
    assert n == 2, "the seed row plus the newly confirmed row"
    assert n_cat is not None and n_cat[0] == 1


# ── 2. Commission double-book guard must see the RAW typed tag ───────────────

def test_commission_block_not_defeated_by_employee_tag_resolution(tmp_db):
    """An employee whose name collides with a salesperson's in-engine
    real-name alias (เจียรนัย = ต๋อ/06, the exact case this codebase's own
    comments call a 'hard-confirmed double-book risk') must still be
    blocked — the policy check has to run on the tag AS TYPED, before it
    gets resolved to that employee's system nickname."""
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "เจียรนัย นามสกุลทดสอบ532", nickname="นัยทดสอบ532")

    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-11", "account_id": str(account_id),
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": COMMISSION_CATEGORY,
        "rows-0-user_category": "เจียรนัย", "rows-0-amount": "1234",
    })
    assert resp.status_code == 200
    assert "หน้าคอมมิชชั่น" in resp.get_data(as_text=True), \
        "the in-engine block must still fire even though 'เจียรนัย' also names an employee"

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-08-11' AND amount=1234"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "must not be saved as a manual entry — the double-book risk this guard exists for"


# ── 3. No misleading "changed tag" notice on a blocked submission ────────────

def test_no_resolution_notice_when_batch_is_blocked(tmp_db):
    """A resolvable tag on a VALID row must not flash 'เปลี่ยนป้าย' when a
    SIBLING row's genuine error blocks the whole batch — nothing was
    actually saved, so nothing was actually renamed."""
    account_id = _active_account(tmp_db)
    _seed_employee(tmp_db, "ทดสอบห้าสามสองปิง นามสกุลทดสอบ", nickname="ปิงทดสอบ532")

    c = _client_as("admin")
    resp = c.post("/cashbook/new", data={
        "txn_date": "2026-08-12", "account_id": str(account_id), "bulk_mode": "1",
        "confirm_new_categories": "1",
        "rows-0-direction": "expense", "rows-0-category": "ค่าน้ำมัน",
        "rows-0-user_category": "ทดสอบห้าสามสองปิง", "rows-0-amount": "10",
        # row 1: genuinely invalid — no category — blocks the WHOLE batch.
        "rows-1-direction": "expense", "rows-1-category": "",
        "rows-1-amount": "20",
    })
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "เปลี่ยนป้าย" not in html, \
        "must not claim a tag was renamed when the batch was blocked and nothing saved"

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-08-12'"
    ).fetchone()[0]
    conn.close()
    assert n == 0
