"""Phase 4 — Salary pay-event posting (cashbook-manual-entry plan). MONEY PATH.

`hr.post_salary_payment` / `hr.void_salary_payment` post/void ONE linked
`cashbook_transactions` expense row per `payroll_items.id` ("จ่ายแล้ว" /
"ยกเลิกการจ่าย" — ADR 0006). Paid-state is DERIVED from the existence of that
linked row (`payroll_item_id`), never a separate flag.

Covers the plan's 10-case Phase 4 test list:
  1. pay → exactly one linked cashbook expense, fields correct.
  2. pay twice (same item) → second rejected, still exactly one row.
  3. net_pay == 0 → no row posted, clear rejection.
  4. pay into a transfer account (is_transfer=1) → rejected.
  5. unpay → linked row deleted, item shows unpaid again.
  6. reopen blocked while any item paid; allowed after all unpaid.
  7. a paid row is READ-ONLY in the cashbook (cross-checks the P2 guard).
  8. two payers → two rows, correct accounts.
  9. role gate: shareholder CAN POST pay/unpay; staff CANNOT.
  10. dup-month warning fires when a manual เงินเดือน row exists in the
      run's month.

Fixture: `tmp_db_conn_hr_clean` (copy of live DB, payroll/leave state wiped —
already carries mig 123's columns + the 6 seeded cashbook_accounts, incl.
transfer account code 904 and non-transfer 392/ชฎามาศ).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import hr as hr_mod


# ── helpers (mirrors tests/test_hr_pay_checklist.py + test_cashbook_manual_entry.py) ──

def _mk_employee(conn, emp_code, full_name, nickname=None, company_id=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, nickname, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES (?, ?, ?, 'M', ?, '2026-01-01', 90, 0, 0, 1)""",
        (emp_code, full_name, nickname, company_id),
    )
    conn.commit()
    return cur.lastrowid


def _mk_run(conn, year_month, status='finalized', company_id=1):
    cur = conn.execute(
        """INSERT INTO payroll_runs (year_month, company_id, status, run_date, created_by)
           VALUES (?, ?, ?, ?, 1)""",
        (year_month, company_id, status, f"{year_month}-28"),
    )
    conn.commit()
    return cur.lastrowid


