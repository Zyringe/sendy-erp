"""Migration 128 — Phase 2 of the cashbook /new overhaul
(projects/cashbook-entry-reconcile/plan.md, decision C5, finding #2).

Adds the schema the cashbook-sourced advance flow needs:
  - cashbook_transactions.salary_advance_id  (FK -> salary_advances, the
    "linked & locked row" link, symmetric with payroll_item_id for salary)
  - a UNIQUE index on it (one cashbook row <-> one advance; nullable, so the
    thousands of non-advance rows keep their NULLs)
  - the expense category `เงินเดือน (เบิกล่วงหน้า)` (seeded, source='setup')

Verified via a real database.init_db() boot (matches how the migration applies
to an existing DB — not the from-empty bootstrap path).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import database

MIG = '128_cashbook_advance_writeback.sql'
ADVANCE_CATEGORY = 'เงินเดือน (เบิกล่วงหน้า)'


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_cashbook_transactions_gains_salary_advance_id(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    try:
        assert 'salary_advance_id' in _cols(conn, 'cashbook_transactions')
        applied = conn.execute(
            "SELECT COUNT(*) FROM applied_migrations WHERE filename = ?", (MIG,)
        ).fetchone()[0]
        assert applied == 1
    finally:
        conn.close()


def test_advance_category_seeded_active_expense(tmp_db):
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT direction, is_active FROM cashbook_categories WHERE name=?",
            (ADVANCE_CATEGORY,),
        ).fetchone()
        assert row is not None, "advance category must be seeded"
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
            (ADVANCE_CATEGORY,),
        ).fetchone()[0]
        assert n_cat == 1, "re-run must not duplicate the seeded category"
    finally:
        conn.close()


def test_salary_advance_id_is_unique_when_set(tmp_db):
    """Two cashbook rows may NOT link the same advance; multiple NULLs are fine
    (every non-advance row keeps salary_advance_id NULL)."""
    database.init_db()
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        acct = conn.execute(
            "SELECT id FROM cashbook_accounts WHERE is_active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
        emp = conn.execute("SELECT id FROM employees LIMIT 1").fetchone()[0]
        adv = conn.execute(
            "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id)"
            " VALUES (?, '2026-07-01', 100, ?)",
            (emp, acct),
        ).lastrowid

        def _mk(salary_advance_id):
            conn.execute(
                "INSERT INTO cashbook_transactions"
                " (account_id, txn_date, direction, category, amount, salary_advance_id)"
                " VALUES (?, '2026-07-01', 'expense', ?, 100, ?)",
                (acct, ADVANCE_CATEGORY, salary_advance_id),
            )

        _mk(adv)
        with pytest.raises(sqlite3.IntegrityError):
            _mk(adv)          # same advance linked twice -> UNIQUE violation
        conn.rollback()

        # two NULL-linked rows must both insert cleanly
        _mk(None)
        _mk(None)
        conn.commit()
    finally:
        conn.close()
