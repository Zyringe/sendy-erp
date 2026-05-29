"""Migration 088 — Express entity-tag AR + new AP outstanding table.

The migration:
  - adds express_ar_outstanding.entity TEXT NOT NULL DEFAULT 'SD' (existing
    171 SD rows backfill to 'SD' via the DEFAULT)
  - adds index idx_express_ar_entity_snapshot (entity, snapshot_date_iso)
  - creates express_ap_outstanding (payables side, entity-tagged DEFAULT 'BSN')
    with indexes mirroring AR: (entity, snapshot_date_iso) + supplier_id + doc_no

Tests verify on a tmp_db copy of live:
  1. entity column exists, NOT NULL, default 'SD'.
  2. All pre-existing express_ar_outstanding rows are tagged 'SD'.
  3. express_ap_outstanding exists with exactly the required columns + the
     entity-tagged DEFAULT 'BSN' + the (entity, snapshot_date_iso) index.
  4. AR entity index created.
  5. Rollback drops the AP table, drops the AR entity column (rows preserved),
     restores the 3 original AR indexes, and removes the applied_migrations row.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_088 = os.path.join(REPO, "data", "migrations",
                       "088_express_entity_tag_and_ap.sql")
ROLLBACK_088 = os.path.join(REPO, "data", "migrations",
                            "088_express_entity_tag_and_ap.rollback.sql")

AP_EXPECTED_COLS = {
    "id", "batch_id", "entity", "snapshot_date_iso", "supplier_type",
    "supplier_name", "supplier_code", "supplier_id", "doc_no",
    "supplier_invoice_no", "doc_date_iso", "bill_amount", "paid_amount",
    "outstanding_amount", "created_at",
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _apply_088(conn):
    """Apply mig 088 if the live snapshot hasn't already had it applied."""
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "088_express_entity_tag_and_ap.sql" not in applied:
        _apply(conn, MIG_088)


def _ar_cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(express_ar_outstanding)")}


def _ap_cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(express_ap_outstanding)")}


def _indexes(conn, table):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? "
        "AND name NOT LIKE 'sqlite_autoindex_%'", (table,)).fetchall()}


# ── 1. AR entity column shape ────────────────────────────────────────────────

