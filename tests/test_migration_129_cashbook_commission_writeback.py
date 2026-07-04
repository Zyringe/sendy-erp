"""Migration 129 — Phase 3 of the cashbook /new overhaul
(projects/cashbook-entry-reconcile/plan.md, decisions C1-C4/C7, D3).

Adds the schema the commission auto-post flow needs:
  - cashbook_transactions.commission_payout_id  (FK -> commission_payouts, the
    "linked & locked row" link, symmetric with payroll_item_id / salary_advance_id)
  - a UNIQUE index on it (one cashbook row <-> one payout; nullable, so the
    thousands of non-commission rows keep their NULLs)
  - salespersons.real_name (D3 alias gate) seeded: 06 & 06-L -> เจียรนัย,
    03 -> ทวีเกียรติ
  - the expense category `จ่ายค่าคอมมิชชั่น` stays seeded (idempotent re-insert)

Verified via a real database.init_db() boot (matches how the migration applies
to an existing DB — not the from-empty bootstrap path).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '129_cashbook_commission_writeback.sql'
COMMISSION_CATEGORY = 'จ่ายค่าคอมมิชชั่น'


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_cashbook_transactions_gains_commission_payout_id(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        assert 'commission_payout_id' in _cols(conn, 'cashbook_transactions')
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert applied == 1
    finally:
        conn.close()


def test_salespersons_gains_real_name_column(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        assert 'real_name' in _cols(conn, 'salespersons')
    finally:
        conn.close()


def test_real_name_seeded_for_toe_and_tor(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            r['code']: r['real_name']
            for r in conn.execute(
                "SELECT code, real_name FROM salespersons WHERE code IN ('06','06-L','03')"
            ).fetchall()
        }
        assert rows.get('06') == 'เจียรนัย'
        assert rows.get('06-L') == 'เจียรนัย'
        assert rows.get('03') == 'ทวีเกียรติ'
    finally:
        conn.close()


def test_other_salespersons_real_name_stays_null(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT real_name FROM salespersons WHERE code = '02'"
        ).fetchone()
        assert row is not None, "expect a seeded '02' salesperson in the live DB clone"
        assert row['real_name'] is None
    finally:
        conn.close()


def test_commission_category_active_expense(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT direction, is_active FROM cashbook_categories WHERE name=?",
            (COMMISSION_CATEGORY,),
        ).fetchone()
        assert row is not None, "commission category must be seeded"
        assert row['direction'] == 'expense'
        assert row['is_active'] == 1
    finally:
        conn.close()


def test_migration_idempotent_via_runner(tmp_db):
    database.init_db()
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        n_mig = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert n_mig == 1
        n_cat = conn.execute(
            "SELECT COUNT(*) FROM cashbook_categories WHERE name=? AND direction='expense'",
            (COMMISSION_CATEGORY,),
        ).fetchone()[0]
        assert n_cat == 1, "re-run must not duplicate the seeded category"
    finally:
        conn.close()


def test_commission_payout_id_is_unique_when_set(tmp_db):
    """Two cashbook rows may NOT link the same payout; multiple NULLs are
    fine (every non-commission row keeps commission_payout_id NULL)."""
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        acct = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
        payout_id = conn.execute(
            "INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid, paid_date)"
            " VALUES ('2026-07', '06', 100, '2026-07-01')"
        ).lastrowid

        def _mk(commission_payout_id):
            conn.execute(
                "INSERT INTO cashbook_transactions"
                " (account_id, txn_date, direction, category, amount, commission_payout_id)"
                " VALUES (?, '2026-07-01', 'expense', ?, 100, ?)",
                (acct, COMMISSION_CATEGORY, commission_payout_id),
            )

        _mk(payout_id)
        with pytest.raises(sqlite3.IntegrityError):
            _mk(payout_id)          # same payout linked twice -> UNIQUE violation
        conn.rollback()

        # two NULL-linked rows must both insert cleanly
        _mk(None)
        _mk(None)
        conn.commit()
    finally:
        conn.close()
