"""Migration 094 — platform product-level info for the Full Shopee/Lazada
product-export import.

The migration is purely ADDITIVE:
  - creates NEW table `platform_products` (product grain), keyed by
    UNIQUE(platform, product_id_str), with a CHECK on platform.
  - adds 8 nullable variation-grain columns to `platform_skus`.

Column names are FIXED — a parallel importer + spec depend on these exact
names, so the tests pin every name.

Tests run on a tmp_db copy of live:
  1. platform_products exists with the full expected column set.
  2. platform_products enforces CHECK(platform IN ('shopee','lazada')).
  3. platform_products enforces UNIQUE(platform, product_id_str).
  4. platform_skus gains the 8 new columns (and keeps all originals + rows).
  5. The forward-looking parent_sku index exists.
  6. Rollback drops the table + the 8 columns and clears applied_migrations,
     leaving platform_skus byte-identical to its pre-migration shape.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_094 = os.path.join(REPO, "data", "migrations",
                       "094_platform_product_info.sql")
ROLLBACK_094 = os.path.join(REPO, "data", "migrations",
                            "094_platform_product_info.rollback.sql")

EXPECTED_PP_COLS = {
    "id", "platform", "product_id_str", "parent_sku", "product_name",
    "name_en", "description", "category_id_str", "category_name", "brand",
    "place_of_origin", "material", "warranty_policy", "warranty_period",
    "status", "cover_image_url", "image_urls", "dts_info", "raw_json",
    "imported_at",
}

NEW_SKU_COLS = {
    "weight_kg", "length_cm", "width_cm", "height_cm", "gtin",
    "special_price_start", "special_price_end", "variation_image_url",
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _pp_cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(platform_products)")}


def _sku_cols(conn):
    return {r[1]: r for r in conn.execute(
        "PRAGMA table_info(platform_skus)")}


def _apply_094(conn):
    """Apply mig 094 unless platform_products already exists on the snapshot.
    Mirrors test_mig091's guard: SQLite ADD COLUMN / CREATE TABLE re-run would
    only matter if applied out-of-band; the post-state is identical either way."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='platform_products'").fetchone()
    if exists is None:
        _apply(conn, MIG_094)
    else:
        conn.execute(
            "INSERT OR IGNORE INTO applied_migrations(filename) "
            "VALUES ('094_platform_product_info.sql')")
        conn.commit()


# ── 1. platform_products table shape ───────────────────────────────────────────

def test_platform_products_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _apply_094(conn)

    cols = _pp_cols(conn)
    assert set(cols) == EXPECTED_PP_COLS, (
        f"column mismatch: missing={EXPECTED_PP_COLS - set(cols)}, "
        f"extra={set(cols) - EXPECTED_PP_COLS}")
    # id is the integer PK.
    assert cols["id"][5] == 1, "id should be PRIMARY KEY"
    # NOT NULL columns per spec.
    assert cols["platform"][3] == 1, "platform should be NOT NULL"
    assert cols["product_id_str"][3] == 1, "product_id_str should be NOT NULL"
    assert cols["imported_at"][3] == 1, "imported_at should be NOT NULL"
    conn.close()


def test_platform_check_constraint(tmp_db):
    """platform restricted to shopee/lazada."""
    conn = sqlite3.connect(tmp_db)
    _apply_094(conn)

    # valid platforms accepted
    conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                 "VALUES ('shopee', 'S1')")
    conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                 "VALUES ('lazada', 'L1')")
    conn.commit()

    # invalid platform rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                     "VALUES ('tiktok', 'T1')")
        conn.commit()
    conn.rollback()
    conn.close()


def test_platform_products_unique(tmp_db):
    """UNIQUE(platform, product_id_str): same id_str on different platforms OK,
    duplicate within a platform rejected."""
    conn = sqlite3.connect(tmp_db)
    _apply_094(conn)

    conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                 "VALUES ('shopee', 'DUP')")
    conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                 "VALUES ('lazada', 'DUP')")  # different platform, allowed
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO platform_products(platform, product_id_str) "
                     "VALUES ('shopee', 'DUP')")  # duplicate, rejected
        conn.commit()
    conn.rollback()
    conn.close()


