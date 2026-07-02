"""Migration 123 — cashbook manual-entry + salary pay-event groundwork.

Renumbered from the plan's "122" to 123: a sibling in-flight worktree
(feat/product-creation-consolidation) had already claimed
122_product_created_via.sql against the shared local dev DB before this
migration was written. The two migrations touch disjoint tables, so there is
no functional collision — only the number changed.

Verifies (via a real `database.init_db()` boot, matching how this migration
applies to an actual existing DB — not the from-empty bootstrap path):
  - cashbook_transactions gains created_by, payroll_run_id, payroll_item_id
  - employees gains default_cashbook_account_id
  - the migration applies cleanly (no error) and is recorded in
    applied_migrations exactly once

Seed caveat: the migration's per-employee default_cashbook_account_id
backfill (matches each employee's most-recent hand-typed เงินเดือน row) runs
during init_db() BEFORE any test data exists in a fresh tmp_db copy taken at
module load time, so on THIS copy it may or may not match real history rows
that predate the copy — it is a best-effort UI default, not asserted here.
It should be spot-checked against the real DB after this migration ships
there (e.g. one known employee's account matches their last salary payment).
"""
import sqlite3

import database


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_cashbook_and_employees_gain_new_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # database.DATABASE_PATH is monkeypatched by tmp_db; init_db() opens its
    # own connection via get_connection() against the same temp file.
    database.init_db()

    cb_cols = _cols(conn, "cashbook_transactions")
    assert {"created_by", "payroll_run_id", "payroll_item_id"} <= cb_cols

    emp_cols = _cols(conn, "employees")
    assert "default_cashbook_account_id" in emp_cols

    applied = conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '123_cashbook_manual_entry.sql'"
    ).fetchone()[0]
    assert applied == 1

    conn.close()


def test_migration_is_idempotent_via_runner(tmp_db):
    """Running init_db() twice must not error (runner skips already-applied
    files) and must not duplicate the applied_migrations row."""
    database.init_db()
    database.init_db()

    conn = sqlite3.connect(tmp_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM applied_migrations "
        "WHERE filename = '123_cashbook_manual_entry.sql'"
    ).fetchone()[0]
    assert n == 1
    conn.close()