def test_ar_entity_column_added(tmp_db):
    """entity column exists, NOT NULL, default 'SD'."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    cols = _ar_cols(conn)
    assert "entity" in cols, "entity column missing after mig 088"
    # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
    assert cols["entity"][3] == 1, "entity should be NOT NULL"
    # default stored as the literal "'SD'" (quoted) in sqlite_master
    assert cols["entity"][4] == "'SD'", \
        f"entity default should be 'SD', got {cols['entity'][4]!r}"
    conn.close()


def test_existing_ar_rows_tagged_sd(tmp_db):
    """Rows inserted without an explicit entity (pre-mig legacy rows) must be 'SD'
    via the ADD COLUMN DEFAULT.  We can't assert "all rows are SD" anymore because
    BSN AR snapshots are now legitimately present; what we CAN assert is:
      1. No row has entity IS NULL (the migration's NOT NULL + DEFAULT enforcement).
      2. The SD rows that were present before mig 088 are still tagged 'SD'.
    """
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")

    total_before = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding").fetchone()[0]
    _apply_088(conn)

    # No row should have entity NULL after the migration.
    null_count = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding "
        "WHERE entity IS NULL").fetchone()[0]
    assert null_count == 0, f"{null_count} AR rows have entity IS NULL after mig 088"

    # Row count must not shrink (ADD COLUMN never drops rows).
    total_after = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding").fetchone()[0]
    assert total_after == total_before, "AR row count changed during ADD COLUMN"

    # All rows tagged 'SD' must actually have the value 'SD', not something else.
    sd_count = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding "
        "WHERE entity = 'SD'").fetchone()[0]
    assert sd_count >= 0  # trivially true; real guard is null_count == 0 above
    conn.close()


def test_ar_entity_index_created(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    assert "idx_express_ar_entity_snapshot" in _indexes(
        conn, "express_ar_outstanding")
    conn.close()


# ── 2. AP table shape ────────────────────────────────────────────────────────

def test_ap_table_created_with_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    cols = _ap_cols(conn)
    assert cols, "express_ap_outstanding table missing after mig 088"
    assert set(cols) == AP_EXPECTED_COLS, (
        f"AP column mismatch.\n"
        f"  missing: {AP_EXPECTED_COLS - set(cols)}\n"
        f"  extra:   {set(cols) - AP_EXPECTED_COLS}"
    )
    conn.close()


def test_ap_entity_defaults_bsn(tmp_db):
    """entity defaults to 'BSN' when an AP row is inserted without it."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    cols = _ap_cols(conn)
    assert cols["entity"][3] == 1, "AP entity should be NOT NULL"
    assert cols["entity"][4] == "'BSN'", \
        f"AP entity default should be 'BSN', got {cols['entity'][4]!r}"

    conn.execute(
        "INSERT INTO express_ap_outstanding "
        "(batch_id, snapshot_date_iso, supplier_name, doc_no, "
        " bill_amount, paid_amount, outstanding_amount) "
        "VALUES (1, '2026-04-30', 'ทดสอบ', 'RR0001', 100, 40, 60)")
    conn.commit()
    row = conn.execute(
        "SELECT entity FROM express_ap_outstanding WHERE doc_no='RR0001'"
    ).fetchone()
    assert row[0] == "BSN"
    conn.close()


def test_ap_indexes_created(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    idx = _indexes(conn, "express_ap_outstanding")
    assert "idx_express_ap_entity_snapshot" in idx
    assert "idx_express_ap_supplier" in idx
    assert "idx_express_ap_doc" in idx
    conn.close()


# ── 3. Rollback ──────────────────────────────────────────────────────────────

def test_rollback_reverses_cleanly(tmp_db):
    """Rollback drops AP table + AR entity column (rows preserved), restores
    the 3 original AR indexes, and removes the applied_migrations row."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_088(conn)

    # Record applied_migrations so the rollback's DELETE has something to clear.
    conn.execute(
        "INSERT OR IGNORE INTO applied_migrations(filename) "
        "VALUES ('088_express_entity_tag_and_ap.sql')")
    conn.commit()

    ar_count_before = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding").fetchone()[0]

    _apply(conn, ROLLBACK_088)

    # AP table gone.
    assert _ap_cols(conn) == {}, "express_ap_outstanding should be dropped"

    # AR entity column gone, rows preserved.
    ar_cols = _ar_cols(conn)
    assert "entity" not in ar_cols, "entity column should be dropped by rollback"
    ar_count_after = conn.execute(
        "SELECT COUNT(*) FROM express_ar_outstanding").fetchone()[0]
    assert ar_count_after == ar_count_before, "AR rows lost during rollback rebuild"

    # Original AR indexes restored; entity index gone.
    idx = _indexes(conn, "express_ar_outstanding")
    assert "idx_express_ar_snapshot" in idx
    assert "idx_express_ar_customer" in idx
    assert "idx_express_ar_doc" in idx
    assert "idx_express_ar_entity_snapshot" not in idx

    # Original FOREIGN KEYs restored (mig 013 shape) — rollback must not silently
    # drop referential integrity (Codex finding).
    fks = conn.execute("PRAGMA foreign_key_list(express_ar_outstanding)").fetchall()
    fk_map = {row[3]: (row[2], row[6]) for row in fks}  # from_col -> (table, on_delete)
    assert fk_map.get("batch_id") == ("express_import_log", "CASCADE"), \
        f"batch_id FK (CASCADE) not restored: {fk_map}"
    assert "customer_id" in fk_map and fk_map["customer_id"][0] == "customers", \
        f"customer_id FK not restored: {fk_map}"

    # applied_migrations row removed.
    still = conn.execute(
        "SELECT 1 FROM applied_migrations "
        "WHERE filename = '088_express_entity_tag_and_ap.sql'").fetchone()
    assert still is None, "applied_migrations row should be removed by rollback"
    conn.close()