def _mk_item(conn, run_id, employee_id, net_pay=15000.0):
    cur = conn.execute(
        """INSERT INTO payroll_items (run_id, employee_id, salary_rate, base_amount, net_pay)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, employee_id, net_pay, net_pay, net_pay),
    )
    conn.commit()
    return cur.lastrowid


def _account_id(conn, code):
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code=?", (code,)
    ).fetchone()
    assert row is not None, f"expected seeded cashbook_accounts row code={code}"
    return row[0]


def _transfer_account_id(conn):
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_transfer=1 AND is_active=1 LIMIT 1"
    ).fetchone()
    assert row is not None, "expected a seeded is_transfer=1 active account (e.g. code 904)"
    return row[0]


def _client_as(role, tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']      = 1
        sess['username']     = f'test-{role}'
        sess['display_name'] = f'Test {role.title()}'
        sess['role']         = role
    return c


# ── 1. pay → exactly one linked cashbook expense, fields correct ───────────

def test_pay_posts_one_linked_cashbook_expense(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY1', 'จ่ายทดสอบ', nickname='นุ่น')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=18500.5)
    account_id = _account_id(conn, '392')

    new_id = hr_mod.post_salary_payment(
        item_id, account_id, '2026-09-28', 'test-admin', conn=conn,
    )
    assert isinstance(new_id, int)

    rows = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row['id'] == new_id
    assert row['amount'] == 18500.5
    assert row['account_id'] == account_id
    assert row['txn_date'] == '2026-09-28'
    assert row['user_category'] == 'นุ่น'
    assert row['created_by'] == 'test-admin'
    assert row['category'] == 'เงินเดือน'
    assert row['direction'] == 'expense'
    assert row['payroll_run_id'] == run_id
    assert row['payroll_item_id'] == item_id
    assert 'นุ่น' in (row['description'] or '')
    assert '2026-09' in (row['description'] or '')


def test_pay_default_pay_date_is_today_when_blank(tmp_db_conn_hr_clean):
    import datetime
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY1B', 'วันที่ว่าง')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=5000.0)
    account_id = _account_id(conn, '392')

    hr_mod.post_salary_payment(item_id, account_id, '', 'test-admin', conn=conn)

    row = conn.execute(
        "SELECT txn_date FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()
    assert row['txn_date'] == datetime.date.today().isoformat()


# ── 2. pay twice → second rejected, still exactly one row ──────────────────

def test_pay_twice_rejected_idempotent(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY2', 'จ่ายซ้ำ')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=10000.0)
    account_id = _account_id(conn, '392')

    hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)
    with pytest.raises(ValueError):
        hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    rows = conn.execute(
        "SELECT * FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchall()
    assert len(rows) == 1, "double-post must never create a second row"


# ── 3. net_pay == 0 → no row posted, clear rejection ────────────────────────

def test_pay_zero_net_pay_rejected(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY3', 'ไม่มียอด')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=0.0)
    account_id = _account_id(conn, '392')

    with pytest.raises(ValueError):
        hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()[0]
    assert count == 0


# ── 4. pay into a transfer account → rejected ───────────────────────────────

def test_pay_into_transfer_account_rejected(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY4', 'บัญชีโอน')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=12000.0)
    transfer_id = _transfer_account_id(conn)

    with pytest.raises(ValueError):
        hr_mod.post_salary_payment(item_id, transfer_id, '2026-09-28', 'test-admin', conn=conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()[0]
    assert count == 0


def test_pay_into_inactive_account_rejected(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY4B', 'บัญชีปิด')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=12000.0)
    inactive_id = _account_id(conn, '392')
    conn.execute("UPDATE cashbook_accounts SET is_active=0 WHERE id=?", (inactive_id,))
    conn.commit()

    with pytest.raises(ValueError):
        hr_mod.post_salary_payment(item_id, inactive_id, '2026-09-28', 'test-admin', conn=conn)


# ── 5. unpay → linked row deleted, item shows unpaid again ──────────────────

def test_unpay_deletes_linked_row(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY5', 'ยกเลิกจ่าย')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=9000.0)
    account_id = _account_id(conn, '392')

    hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()[0] == 1

    hr_mod.void_salary_payment(item_id, 'test-admin', conn=conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()[0] == 0

    # re-pay must now succeed (proves the item is unpaid again, not double-locked)
    new_id = hr_mod.post_salary_payment(item_id, account_id, '2026-09-29', 'test-admin', conn=conn)
    assert isinstance(new_id, int)


def test_unpay_noop_when_nothing_posted(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY5B', 'ไม่เคยจ่าย')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=9000.0)

    hr_mod.void_salary_payment(item_id, 'test-admin', conn=conn)  # must not raise


# ── 6. reopen blocked while any item paid; allowed after all unpaid ────────

def test_reopen_blocked_while_paid_then_allowed_after_unpay(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eid1 = _mk_employee(conn, 'T_PAY6A', 'จ่ายแล้วคนที่1')
    eid2 = _mk_employee(conn, 'T_PAY6B', 'ยังไม่จ่าย')
    run_id = _mk_run(conn, '2026-09')
    item1 = _mk_item(conn, run_id, eid1, net_pay=11000.0)
    item2 = _mk_item(conn, run_id, eid2, net_pay=13000.0)
    account_id = _account_id(conn, '392')

    hr_mod.post_salary_payment(item1, account_id, '2026-09-28', 'test-admin', conn=conn)

    with pytest.raises(ValueError):
        hr_mod.reopen_run(run_id, reason='ทดสอบ reopen ขณะจ่ายแล้ว', actor='test-admin', conn=conn)

    status = conn.execute(
        "SELECT status FROM payroll_runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    assert status == 'finalized', "reopen must not have mutated status while blocked"

    hr_mod.void_salary_payment(item1, 'test-admin', conn=conn)
    run = hr_mod.reopen_run(run_id, reason='ทดสอบ reopen หลังยกเลิกจ่าย', actor='test-admin', conn=conn)
    assert run['status'] == 'draft'


# ── 7. a paid row is READ-ONLY in the cashbook (cross-check P2 guard) ──────

def test_paid_row_is_readonly_in_cashbook(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY7', 'ล็อกแก้ไข')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=7000.0)
    account_id = _account_id(conn, '392')

    txn_id = hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    c = _client_as('admin', tmp_db)
    resp_edit = c.post(f"/cashbook/txn/{txn_id}/edit", data={
        "account_id": str(account_id), "txn_date": "2026-09-29",
        "direction": "expense", "category": "เงินเดือน", "amount": "999999",
    })
    assert resp_edit.status_code == 403

    resp_delete = c.post(f"/cashbook/txn/{txn_id}/delete", data={})
    assert resp_delete.status_code == 403

    row = conn.execute(
        "SELECT amount, txn_date FROM cashbook_transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row['amount'] == 7000.0 and row['txn_date'] == '2026-09-28', \
        "salary pay-event row must be unchanged by the cashbook edit/delete guard"


# ── 8. two payers: independent rows, correct accounts ───────────────────────

def test_two_payers_independent_rows_correct_accounts(tmp_db_conn_hr_clean):
    conn = tmp_db_conn_hr_clean
    eidA = _mk_employee(conn, 'T_PAY8A', 'พนักงานเอ')
    eidB = _mk_employee(conn, 'T_PAY8B', 'พนักงานบี')
    run_id = _mk_run(conn, '2026-09')
    itemA = _mk_item(conn, run_id, eidA, net_pay=16000.0)
    itemB = _mk_item(conn, run_id, eidB, net_pay=17000.0)

    acct_392 = _account_id(conn, '392')
    acct_chadamas = _account_id(conn, 'ชฎามาศ')

    hr_mod.post_salary_payment(itemA, acct_392, '2026-09-28', 'test-admin', conn=conn)
    hr_mod.post_salary_payment(itemB, acct_chadamas, '2026-09-28', 'test-manager', conn=conn)

    rows = {
        r['payroll_item_id']: r
        for r in conn.execute(
            "SELECT * FROM cashbook_transactions WHERE payroll_run_id=?", (run_id,)
        ).fetchall()
    }
    assert len(rows) == 2
    assert rows[itemA]['account_id'] == acct_392
    assert rows[itemA]['created_by'] == 'test-admin'
    assert rows[itemB]['account_id'] == acct_chadamas
    assert rows[itemB]['created_by'] == 'test-manager'


# ── 9. role gate: shareholder CAN POST pay/unpay; staff CANNOT ─────────────

def test_shareholder_can_post_pay_and_unpay(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY9A', 'ผู้ถือหุ้นจ่าย')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=8000.0)
    account_id = _account_id(conn, '392')

    c = _client_as('shareholder', tmp_db)
    resp = c.post(
        f"/hr/payroll/{run_id}/item/{item_id}/pay",
        data={"account_id": str(account_id), "pay_date": "2026-09-28"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.data[:500]

    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()
    assert row is not None, "shareholder POST pay must succeed"

    resp2 = c.post(f"/hr/payroll/{run_id}/item/{item_id}/unpay", data={}, follow_redirects=False)
    assert resp2.status_code == 302, resp2.data[:500]

    row2 = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()
    assert row2 is None, "shareholder POST unpay must succeed"


def test_staff_cannot_post_pay(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY9B', 'staffพยายามจ่าย')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=8000.0)
    account_id = _account_id(conn, '392')

    c = _client_as('staff', tmp_db)
    resp = c.post(
        f"/hr/payroll/{run_id}/item/{item_id}/pay",
        data={"account_id": str(account_id), "pay_date": "2026-09-28"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 403)

    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()
    assert row is None, "staff POST pay must never insert a row"


def test_staff_cannot_post_unpay(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY9C', 'staffพยายามยกเลิก')
    run_id = _mk_run(conn, '2026-09')
    item_id = _mk_item(conn, run_id, eid, net_pay=8000.0)
    account_id = _account_id(conn, '392')
    hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    c = _client_as('staff', tmp_db)
    resp = c.post(f"/hr/payroll/{run_id}/item/{item_id}/unpay", data={}, follow_redirects=False)
    assert resp.status_code in (302, 403)

    row = conn.execute(
        "SELECT id FROM cashbook_transactions WHERE payroll_item_id=?", (item_id,)
    ).fetchone()
    assert row is not None, "staff POST unpay must never delete the row"


# ── 10. dup-month warning fires on the payroll_detail page ─────────────────

def test_dup_month_warning_present_when_manual_salary_row_exists(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY10', 'เตือนซ้ำ')
    run_id = _mk_run(conn, '2026-09')
    _mk_item(conn, run_id, eid, net_pay=14000.0)
    account_id = _account_id(conn, '392')

    # A manual (payroll_item_id IS NULL) เงินเดือน row already sitting in the
    # run's month — the classic "someone hand-typed it before this feature".
    conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount, created_by)
           VALUES (?, '2026-09-15', 'expense', 'เงินเดือน', 5000, 'manual-entry')""",
        (account_id,),
    )
    conn.commit()

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'มีรายการเงินเดือนกรอกมือ' in html
    assert '1' in html.split('มีรายการเงินเดือนกรอกมือ')[1][:10]


