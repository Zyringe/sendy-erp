"""Migration 140 — unlock 'tiktok' in the platform CHECK constraints.

Table-rebuild (SQLite can't ALTER a CHECK in place) on `platform_skus`,
`platform_products`, `ecommerce_listings`: widen
CHECK(platform IN ('shopee','lazada')) -> CHECK(platform IN
('shopee','lazada','tiktok')). `marketplace_orders` is explicitly OUT OF
SCOPE (separate sales-booking arc, not touched by this migration).

Must survive the rebuild:
  - the `platform_skus_price_history_update` AFTER UPDATE trigger (auto-drops
    with its table, must be recreated verbatim)
  - `idx_platform_products_parent_sku` and `idx_el_platform` (auto-drop with
    their table)
  - `listing_bundles.listing_id REFERENCES ecommerce_listings(id)` — the only
    FK dependent on any of the 3 rebuilt tables. IDs are preserved by the
    explicit-column INSERT..SELECT, so existing listing_bundles rows must
    still resolve after the rebuild.

Tests run against `pre140_conn` (empty_db_conn + a rollback-first reset, see
that fixture below) so they hold regardless of whether THIS repo's local live
DB has migration 140 applied yet — mirrors the mig134 `pre134_conn` pattern.
"""
import os
import sqlite3

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_140 = os.path.join(REPO, "data", "migrations", "140_platform_check_tiktok.sql")
ROLLBACK_140 = os.path.join(
    REPO, "data", "migrations", "140_platform_check_tiktok.rollback.sql")


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


@pytest.fixture
def pre140_conn(empty_db_conn):
    """empty_db clones the LIVE local DB schema (zero rows). Once this PR's
    own verification step applies migration 140 locally (sendy-up runs
    run_pending_migrations), the clone starts carrying the 3-value CHECK
    already — reconstruct the definite pre-140 state by running the rollback
    first, exactly like `pre134_conn` in test_mig134_product_generic_standins.
    Safe regardless of the live DB's actual state: the rollback rebuilds
    fresh with the 2-value CHECK either way, and the fixture carries no rows."""
    _apply(empty_db_conn, ROLLBACK_140)
    empty_db_conn.commit()
    return empty_db_conn


def _insert_sku(conn, platform, variation_id, internal_product_id=None):
    conn.execute(
        """INSERT INTO platform_skus
               (platform, variation_id, product_name, price, stock, internal_product_id)
           VALUES (?,?,?,?,?,?)""",
        (platform, variation_id, f'test {platform}', 100, 5, internal_product_id),
    )


def _insert_product(conn, platform, product_id_str):
    conn.execute(
        "INSERT INTO platform_products (platform, product_id_str, product_name) "
        "VALUES (?,?,?)",
        (platform, product_id_str, f'test {platform}'),
    )


def _insert_listing(conn, platform, listing_key, product_id=None):
    cur = conn.execute(
        "INSERT INTO ecommerce_listings (platform, item_name, listing_key, product_id) "
        "VALUES (?,?,?,?)",
        (platform, f'test {platform}', listing_key, product_id),
    )
    return cur.lastrowid


def test_tiktok_insert_rejected_before_migration(pre140_conn):
    """Pin the pre-migration state: confirms the fixture is genuinely pre-140
    (guards against this test suite silently no-op'ing if the live DB is ever
    upgraded ahead of this file)."""
    conn = pre140_conn
    with pytest.raises(sqlite3.IntegrityError):
        _insert_sku(conn, 'tiktok', 'T1')


def test_tiktok_insert_succeeds_on_all_three_tables_after_migration(pre140_conn):
    conn = pre140_conn
    _apply(conn, MIG_140)
    conn.commit()

    _insert_sku(conn, 'tiktok', 'T1')
    _insert_product(conn, 'tiktok', 'TP1')
    _insert_listing(conn, 'tiktok', 'tiktok:T1')
    conn.commit()  # raises if any CHECK still rejects tiktok

    assert conn.execute(
        "SELECT platform FROM platform_skus WHERE variation_id='T1'"
    ).fetchone()['platform'] == 'tiktok'
    assert conn.execute(
        "SELECT platform FROM platform_products WHERE product_id_str='TP1'"
    ).fetchone()['platform'] == 'tiktok'
    assert conn.execute(
        "SELECT platform FROM ecommerce_listings WHERE listing_key='tiktok:T1'"
    ).fetchone()['platform'] == 'tiktok'


def test_invalid_platform_still_rejected_after_migration(pre140_conn):
    conn = pre140_conn
    _apply(conn, MIG_140)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_sku(conn, 'ebay', 'E1')
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product(conn, 'ebay', 'EP1')
    with pytest.raises(sqlite3.IntegrityError):
        _insert_listing(conn, 'ebay', 'ebay:E1')


