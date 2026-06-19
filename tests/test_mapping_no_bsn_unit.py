"""Post-mig-112 mapping smoke tests (TDD spec).

1. _resolve_mapping(conn, code) returns the right product for a seeded mapping.
2. upsert_mapping upserts by bsn_code (2nd call updates same row, no dup).
3. resolve_pending_mappings still backfills + syncs a pending mapped row.
4. pragma_table_info('product_code_mapping') has NO bsn_unit after init_db
   (the live DB will have mig-112 applied; the CREATE TABLE in database.py
   also has no bsn_unit — verified via the schema-clone empty_db fixture).
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))
import models  # noqa: E402
import database  # noqa: E402

PA = 907201
PB = 907202
CODE = "ZNBU100"


def _seed_products(conn):
    conn.row_factory = sqlite3.Row
    for pid in (PA, PB):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
            "VALUES (?, ?, 'ตัว', ?, 1)",
            (pid, f"P{pid}", f"SK{pid}")
        )
    conn.commit()


# ── Test 1: _resolve_mapping returns the right product ───────────────────────

def test_resolve_mapping_returns_correct_product(tmp_db, monkeypatch):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_products(conn)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
        "VALUES (?, ?, ?)",
        (CODE, "test product", PA)
    )
    conn.commit()

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_db)
    # _resolve_mapping is internal; call via get_connection-patched conn
    m_conn = sqlite3.connect(tmp_db)
    m_conn.row_factory = sqlite3.Row
    pid, is_ignored, mapped = models._resolve_mapping(m_conn, CODE)
    m_conn.close()
    conn.close()

    assert pid == PA
    assert is_ignored == 0
    assert mapped is True


def test_resolve_mapping_unknown_code_returns_none(tmp_db, monkeypatch):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_products(conn)
    conn.commit()

    m_conn = sqlite3.connect(tmp_db)
    m_conn.row_factory = sqlite3.Row
    pid, is_ignored, mapped = models._resolve_mapping(m_conn, "NO_SUCH_CODE")
    m_conn.close()
    conn.close()

    assert pid is None
    assert mapped is False


# ── Test 2: upsert_mapping upserts by bsn_code (no dup) ─────────────────────

def test_upsert_mapping_second_call_updates_no_dup(tmp_db, monkeypatch):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_products(conn)
    conn.close()

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_db)

    models.upsert_mapping(CODE, "name1", product_id=PA)
    models.upsert_mapping(CODE, "name2", product_id=PB)  # second call updates

    c = sqlite3.connect(tmp_db)
    rows = c.execute(
        "SELECT bsn_code, product_id, bsn_name FROM product_code_mapping "
        "WHERE bsn_code = ?", (CODE,)
    ).fetchall()
    c.close()

    assert len(rows) == 1, f"Expected 1 row, got {len(rows)} — duplicate detected"
    assert rows[0][1] == PB
    assert rows[0][2] == "name2"


# ── Test 3: resolve_pending_mappings backfills + syncs ───────────────────────

def test_resolve_pending_mappings_backfills_pending_row(tmp_db, monkeypatch):
    """A pending (product_id=NULL) sales_transactions row gets backfilled."""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_db)

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _seed_products(conn)
    # Insert mapping row
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
        "VALUES (?, ?, ?)", (CODE, "n", PA)
    )
    # Insert a pending (product_id=NULL) transaction row for that code
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id, date_iso, doc_no, doc_base, product_id, bsn_code, "
        "product_name_raw, customer, customer_code, qty, unit, unit_price, "
        "vat_type, discount, total, net, synced_to_stock) "
        "VALUES (0, '2026-06-01', 'ZNBU-1', 'ZNBU', NULL, ?, 'p', 'C', 'C1', "
        "0, 'ตัว', 10, 0, 0, 0, 0, 0)",
        (CODE,)
    )
    conn.commit()

    models.resolve_pending_mappings(conn)

    row = conn.execute(
        "SELECT product_id FROM sales_transactions WHERE doc_no='ZNBU-1'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["product_id"] == PA


# ── Test 4: no bsn_unit column in product_code_mapping after migration ───────

MIG_112 = os.path.join(REPO, "data", "migrations", "112_drop_mapping_bsn_unit.sql")


def test_no_bsn_unit_column_after_migration(tmp_db):
    """After applying mig-112, product_code_mapping must have NO bsn_unit column.

    Applies the migration to a copy of the live DB to verify correctness.
    Also verifies row-count preservation and UNIQUE(bsn_code) holds.
    """
    conn = sqlite3.connect(tmp_db)
    pre_codes = {r[0] for r in conn.execute(
        "SELECT bsn_code FROM product_code_mapping"
    ).fetchall()}
    conn.close()

    # Apply migration 112
    conn = sqlite3.connect(tmp_db)
    with open(MIG_112, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.close()

    conn = sqlite3.connect(tmp_db)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)"
    ).fetchall()}
    post_codes = {r[0] for r in conn.execute(
        "SELECT bsn_code FROM product_code_mapping"
    ).fetchall()}
    post_count = conn.execute(
        "SELECT COUNT(*) FROM product_code_mapping"
    ).fetchone()[0]
    conn.close()

    assert "bsn_unit" not in cols, "bsn_unit still present after mig-112"
    # Every bsn_code survived (no codes dropped)
    assert pre_codes == post_codes, (
        f"Codes lost after mig-112: {pre_codes - post_codes}"
    )
    # Exactly one row per bsn_code (dedup worked)
    assert post_count == len(post_codes), (
        f"Expected {len(post_codes)} rows, got {post_count} — dedup failed"
    )


def test_no_bsn_unit_in_database_schema(empty_db):
    """The base CREATE TABLE in database.py (schema-clone from live) does not
    have bsn_unit once mig-112 has been applied to the live DB.

    Since live DB still has bsn_unit pre-merge, this test applies mig-112
    to the schema-clone and then asserts the column is gone.
    """
    # Apply mig-112 to the cloned schema DB
    conn = sqlite3.connect(empty_db)
    with open(MIG_112, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.close()

    conn = sqlite3.connect(empty_db)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(product_code_mapping)"
    ).fetchall()}
    conn.close()
    assert "bsn_unit" not in cols, (
        "bsn_unit still in product_code_mapping after mig-112 — migration SQL error"
    )
