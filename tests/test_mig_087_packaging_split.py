"""Migration 087 — drop products.material, rename packaging→packaging_th, add packaging_short.

Verifies schema change, packaging_short backfill from PACKAGING_SHORT dict,
whitelist triggers on both new columns, products_full VIEW recreation,
index rename, audit/timestamp/price-history triggers preserved, and
rollback path (per feedback_rename_migration_safety: rollback reads
CURRENT table so post-mig inserts survive).
"""
import os
import sqlite3

import pytest


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_087 = os.path.join(REPO, "data", "migrations",
                       "087_drop_material_split_packaging.sql")
ROLLBACK_087 = os.path.join(REPO, "data", "migrations",
                            "087_drop_material_split_packaging.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _apply_if_not_applied(conn):
    """Idempotent forward-apply. Once mig 087 is applied to live DB the tmp_db
    fixture inherits its post-mig schema; re-applying would crash on duplicate
    triggers, so skip when already in applied_migrations.
    """
    applied = {r[0] for r in conn.execute(
        "SELECT filename FROM applied_migrations").fetchall()}
    if "087_drop_material_split_packaging.sql" not in applied:
        _apply(conn, MIG_087)


def _columns(conn, table):
    return {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}


def _view_columns(conn, view):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({view})")}


def _indexes(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _triggers(conn, table):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
        (table,)
    )}


# ── Forward mig: schema shape ────────────────────────────────────────────────

