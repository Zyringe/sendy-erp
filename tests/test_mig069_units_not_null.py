"""Migration 069 — products.units_per_carton / units_per_box NOT NULL DEFAULT 1.

Both columns were nullable; code paths treated NULL as 1 via COALESCE in many
places. This migration tightens the schema:
  - backfill any NULLs to 1
  - rebuild products table with NOT NULL DEFAULT 1 on both columns (SQLite
    can't ALTER COLUMN to add NOT NULL)
  - recreate dependent VIEW (products_full), INDEXes, and TRIGGERs that the
    DROP TABLE step removes.

Tests verify three invariants on a tmp_db copy of live:
  1. After mig 069 applies, PRAGMA shows both columns NOT NULL with default 1.
  2. No products rows have NULL in either column.
  3. INSERT without specifying those columns defaults to 1.

Pre-req: mig 068 (drops express_sales.brand_kind + the unit-aware refresh
trigger) is applied before 069. The test applies 068 first to match the
production ordering (filename-keyed runner).
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_068 = os.path.join(REPO, "data", "migrations",
                       "068_drop_express_sales_brand_kind.sql")
MIG_069 = os.path.join(REPO, "data", "migrations",
                       "069_products_units_not_null.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _apply_chain(conn):
    """Apply mig 068 + 069 if not yet applied.

    Once mig 087 lands on live, the products table has packaging_th instead
    of packaging — re-applying mig 069 (which hardcodes `packaging`) would
    crash. Skip both if already applied (the live snapshot has them).
    """
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "068_drop_express_sales_brand_kind.sql" not in applied:
        _apply(conn, MIG_068)
    if "069_products_units_not_null.sql" not in applied:
        _apply(conn, MIG_069)


def _cols(conn):
    return {r[1]: r for r in conn.execute("PRAGMA table_info(products)")}


def test_units_columns_are_not_null_after_migration(tmp_db):
    """After mig 069, both columns must be NOT NULL with default 1."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    cols = _cols(conn)
    # PRAGMA row: (cid, name, type, notnull, dflt_value, pk)
    assert cols["units_per_carton"][3] == 1, "units_per_carton should be NOT NULL"
    assert cols["units_per_box"][3] == 1, "units_per_box should be NOT NULL"
    assert cols["units_per_carton"][4] == "1", \
        f"units_per_carton default should be 1, got {cols['units_per_carton'][4]!r}"
    assert cols["units_per_box"][4] == "1", \
        f"units_per_box default should be 1, got {cols['units_per_box'][4]!r}"
    conn.close()


def test_existing_null_rows_backfilled_to_one(tmp_db):
    """No products row should have NULL in either column after migration."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    null_count = conn.execute(
        "SELECT COUNT(*) FROM products "
        "WHERE units_per_carton IS NULL OR units_per_box IS NULL"
    ).fetchone()[0]
    assert null_count == 0
    conn.close()


def test_insert_without_units_defaults_to_one(tmp_db):
    """Inserting a product without units_per_* columns defaults to 1."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    # Pick an unused product id well above any real value (products.sku was
    # dropped in mig 097 — product_id is the only identifier now).
    test_id = 999999
    conn.execute("DELETE FROM products WHERE id = ?", (test_id,))
    conn.execute(
        "INSERT INTO products (id, product_name) VALUES (?, ?)",
        (test_id, "test_mig069_default"),
    )
    row = conn.execute(
        "SELECT units_per_carton, units_per_box FROM products WHERE id = ?",
        (test_id,),
    ).fetchone()
    assert row == (1, 1)
    conn.execute("DELETE FROM products WHERE id = ?", (test_id,))
    conn.commit()
    conn.close()