def test_parent_sku_index_exists(tmp_db):
    conn = sqlite3.connect(tmp_db)
    _apply_094(conn)

    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='platform_products' "
        "AND name NOT LIKE 'sqlite_autoindex_%'").fetchall()}
    assert "idx_platform_products_parent_sku" in idx
    conn.close()


# ── 2. platform_skus new columns ───────────────────────────────────────────────

def test_platform_skus_new_columns(tmp_db):
    conn = sqlite3.connect(tmp_db)

    before = _sku_cols(conn)
    row_before = conn.execute(
        "SELECT COUNT(*) FROM platform_skus").fetchone()[0]

    _apply_094(conn)

    after = _sku_cols(conn)
    # all 8 new columns present
    assert NEW_SKU_COLS <= set(after), \
        f"missing new columns: {NEW_SKU_COLS - set(after)}"
    # all original columns preserved
    assert set(before) <= set(after), \
        f"original columns lost: {set(before) - set(after)}"
    # column types per spec (cid, name, type, notnull, dflt, pk)
    assert after["weight_kg"][2] == "REAL"
    assert after["length_cm"][2] == "REAL"
    assert after["width_cm"][2] == "REAL"
    assert after["height_cm"][2] == "REAL"
    assert after["gtin"][2] == "TEXT"
    assert after["special_price_start"][2] == "TEXT"
    assert after["special_price_end"][2] == "TEXT"
    assert after["variation_image_url"][2] == "TEXT"
    # all new columns nullable (notnull flag == 0)
    for c in NEW_SKU_COLS:
        assert after[c][3] == 0, f"{c} should be nullable"

    # ADD COLUMN never drops rows
    row_after = conn.execute(
        "SELECT COUNT(*) FROM platform_skus").fetchone()[0]
    assert row_after == row_before, "platform_skus rows changed during ADD COLUMN"
    conn.close()


# ── 3. Rollback ────────────────────────────────────────────────────────────────

def test_rollback_reverses_cleanly(tmp_db):
    """Rollback drops platform_products + the 8 columns, clears the
    applied_migrations row, and leaves platform_skus identical to pre-state.

    The live snapshot may already carry mig 094 (once it has run on the dev DB),
    so we cannot read the pre-state baseline straight off the snapshot. Instead
    we normalise to the genuine pre-094 state first: if the migration is already
    present, roll it back to capture the true baseline, then apply forward and
    roll back again and compare. This makes the assertion correct on both a
    pre-094 and a post-094 snapshot."""
    conn = sqlite3.connect(tmp_db)

    already = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='platform_products'").fetchone() is not None
    if already:
        _apply(conn, ROLLBACK_094)  # normalise to true pre-094 baseline

    sku_before = [tuple(r) for r in conn.execute(
        "PRAGMA table_info(platform_skus)")]
    rows_before = conn.execute(
        "SELECT COUNT(*) FROM platform_skus").fetchone()[0]

    _apply(conn, MIG_094)
    _apply(conn, ROLLBACK_094)

    # platform_products gone
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='platform_products'").fetchone() is None, \
        "platform_products should be dropped by rollback"

    # 8 columns gone, platform_skus schema byte-identical to pre-state
    sku_after = [tuple(r) for r in conn.execute(
        "PRAGMA table_info(platform_skus)")]
    assert sku_after == sku_before, \
        "platform_skus schema differs from pre-migration state after rollback"

    rows_after = conn.execute(
        "SELECT COUNT(*) FROM platform_skus").fetchone()[0]
    assert rows_after == rows_before, "platform_skus rows lost during rollback"

    # applied_migrations row removed
    assert conn.execute(
        "SELECT 1 FROM applied_migrations "
        "WHERE filename='094_platform_product_info.sql'").fetchone() is None, \
        "applied_migrations row should be removed by rollback"
    conn.close()
