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
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ROLLBACK_183 = os.path.join(
    REPO, 'data', 'migrations', '183_cashbook_account_names_and_flag.rollback.sql'
)

SEEDED_NAMES = {
    '392':      'ไทยพาณิชย์ 392',   # Put, 2026-09-15: 392 is SCB, not กสิกร
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


@pytest.fixture
def pre183_db(tmp_db):
    """tmp_db clones the LIVE dev DB, which may OR MAY NOT already have
    migration 183 applied — once it does (the standing repo rule: any dev/
    CI machine that has booted the app normally since this merged), a bare
    `database.init_db()` call sees 183 already in `applied_migrations` and
    SILENTLY SKIPS running it — every test below would then only be
    observing data seeded by the ALREADY-CORRECT migration that ran once,
    long ago, completely decoupled from whatever this file's mutations do
    to the SQL on disk right now. Reconstruct the TRUE pre-183 state first
    (rollback, only if the column is actually present) so `database.
    init_db()` is forced to run (a possibly-mutated) 183 fresh every time.
    Mirrors `pre134_conn` in test_mig134_product_generic_standins.py,
    adapted for `tmp_db` since this migration's subject (the real 7
    cashbook accounts) only exists on a live-DB clone, not a schema-only
    empty_db. Proven live: `$SP/r543/mut_post183.log` shows M17-M19 all
    GREEN (vacuous) against a pre-seeded post-183 DB copy before this
    fixture existed; `$SP/notes-534.md` records the same mutations RED
    with it in place."""
    conn = sqlite3.connect(tmp_db)
    try:
        cols = _cols(conn, 'cashbook_accounts')
        if 'income_recorded_elsewhere' in cols:
            with open(ROLLBACK_183, encoding='utf-8') as f:
                conn.executescript(f.read())
            conn.commit()
    finally:
        conn.close()
    return tmp_db


def test_income_recorded_elsewhere_column_added(pre183_db):
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        assert 'income_recorded_elsewhere' in _cols(conn, 'cashbook_accounts')
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert applied == 1
    finally:
        conn.close()


def test_migration_idempotent_via_runner(pre183_db):
    database.init_db()
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


@pytest.mark.parametrize('code,expected_name', list(SEEDED_NAMES.items()))
def test_seeds_exact_name_per_code(pre183_db, code, expected_name):
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        row = _row(conn, code)
        assert row is not None, f"account {code} not found"
        assert row[0] == expected_name
    finally:
        conn.close()


def test_flag_set_only_on_chadamas(pre183_db):
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        flagged = {
            r[0] for r in conn.execute(
                "SELECT code FROM cashbook_accounts WHERE income_recorded_elsewhere = 1"
            ).fetchall()
        }
        assert flagged == {'ชฎามาศ'}
    finally:
        conn.close()


def test_904_gets_neither_name_nor_flag(pre183_db):
    """Issue's explicit instruction: '904 is inactive; leave it.'"""
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        row = _row(conn, '904')
        assert row is not None
        assert row[0] is None or row[0] == ''
        assert row[1] == 0
    finally:
        conn.close()


def test_seed_does_not_overwrite_an_existing_custom_name(pre183_db):
    """`display_name` predates this migration (055/056) — if some account
    already carries a Put-set name before 183 runs, the seed must not clobber
    it. Simulated by writing a custom name BEFORE calling init_db(), since the
    column already exists pre-183 (only the seed UPDATE happens here, and it
    is explicitly guarded `WHERE display_name IS NULL OR display_name = ''`)."""
    conn = sqlite3.connect(pre183_db)
    conn.execute(
        "UPDATE cashbook_accounts SET display_name='กสิกรของพุธ' WHERE code='392'"
    )
    conn.commit()
    conn.close()

    database.init_db()

    conn = sqlite3.connect(pre183_db)
    try:
        row = _row(conn, '392')
        assert row[0] == 'กสิกรของพุธ'   # untouched, NOT overwritten to 'ไทยพาณิชย์ 392'
    finally:
        conn.close()


def test_other_accounts_unaffected_by_flag(pre183_db):
    database.init_db()
    conn = sqlite3.connect(pre183_db)
    try:
        for code in ('392', 'LEX', 'SPX', 'กิติยา', 'Put-Cash'):
            row = _row(conn, code)
            assert row[1] == 0, f"{code} must not be flagged"
    finally:
        conn.close()