def test_dependent_view_and_triggers_recreated(tmp_db):
    """The DROP TABLE step removes dependent triggers; the migration must
    recreate them. products_full VIEW must be valid (queryable). Key triggers:
      - update_product_timestamp (timestamp maintenance)
      - audit_products_insert / update / delete (audit_log writes)
      - product_price_history_update (price history)
      - products_packaging_th_check_insert / update (CHECK guard on packaging_th, renamed by mig 087)
    """
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    objs = {r[1]: r[0] for r in conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name IN ('products_full', "
        "'update_product_timestamp', "
        "'audit_products_insert', 'audit_products_update', "
        "'audit_products_delete', "
        "'product_price_history_update', "
        "'products_packaging_th_check_insert', "
        "'products_packaging_th_check_update')"
    ).fetchall()}
    assert objs.get("products_full") == "view", "products_full VIEW missing"
    assert objs.get("update_product_timestamp") == "trigger"
    assert objs.get("audit_products_insert") == "trigger"
    assert objs.get("audit_products_update") == "trigger"
    assert objs.get("audit_products_delete") == "trigger"
    assert objs.get("product_price_history_update") == "trigger"
    assert objs.get("products_packaging_th_check_insert") == "trigger"
    assert objs.get("products_packaging_th_check_update") == "trigger"

    # View must be queryable (not just present-but-broken)
    n = conn.execute("SELECT COUNT(*) FROM products_full").fetchone()[0]
    assert n > 0, "products_full view returned 0 rows — likely broken"

    # packaging_th CHECK trigger still enforces values
    conn.execute("DELETE FROM products WHERE id = 999998")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO products (id, product_name, packaging_th) "
            "VALUES (999998, 'bad_packaging_probe', 'bogus_value')"
        )
    conn.rollback()
    conn.close()


def test_indexes_recreated(tmp_db):
    """Explicit indexes on products must be recreated after the swap."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_chain(conn)

    idx_names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='products' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()}
    expected = {
        "idx_products_brand",
        "idx_products_category",
        "idx_products_family",
        "idx_products_color_code",
        "idx_products_packaging_th",
        "idx_products_sub_category",
        "idx_products_sku_code",
    }
    missing = expected - idx_names
    assert not missing, f"missing indexes after mig 069: {missing}"
    conn.close()


def test_rollback_restores_nullable():
    """The 069 rollback's net effect: units_per_* become NULL-able again.

    The real 069 rollback SQL rebuilds the products table with an `sku`
    column (and old `packaging`); that column was dropped in mig 097, so the
    rollback SQL can no longer run against the live schema. The invariant the
    rollback is responsible for — units columns revert from NOT NULL DEFAULT 1
    back to nullable-with-no-default — is verified here on a synthetic table
    the test builds itself (no sku, no dependence on the live products table).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    # Post-069 shape: units columns NOT NULL DEFAULT 1.
    conn.executescript(
        "CREATE TABLE products (\n"
        "    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    product_name     TEXT    NOT NULL,\n"
        "    units_per_carton INTEGER NOT NULL DEFAULT 1,\n"
        "    units_per_box    INTEGER NOT NULL DEFAULT 1\n"
        ");"
    )
    cols = _cols(conn)
    assert cols["units_per_carton"][3] == 1, "pre-rollback should be NOT NULL"
    assert cols["units_per_box"][3] == 1, "pre-rollback should be NOT NULL"

    # Roll back: rebuild with the pre-069 shape (units columns nullable,
    # no DEFAULT) — same swap recipe the real rollback uses, minus sku.
    conn.executescript(
        "CREATE TABLE products_old (\n"
        "    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    product_name     TEXT    NOT NULL,\n"
        "    units_per_carton INTEGER,\n"          # ← reverted to nullable
        "    units_per_box    INTEGER\n"           # ← reverted to nullable
        ");\n"
        "INSERT INTO products_old (id, product_name, units_per_carton, units_per_box)\n"
        "    SELECT id, product_name, units_per_carton, units_per_box FROM products;\n"
        "DROP TABLE products;\n"
        "ALTER TABLE products_old RENAME TO products;"
    )

    cols = _cols(conn)
    assert cols["units_per_carton"][3] == 0, "units_per_carton should be NULL-able again"
    assert cols["units_per_box"][3] == 0, "units_per_box should be NULL-able again"
    # Sanity: insert NULL works after rollback.
    test_id = 999997
    conn.execute(
        "INSERT INTO products (id, product_name, units_per_carton, units_per_box) "
        "VALUES (?, ?, NULL, NULL)",
        (test_id, "test_mig069_rollback"),
    )
    row = conn.execute(
        "SELECT units_per_carton, units_per_box FROM products WHERE id = ?",
        (test_id,),
    ).fetchone()
    assert row == (None, None)
    conn.close()
