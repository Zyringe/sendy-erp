"""Migration 182 — issue #533 (cashbook payout mirror).

Adds the natural-key columns that let a cashbook_transactions row be
"payout-sourced": payout_platform / payout_deposit_date / payout_amount /
payout_occurrence, plus a partial UNIQUE index over the four so the same
payout can never be mirrored twice (payout_platform IS NULL rows — the
overwhelming majority — are untouched by the index; SQLite treats every NULL
as distinct).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '182_cashbook_payout_mirror.sql'


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_cashbook_transactions_gains_payout_columns(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        cols = _cols(conn, 'cashbook_transactions')
        for c in ('payout_platform', 'payout_deposit_date', 'payout_amount', 'payout_occurrence'):
            assert c in cols
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


def test_ordinary_rows_keep_null_and_are_unconstrained(tmp_db):
    """The overwhelming majority of rows never touch this natural key —
    many NULLs must coexist under the partial UNIQUE index."""
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        acct = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
        for i in range(2):
            conn.execute(
                "INSERT INTO cashbook_transactions"
                " (account_id, txn_date, direction, category, amount)"
                " VALUES (?, '2026-07-01', 'expense', 'ทดสอบ', ?)",
                (acct, 100 + i),
            )
        conn.commit()
    finally:
        conn.close()


def test_payout_natural_key_unique_when_set(tmp_db):
    """Two rows may not mirror the SAME payout (same platform+date+amount+
    occurrence); distinct occurrence numbers coexist fine (the real
    duplicate-payout case, see 108_marketplace_payouts_drop_unique.sql)."""
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        acct = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE code='SPX'"
        ).fetchone()[0]

        def _mk(occurrence):
            conn.execute(
                "INSERT INTO cashbook_transactions"
                " (account_id, txn_date, direction, category, amount,"
                "  payout_platform, payout_deposit_date, payout_amount, payout_occurrence)"
                " VALUES (?, '2026-03-10', 'income', 'ยอดขายของ', 3596,"
                "  'shopee', '2026-03-10', 3596, ?)",
                (acct, occurrence),
            )

        _mk(1)
        with pytest.raises(sqlite3.IntegrityError):
            _mk(1)
        conn.rollback()

        _mk(1)
        _mk(2)          # same platform+date+amount, different occurrence -> OK
        conn.commit()
    finally:
        conn.close()