def test_marketplace_orders_check_untouched(pre140_conn):
    """Out-of-scope guard: marketplace_orders.platform stays shopee/lazada-only."""
    conn = pre140_conn
    _apply(conn, MIG_140)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO marketplace_orders (platform, order_sn) VALUES ('tiktok','O1')"
        )


def test_existing_shopee_lazada_rows_preserved(pre140_conn):
    conn = pre140_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (9001, 'mapped')")
    _insert_sku(conn, 'shopee', 'S1', internal_product_id=9001)
    _insert_sku(conn, 'lazada', 'L1')
    _insert_product(conn, 'shopee', 'SP1')
    listing_id = _insert_listing(conn, 'shopee', 'shopee:S1', product_id=9001)
    conn.commit()

    _apply(conn, MIG_140)
    conn.commit()

    sku = conn.execute(
        "SELECT * FROM platform_skus WHERE variation_id='S1'"
    ).fetchone()
    assert sku['platform'] == 'shopee'
    assert sku['internal_product_id'] == 9001
    assert conn.execute(
        "SELECT * FROM platform_skus WHERE variation_id='L1'"
    ).fetchone()['platform'] == 'lazada'
    assert conn.execute(
        "SELECT * FROM platform_products WHERE product_id_str='SP1'"
    ).fetchone()['platform'] == 'shopee'
    listing = conn.execute(
        "SELECT * FROM ecommerce_listings WHERE listing_key='shopee:S1'"
    ).fetchone()
    assert listing['id'] == listing_id       # id preserved by explicit-column rebuild
    assert listing['product_id'] == 9001


def test_price_history_trigger_still_fires_after_migration(pre140_conn):
    conn = pre140_conn
    _apply(conn, MIG_140)
    conn.commit()

    conn.execute(
        """INSERT INTO platform_skus (platform, variation_id, product_name, price, stock)
           VALUES ('tiktok','T2','test',100,5)"""
    )
    conn.commit()
    conn.execute("UPDATE platform_skus SET price=150 WHERE variation_id='T2'")
    conn.commit()

    rows = conn.execute(
        "SELECT platform, variation_id, field_name, old_value, new_value "
        "FROM platform_price_history WHERE variation_id='T2'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]['field_name'] == 'price'
    assert rows[0]['old_value'] == 100
    assert rows[0]['new_value'] == 150
    assert rows[0]['platform'] == 'tiktok'


def test_indexes_recreated_after_migration(empty_db_conn):
    conn = empty_db_conn
    _apply(conn, MIG_140)
    conn.commit()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert 'idx_platform_products_parent_sku' in names
    assert 'idx_el_platform' in names


def test_listing_bundles_fk_survives_ecommerce_listings_rebuild(empty_db_conn):
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (9002, 'component')")
    listing_id = _insert_listing(conn, 'shopee', 'shopee:bundle1')
    conn.execute(
        """INSERT INTO listing_bundles (listing_id, component_product_id, qty_per_sale)
           VALUES (?, 9002, 2)""",
        (listing_id,),
    )
    conn.commit()

    _apply(conn, MIG_140)
    conn.commit()

    bundle = conn.execute(
        "SELECT listing_id, component_product_id FROM listing_bundles"
    ).fetchone()
    assert bundle['listing_id'] == listing_id
    # listing_id must still resolve to a real row post-rebuild
    assert conn.execute(
        "SELECT 1 FROM ecommerce_listings WHERE id=?", (bundle['listing_id'],)
    ).fetchone() is not None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rollback_restores_two_value_check_when_no_tiktok_data(empty_db_conn):
    conn = empty_db_conn
    _insert_sku(conn, 'shopee', 'S9')
    conn.commit()

    _apply(conn, MIG_140)
    conn.commit()
    _apply(conn, ROLLBACK_140)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        _insert_sku(conn, 'tiktok', 'T9')
    # pre-existing data survives the round trip
    assert conn.execute(
        "SELECT platform FROM platform_skus WHERE variation_id='S9'"
    ).fetchone()['platform'] == 'shopee'


def test_rollback_fails_loudly_if_tiktok_rows_exist(empty_db_conn):
    """Documented, accepted limitation (plan.md Phase 1): rollback is a
    forensic path only once real tiktok data has landed."""
    conn = empty_db_conn
    _apply(conn, MIG_140)
    conn.commit()
    _insert_sku(conn, 'tiktok', 'T10')
    conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        _apply(conn, ROLLBACK_140)