def test_post_mig_drops_material_column(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    assert "material" not in _columns(tmp_db_conn, "products")


def test_post_mig_renames_packaging_to_packaging_th(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    cols = _columns(tmp_db_conn, "products")
    assert "packaging_th" in cols
    assert "packaging" not in cols


def test_post_mig_adds_packaging_short_column(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    cols = _columns(tmp_db_conn, "products")
    assert "packaging_short" in cols
    assert cols["packaging_short"] == "TEXT"


# ── Forward mig: backfill ────────────────────────────────────────────────────

def test_post_mig_packaging_short_backfilled_for_known_values(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    unmapped = tmp_db_conn.execute(
        "SELECT DISTINCT packaging_th FROM products "
        "WHERE packaging_th IS NOT NULL AND packaging_short IS NULL"
    ).fetchall()
    assert unmapped == [], (
        f"Backfill missed packaging_th values: {[r[0] for r in unmapped]} — "
        f"extend PACKAGING_SHORT mapping in mig 087"
    )


def test_post_mig_packaging_short_specific_mappings(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    expected = {
        "ตัว": "UN", "แผง": "PN", "ถุง": "BG",
        "ซอง": "SC", "แพ็ค": "PK", "โหล": "DZ",
        "แพ็คหัว": "HP", "แพ็คถุง": "PP",
        "แบบหลอด": "TB", "อัดแผง": "SP", "1กลมี60ใบ": "C60",
    }
    rows = tmp_db_conn.execute(
        "SELECT DISTINCT packaging_th, packaging_short FROM products "
        "WHERE packaging_th IS NOT NULL"
    ).fetchall()
    actual = {r[0]: r[1] for r in rows}
    for th, short in expected.items():
        if th in actual:
            assert actual[th] == short, f"{th!r} → expected {short!r}, got {actual[th]!r}"


# ── Forward mig: whitelist triggers ──────────────────────────────────────────

def test_post_mig_packaging_th_trigger_rejects_invalid_insert(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    tmp_db_conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db_conn.execute("INSERT INTO products (product_name, packaging_th) VALUES ('mig087-test', 'INVALID_VALUE')")


def test_post_mig_packaging_th_trigger_accepts_valid(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    tmp_db_conn.execute("PRAGMA foreign_keys = OFF")
    tmp_db_conn.execute("INSERT INTO products (product_name, packaging_th) VALUES ('mig087-test', 'ตัว')")


def test_post_mig_packaging_short_trigger_rejects_invalid(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    tmp_db_conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db_conn.execute("INSERT INTO products (product_name, packaging_short) VALUES ('mig087-test', 'XYZ')")


def test_post_mig_packaging_short_trigger_accepts_valid(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    tmp_db_conn.execute("PRAGMA foreign_keys = OFF")
    tmp_db_conn.execute("INSERT INTO products (product_name, packaging_short) VALUES ('mig087-test', 'PN')")


def test_post_mig_old_packaging_triggers_removed(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    trigs = _triggers(tmp_db_conn, "products")
    assert "products_packaging_check_insert" not in trigs
    assert "products_packaging_check_update" not in trigs
    assert "products_packaging_th_check_insert" in trigs
    assert "products_packaging_th_check_update" in trigs
    assert "products_packaging_short_check_insert" in trigs
    assert "products_packaging_short_check_update" in trigs


# ── Forward mig: VIEW + index ────────────────────────────────────────────────

def test_post_mig_view_exposes_packaging_th_and_short(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    cols = _view_columns(tmp_db_conn, "products_full")
    assert "packaging_th" in cols
    assert "packaging_short" in cols
    assert "packaging" not in cols
    # And the old material column is gone from the VIEW too (it was never there, sanity)
    assert "material" not in cols


def test_post_mig_view_returns_rows(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    row = tmp_db_conn.execute(
        "SELECT id, packaging_th, packaging_short FROM products_full LIMIT 1"
    ).fetchone()
    assert row is not None


def test_post_mig_index_renamed(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    idx = _indexes(tmp_db_conn, "products")
    assert "idx_products_packaging_th" in idx
    assert "idx_products_packaging" not in idx


# ── Forward mig: existing triggers preserved ─────────────────────────────────

def test_post_mig_audit_triggers_preserved(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    trigs = _triggers(tmp_db_conn, "products")
    for name in (
        "audit_products_insert", "audit_products_update", "audit_products_delete",
        "update_product_timestamp", "product_price_history_update",
    ):
        assert name in trigs, f"trigger {name!r} should be preserved across mig 087"


# ── Forward mig: data preservation ───────────────────────────────────────────

def test_post_mig_row_count_unchanged(tmp_db_conn):
    pre = tmp_db_conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    _apply_if_not_applied(tmp_db_conn)
    post = tmp_db_conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert post == pre


def test_post_mig_packaging_th_has_data(tmp_db_conn):
    """At least one product retains its packaging value as packaging_th."""
    _apply_if_not_applied(tmp_db_conn)
    row = tmp_db_conn.execute(
        "SELECT id, product_name, packaging_th FROM products "
        "WHERE packaging_th IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[2] in (
        "แผง", "ตัว", "ถุง", "แพ็คหัว", "แพ็คถุง",
        "ซอง", "อัดแผง", "แพ็ค", "แบบหลอด", "โหล", "1กลมี60ใบ"
    )


def test_post_mig_applied_migrations_records_087(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    row = tmp_db_conn.execute(
        "SELECT filename FROM applied_migrations WHERE filename = ?",
        ("087_drop_material_split_packaging.sql",)
    ).fetchone()
    assert row is not None


# ── Rollback ─────────────────────────────────────────────────────────────────

def test_rollback_restores_material_column(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    _apply(tmp_db_conn, ROLLBACK_087)
    cols = _columns(tmp_db_conn, "products")
    # Column exists again; data loss accepted (rollback can't recover dropped column data)
    assert "material" in cols


def test_rollback_renames_packaging_th_back_to_packaging(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    _apply(tmp_db_conn, ROLLBACK_087)
    cols = _columns(tmp_db_conn, "products")
    assert "packaging" in cols
    assert "packaging_th" not in cols


def test_rollback_drops_packaging_short_column(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    _apply(tmp_db_conn, ROLLBACK_087)
    cols = _columns(tmp_db_conn, "products")
    assert "packaging_short" not in cols


def test_rollback_preserves_post_mig_inserts(tmp_db_conn):
    """Per feedback_rename_migration_safety: rollback reads CURRENT table."""
    _apply_if_not_applied(tmp_db_conn)
    tmp_db_conn.execute("PRAGMA foreign_keys = OFF")
    tmp_db_conn.execute("INSERT INTO products (product_name, packaging_th, packaging_short) VALUES ('post_mig_insert', 'ตัว', 'UN')")
    tmp_db_conn.commit()
    _apply(tmp_db_conn, ROLLBACK_087)
    row = tmp_db_conn.execute(
        "SELECT id, packaging FROM products WHERE product_name = 'post_mig_insert'"
    ).fetchone()
    assert row is not None
    assert row[1] == "ตัว"  # packaging_th value carried into packaging column


def test_rollback_removes_applied_migrations_row(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    _apply(tmp_db_conn, ROLLBACK_087)
    row = tmp_db_conn.execute(
        "SELECT filename FROM applied_migrations WHERE filename = ?",
        ("087_drop_material_split_packaging.sql",)
    ).fetchone()
    assert row is None


def test_rollback_restores_old_packaging_check_triggers(tmp_db_conn):
    _apply_if_not_applied(tmp_db_conn)
    _apply(tmp_db_conn, ROLLBACK_087)
    trigs = _triggers(tmp_db_conn, "products")
    assert "products_packaging_check_insert" in trigs
    assert "products_packaging_check_update" in trigs
    # New ones gone
    assert "products_packaging_th_check_insert" not in trigs
    assert "products_packaging_short_check_insert" not in trigs
