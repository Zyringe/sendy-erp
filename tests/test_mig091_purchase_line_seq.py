"""Migration 091 — formalize purchase_transactions.line_seq.

The migration:
  - adds purchase_transactions.line_seq INTEGER NOT NULL DEFAULT 1 (existing
    rows backfill to 1 via the DEFAULT)

The asymmetry vs sales_transactions (which has NO line_seq) is intentional:
purchase doc_nos lack the line-suffix that sales doc_nos carry, so purchase
lines need line_seq for unique identity.

Idempotency note: the loader ran the raw ALTER on the dev DB, so the column may
already be present on the cloned live snapshot. `_apply_091` skips the forward
ALTER if line_seq already exists (mirrors test_mig088's _apply_088 guard which
skips when the migration is already recorded). Either way the post-state is the
same — column present, NOT NULL, default 1.

Tests verify on a tmp_db copy of live:
  1. line_seq column exists, NOT NULL, default 1.
  2. No existing purchase_transactions row is lost (ADD COLUMN never drops rows).
  3. New rows inserted without line_seq default to 1.
  4. sales_transactions is NOT touched (no line_seq column).
  5. Rollback drops line_seq (rows preserved), restores the 2 original indexes
     and the 3 original FKs, and removes the applied_migrations row.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_091 = os.path.join(REPO, "data", "migrations",
                       "091_purchase_transactions_line_seq.sql")
ROLLBACK_091 = os.path.join(REPO, "data", "migrations",
                            "091_purchase_transactions_line_seq.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _pt_cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(purchase_transactions)")}


def _apply_091(conn):
    """Apply mig 091 unless the column is already present on the live snapshot
    (the loader ALTER'd dev directly). SQLite ADD COLUMN has no IF NOT EXISTS,
    so re-running would throw — guard the same way the runner is reconciled."""
    if "line_seq" not in _pt_cols(conn):
        _apply(conn, MIG_091)
    else:
        # Mirror the forward file's self-record so the rollback's DELETE has a
        # row to clear (the dev DB gets this row out-of-band; tests synthesize it).
        conn.execute(
            "INSERT OR IGNORE INTO applied_migrations(filename) "
            "VALUES ('091_purchase_transactions_line_seq.sql')")
        conn.commit()


def _indexes(conn, table):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? "
        "AND name NOT LIKE 'sqlite_autoindex_%'", (table,)).fetchall()}


# ── 1. Column shape ───────────────────────────────────────────────────────────

def test_line_seq_column_added(tmp_db):
    """line_seq exists, NOT NULL, default 1."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_091(conn)

    cols = _pt_cols(conn)
    assert "line_seq" in cols, "line_seq column missing after mig 091"
    # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
    assert cols["line_seq"][2] == "INTEGER"
    assert cols["line_seq"][3] == 1, "line_seq should be NOT NULL"
    assert cols["line_seq"][4] == "1", \
        f"line_seq default should be 1, got {cols['line_seq'][4]!r}"
    conn.close()


def test_existing_rows_preserved(tmp_db):
    """ADD COLUMN never drops rows; count unchanged across the migration."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    before = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
    _apply_091(conn)
    after = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
    assert after == before, "purchase_transactions row count changed during ADD COLUMN"
    # Every existing row has a non-NULL line_seq (NOT NULL + DEFAULT 1).
    nulls = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions WHERE line_seq IS NULL"
    ).fetchone()[0]
    assert nulls == 0, f"{nulls} rows have line_seq IS NULL after mig 091"
    conn.close()


def test_new_row_defaults_to_1(tmp_db):
    """A row inserted without line_seq defaults to 1."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_091(conn)

    conn.execute(
        "INSERT INTO purchase_transactions (date_iso, doc_no) "
        "VALUES ('2026-05-30', 'TEST-MIG091')")
    conn.commit()
    row = conn.execute(
        "SELECT line_seq FROM purchase_transactions WHERE doc_no='TEST-MIG091'"
    ).fetchone()
    assert row[0] == 1
    conn.close()


def test_sales_transactions_not_touched(tmp_db):
    """The asymmetry is justified: sales_transactions must NOT gain line_seq."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_091(conn)

    st_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(sales_transactions)")}
    assert "line_seq" not in st_cols, \
        "sales_transactions must NOT have line_seq (intentional asymmetry)"
    conn.close()


# ── 2. Rollback ───────────────────────────────────────────────────────────────

def test_rollback_reverses_cleanly(tmp_db):
    """Rollback drops line_seq (rows preserved), restores the 2 original indexes
    and the 3 original FKs, and removes the applied_migrations row."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_091(conn)

    count_before = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]

    _apply(conn, ROLLBACK_091)

    # line_seq column gone, rows preserved.
    cols = _pt_cols(conn)
    assert "line_seq" not in cols, "line_seq should be dropped by rollback"
    count_after = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions").fetchone()[0]
    assert count_after == count_before, "rows lost during rollback rebuild"

    # Original indexes restored.
    idx = _indexes(conn, "purchase_transactions")
    assert "idx_pt_doc_base" in idx
    assert "idx_pt_supplier_id" in idx

    # Original FOREIGN KEYs restored (suppliers / products / import_log).
    fks = conn.execute(
        "PRAGMA foreign_key_list(purchase_transactions)").fetchall()
    fk_tables = {row[2] for row in fks}  # referenced table names
    assert "suppliers" in fk_tables, f"supplier_id FK not restored: {fk_tables}"
    assert "products" in fk_tables, f"product_id FK not restored: {fk_tables}"
    assert "import_log" in fk_tables, f"batch_id FK not restored: {fk_tables}"

    # applied_migrations row removed.
    still = conn.execute(
        "SELECT 1 FROM applied_migrations "
        "WHERE filename = '091_purchase_transactions_line_seq.sql'").fetchone()
    assert still is None, "applied_migrations row should be removed by rollback"
    conn.close()


def test_rollback_preserves_batch_37_rows(tmp_db):
    """The 4018 batch_id=37 rows (loader-added) survive the rollback rebuild."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_091(conn)

    b37_before = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions WHERE batch_id=37"
    ).fetchone()[0]

    _apply(conn, ROLLBACK_091)

    b37_after = conn.execute(
        "SELECT COUNT(*) FROM purchase_transactions WHERE batch_id=37"
    ).fetchone()[0]
    assert b37_after == b37_before, \
        f"batch_id=37 rows changed during rollback: {b37_before} -> {b37_after}"
    conn.close()
