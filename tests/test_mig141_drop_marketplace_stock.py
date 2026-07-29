"""TDD gate for migration 141 — drop products.shopee_stock / lazada_stock.

Background: projects/sendy-products-page-improve/plan.md, Phase 2b (Decision
#9, Put's explicit call — "full clean of the dead columns incl. migration").
Both columns were write-only from the start (the live UI numbers have always
come from platform_skus subqueries of the same alias name); PR #324 (P2a)
removed every remaining app write path. This migration is the pure schema
drop with nothing racing it.

Fixture pattern: `empty_db` clones the LIVE local DB schema, which already
has migration 141 applied on any machine where this PR's local verify step
has run (init_db() applies it on boot) — re-running the forward migration
there would still succeed structurally (its INSERT never references the
dropped columns), but to keep this deterministic regardless of the live DB's
current state, reconstruct the true pre-141 shape by running the rollback
first (mirrors `pre134_conn` in test_mig134_product_generic_standins.py).
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_141 = os.path.join(
    REPO, "data", "migrations", "141_drop_products_marketplace_stock.sql")
ROLLBACK_141 = os.path.join(
    REPO, "data", "migrations", "141_drop_products_marketplace_stock.rollback.sql")

EXPECTED_TRIGGERS = {
    "update_product_timestamp",
    "audit_products_insert",
    "audit_products_update",
    "audit_products_delete",
    "product_price_history_update",
    "products_packaging_th_check_insert",
    "products_packaging_th_check_update",
    "products_packaging_short_check_insert",
    "products_packaging_short_check_update",
}

EXPECTED_INDEXES = {
    "idx_products_brand",
    "idx_products_category",
    "idx_products_family",
    "idx_products_color_code",
    "idx_products_sub_category",
    "idx_products_sku_code",
    "idx_products_packaging_th",
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre141_conn(empty_db_conn):
    """empty_db_conn reconstructed to the guaranteed pre-141 shape (31 cols,
    shopee_stock/lazada_stock present) regardless of whether the live local
    DB this session already has migration 141 applied."""
    _apply(empty_db_conn, ROLLBACK_141)
    return empty_db_conn


def _seed_products(conn, n=3):
    ids = []
    for i in range(n):
        cur = conn.execute(
            "INSERT INTO products (product_name) VALUES (?)",
            (f"test product {i}",),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def _table_cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── forward migration ────────────────────────────────────────────────────────

def test_columns_dropped(pre141_conn):
    _seed_products(pre141_conn)
    _apply(pre141_conn, MIG_141)
    cols = _table_cols(pre141_conn, "products")
    assert "shopee_stock" not in cols
    assert "lazada_stock" not in cols
    assert len(cols) == 29


def test_products_full_view_valid_and_queryable_without_dropped_cols(pre141_conn):
    _seed_products(pre141_conn, n=2)
    _apply(pre141_conn, MIG_141)
    view_cols = _table_cols(pre141_conn, "products_full")
    assert "shopee_stock" not in view_cols
    assert "lazada_stock" not in view_cols
    rows = pre141_conn.execute("SELECT * FROM products_full").fetchall()
    assert len(rows) == 2


def test_all_9_triggers_present(pre141_conn):
    _apply(pre141_conn, MIG_141)
    names = {r["name"] for r in pre141_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='products'"
    )}
    assert names == EXPECTED_TRIGGERS


def test_no_trigger_references_dropped_columns(pre141_conn):
    _apply(pre141_conn, MIG_141)
    bad = pre141_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='products' "
        "AND (sql LIKE '%shopee_stock%' OR sql LIKE '%lazada_stock%')"
    ).fetchall()
    assert bad == []


def test_all_7_indexes_present(pre141_conn):
    # Scoped to tbl_name='products' — P1's idx_product_locations_pid and
    # idx_pcm_pid live on product_locations / product_code_mapping, a
    # different table, unaffected by this rebuild.
    _apply(pre141_conn, MIG_141)
    names = {r["name"] for r in pre141_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='products'"
    )}
    assert names == EXPECTED_INDEXES


def test_row_count_unchanged(pre141_conn):
    ids = _seed_products(pre141_conn, n=5)
    before = pre141_conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert before == len(ids)
    _apply(pre141_conn, MIG_141)
    after = pre141_conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert after == before


def test_integrity_and_fk_check_clean(pre141_conn):
    _seed_products(pre141_conn)
    _apply(pre141_conn, MIG_141)
    assert pre141_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert pre141_conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ── rollback ──────────────────────────────────────────────────────────────────

def test_rollback_restores_columns_zeroed(pre141_conn):
    ids = _seed_products(pre141_conn, n=2)
    _apply(pre141_conn, MIG_141)
    _apply(pre141_conn, ROLLBACK_141)

    cols = _table_cols(pre141_conn, "products")
    assert "shopee_stock" in cols
    assert "lazada_stock" in cols
    assert len(cols) == 31

    rows = pre141_conn.execute(
        "SELECT shopee_stock, lazada_stock FROM products WHERE id IN ({})".format(
            ",".join("?" * len(ids))
        ), ids
    ).fetchall()
    assert len(rows) == len(ids)
    for r in rows:
        assert r["shopee_stock"] == 0
        assert r["lazada_stock"] == 0


def test_rollback_preserves_rows_inserted_after_forward_migration(pre141_conn):
    """Per the rename/cosmetic-migration safety rule: rollback must
    INSERT...SELECT from the CURRENT table, not a pre-forward snapshot, so a
    row created after 141 applied still survives the rollback."""
    _seed_products(pre141_conn, n=1)
    _apply(pre141_conn, MIG_141)

    cur = pre141_conn.execute(
        "INSERT INTO products (product_name) VALUES ('created after 141')"
    )
    pre141_conn.commit()
    new_pid = cur.lastrowid

    _apply(pre141_conn, ROLLBACK_141)

    row = pre141_conn.execute(
        "SELECT product_name FROM products WHERE id = ?", (new_pid,)
    ).fetchone()
    assert row is not None
    assert row["product_name"] == "created after 141"
