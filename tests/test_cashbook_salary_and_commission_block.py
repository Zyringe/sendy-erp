"""Phase 3 — /cashbook/new salary hard-block + in-engine commission hybrid
block + bulk skip+summary (plan.md decisions C1-C3/D1, scrutiny findings
#1/#3). Salary is ALWAYS blocked from manual entry (sourced in HR payroll,
ADR 0006); commission is a HYBRID — in-engine reps (D3: เจียรนัย=ต๋อ/06(-L),
ทวีเกียรติ=ท/03) are blocked (sourced on /commission, Phase 3 auto-post),
off-system reps (อัคเรศ, แต, บ่าว, ...) stay manual (the cashbook is still
their home — ADR 0008).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database
import commission as commission_mod

SALARY_CATEGORY = 'เงินเดือน'
COMMISSION_CATEGORY = 'จ่ายค่าคอมมิชชั่น'


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


def _active_account(db):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id FROM cashbook_accounts WHERE is_active=1 AND is_transfer=0 ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("no active non-transfer cashbook account")
    return row[0]


# ── Salary hard-block (single mode) ─────────────────────────────────────────

def test_single_manual_salary_row_blocked_nothing_saved(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-20', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': SALARY_CATEGORY,
        'rows-0-amount': '5000',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200, "blocked single row re-renders, no save"
    assert 'หน้าเงินเดือน' in resp.get_data(as_text=True)

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-20' AND amount=5000"
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_single_normal_row_still_saves(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-20', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': 'ทดสอบ Normal Save',
        'rows-0-amount': '111',
    }
    resp = c.post('/cashbook/new', data=form, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE category='ทดสอบ Normal Save' AND amount=111"
    ).fetchone()[0]
    conn.close()
    assert n == 1


# ── Bulk skip+summary (D1) ───────────────────────────────────────────────────

def test_bulk_skips_salary_row_saves_the_rest_with_summary(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-21', 'account_id': str(account_id), 'bulk_mode': '1',
        'rows-0-direction': 'expense', 'rows-0-category': SALARY_CATEGORY,
        'rows-0-amount': '4000',
        'rows-1-direction': 'expense', 'rows-1-category': 'ทดสอบ BulkSkip A',
        'rows-1-amount': '55',
        'rows-2-direction': 'expense', 'rows-2-category': 'ทดสอบ BulkSkip B',
        'rows-2-amount': '66',
    }
    resp = c.post('/cashbook/new', data=form, follow_redirects=False)
    assert resp.status_code == 302, "some rows still valid -> redirect (partial success)"

    conn = sqlite3.connect(migrated_db)
    n_salary = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-21' AND amount=4000"
    ).fetchone()[0]
    n_a = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE category='ทดสอบ BulkSkip A' AND amount=55"
    ).fetchone()[0]
    n_b = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions"
        " WHERE category='ทดสอบ BulkSkip B' AND amount=66"
    ).fetchone()[0]
    conn.close()
    assert n_salary == 0, "salary row must be skipped, not saved"
    assert n_a == 1 and n_b == 1, "the other 2 valid rows must still save"


def test_bulk_all_rows_blocked_rerenders_saves_nothing(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-27', 'account_id': str(account_id), 'bulk_mode': '1',
        'rows-0-direction': 'expense', 'rows-0-category': SALARY_CATEGORY,
        'rows-0-amount': '4100',
        'rows-1-direction': 'expense', 'rows-1-category': SALARY_CATEGORY,
        'rows-1-amount': '4200',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200, "nothing left to save -> re-render, not a redirect"

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-27'"
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_bulk_genuine_validation_error_still_rejects_whole_batch(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-22', 'account_id': str(account_id), 'bulk_mode': '1',
        # row 0: genuinely invalid (no category)
        'rows-0-direction': 'expense', 'rows-0-category': '',
        'rows-0-amount': '77',
        # row 1: otherwise-valid
        'rows-1-direction': 'expense', 'rows-1-category': 'ทดสอบ RejectAll',
        'rows-1-amount': '88',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200, "a genuine validation error rejects the WHOLE batch"

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE category='ทดสอบ RejectAll' AND amount=88"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "the otherwise-valid row must NOT be saved when a sibling row genuinely errors"


# ── In-engine vs off-system commission (D3) ─────────────────────────────────

def test_manual_commission_to_in_engine_alias_blocked(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-23', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': COMMISSION_CATEGORY,
        'rows-0-user_category': 'เจียรนัย', 'rows-0-amount': '1200',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200
    assert 'หน้าคอมมิชชั่น' in resp.get_data(as_text=True)

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-23' AND amount=1200"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "เจียรนัย is ต๋อ/06 in-engine — manual entry must be blocked"


def test_manual_commission_to_in_engine_code_blocked(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-24', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': COMMISSION_CATEGORY,
        'rows-0-user_category': '06', 'rows-0-amount': '1300',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-24' AND amount=1300"
    ).fetchone()[0]
    conn.close()
    assert n == 0, "the bare code '06' is also an in-engine identifier — must block"


def test_manual_commission_to_off_system_rep_allowed(migrated_db):
    account_id = _active_account(migrated_db)
    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-25', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': COMMISSION_CATEGORY,
        'rows-0-user_category': 'อัคเรศ', 'rows-0-amount': '1400',
    }
    resp = c.post('/cashbook/new', data=form, follow_redirects=False)
    assert resp.status_code == 302, "อัคเรศ is off-system — manual entry stays allowed"

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE txn_date='2026-07-25' AND amount=1400"
    ).fetchone()[0]
    conn.close()
    assert n == 1


# ── No-double-book: engine auto-post exists + manual attempt for same rep ──

def test_manual_commission_blocked_even_after_engine_auto_post_exists(migrated_db):
    account_id = _active_account(migrated_db)
    # Seed a real /commission auto-post for ทวีเกียรติ (ท/03) first.
    commission_mod.record_payout(
        year_month='2026-07', salesperson_code='03', amount_paid=999,
        paid_date='2026-07-26', paid_by='test-admin', account_id=account_id,
        db_path=migrated_db,
    )

    c = _client_as_user(1, 'admin')
    form = {
        'txn_date': '2026-07-26', 'account_id': str(account_id),
        'rows-0-direction': 'expense', 'rows-0-category': COMMISSION_CATEGORY,
        'rows-0-user_category': 'ทวีเกียรติ', 'rows-0-amount': '888',
    }
    resp = c.post('/cashbook/new', data=form)
    assert resp.status_code == 200

    conn = sqlite3.connect(migrated_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM cashbook_transactions WHERE amount=888 AND category=?",
        (COMMISSION_CATEGORY,),
    ).fetchone()[0]
    conn.close()
    assert n == 0, "manual entry for an in-engine rep stays blocked regardless of existing payouts"
