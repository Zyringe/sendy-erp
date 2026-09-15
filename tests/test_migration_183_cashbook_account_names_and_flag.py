"""Migration 183 — issue #534 (cashbook account names + รายรับบันทึกที่อื่น flag).

Adds `cashbook_accounts.income_recorded_elsewhere` and seeds the six readable
`display_name`s + the ชฎามาศ flag from the issue's table. `display_name`
itself predates this migration (055/056) — nothing has ever written to it
(verified 2026-09-15: blank on both prod and the local dev DB) — so this
migration is purely additive: one new column + a guarded, idempotent-by-design
data seed. `904` gets neither (issue: "leave it").
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '183_cashbook_account_names_and_flag.sql'

SEEDED_NAMES = {
    '392':      'กสิกร 392',
    'LEX':      'กสิกร รับเงิน Lazada',
    'SPX':      'กสิกร รับเงิน Shopee',
    'ชฎามาศ':    'บัญชีส่วนตัว ชฎามาศ',
    'กิติยา':    'เงินสำรอง เซียง',
    'Put-Cash': 'เงินสดลิ้นชัก',
}


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _row(conn, code):
    return conn.execute(
        "SELECT display_name, income_recorded_elsewhere, is_active"
        " FROM cashbook_accounts WHERE code=?", (code,)
    ).fetchone()


def test_income_recorded_elsewhere_column_added(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        assert 'income_recorded_elsewhere' in _cols(conn, 'cashbook_accounts')
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert applied == 1
    finally:
        conn.close()


def test_migration_idempotent_via_runner(tmp_db):
    database.init_db()
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


@pytest.mark.parametrize('code,expected_name', list(SEEDED_NAMES.items()))
def test_seeds_exact_name_per_code(tmp_db, code, expected_name):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        row = _row(conn, code)
        assert row is not None, f"account {code} not found"
        assert row[0] == expected_name
    finally:
        conn.close()


def test_flag_set_only_on_chadamas(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        flagged = {
            r[0] for r in conn.execute(
                "SELECT code FROM cashbook_accounts WHERE income_recorded_elsewhere = 1"
            ).fetchall()
        }
        assert flagged == {'ชฎามาศ'}
    finally:
        conn.close()


def test_904_gets_neither_name_nor_flag(tmp_db):
    """Issue's explicit instruction: '904 is inactive; leave it.'"""
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        row = _row(conn, '904')
        assert row is not None
        assert row[0] is None or row[0] == ''
        assert row[1] == 0
    finally:
        conn.close()


def test_seed_does_not_overwrite_an_existing_custom_name(tmp_db):
    """`display_name` predates this migration (055/056) — if some account
    already carries a Put-set name before 183 runs, the seed must not clobber
    it. Simulated by writing a custom name BEFORE calling init_db(), since the
    column already exists pre-183 (only the seed UPDATE happens here, and it
    is explicitly guarded `WHERE display_name IS NULL OR display_name = ''`)."""
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "UPDATE cashbook_accounts SET display_name='กสิกรของพุธ' WHERE code='392'"
    )
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(tmp_db)
    try:
        row = _row(conn, '392')
        assert row[0] == 'กสิกรของพุธ'   # untouched, NOT overwritten to 'กสิกร 392'
    finally:
        conn.close()


def test_other_accounts_unaffected_by_flag(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        for code in ('392', 'LEX', 'SPX', 'กิติยา', 'Put-Cash'):
            row = _row(conn, code)
            assert row[1] == 0, f"{code} must not be flagged"
    finally:
        conn.close()