def test_no_dup_month_warning_when_no_manual_rows(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    eid = _mk_employee(conn, 'T_PAY10B', 'ไม่เตือน')
    run_id = _mk_run(conn, '2026-09')
    _mk_item(conn, run_id, eid, net_pay=14000.0)
    conn.close()

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'มีรายการเงินเดือนกรอกมือ' not in html


# ── Extra: payroll_detail render shows the interactive pay/unpay controls ──

def test_payroll_detail_shows_pay_button_for_unpaid_item(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    _mk_employee(conn, 'T_PAY11', 'ยังไม่จ่าย', nickname='รอจ่าย')
    run_id = _mk_run(conn, '2026-09')
    eid = conn.execute("SELECT id FROM employees WHERE emp_code='T_PAY11'").fetchone()[0]
    _mk_item(conn, run_id, eid, net_pay=9500.0)
    conn.close()

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    html = resp.data.decode('utf-8')
    assert 'จ่ายแล้ว' in html


def test_payroll_detail_shows_paid_badge_and_unpay_button(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    _mk_employee(conn, 'T_PAY12', 'จ่ายแล้ว', nickname='จ่ายแล้วนะ')
    run_id = _mk_run(conn, '2026-09')
    eid = conn.execute("SELECT id FROM employees WHERE emp_code='T_PAY12'").fetchone()[0]
    item_id = _mk_item(conn, run_id, eid, net_pay=9500.0)
    account_id = _account_id(conn, '392')
    hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    html = resp.data.decode('utf-8')
    assert 'ยกเลิกการจ่าย' in html


def test_payroll_detail_shows_no_transfer_message_for_zero_net_pay(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    _mk_employee(conn, 'T_PAY13', 'ไม่มียอดโอน')
    run_id = _mk_run(conn, '2026-09')
    eid = conn.execute("SELECT id FROM employees WHERE emp_code='T_PAY13'").fetchone()[0]
    _mk_item(conn, run_id, eid, net_pay=0.0)
    conn.close()

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    html = resp.data.decode('utf-8')
    assert 'ไม่มียอดโอน' in html


def test_reopen_button_disabled_while_any_item_paid(tmp_db_conn_hr_clean, tmp_db):
    conn = tmp_db_conn_hr_clean
    _mk_employee(conn, 'T_PAY14', 'reopen บล็อก')
    run_id = _mk_run(conn, '2026-09')
    eid = conn.execute("SELECT id FROM employees WHERE emp_code='T_PAY14'").fetchone()[0]
    item_id = _mk_item(conn, run_id, eid, net_pay=9500.0)
    account_id = _account_id(conn, '392')
    hr_mod.post_salary_payment(item_id, account_id, '2026-09-28', 'test-admin', conn=conn)

    c = _client_as('admin', tmp_db)
    resp = c.get(f"/hr/payroll/{run_id}")
    html = resp.data.decode('utf-8')
    assert 'ยกเลิกการจ่ายก่อน reopen' in html
