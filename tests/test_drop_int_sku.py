"""
TDD gate for migration 097 — drop the legacy integer products.sku column and
move the whole app onto product_id.

The fixture below applies migration 097 to the test's temp DB if the live clone
still has the sku column, so these tests pass regardless of whether the live DB
has been migrated yet (order-independent within this PR).

Contract under test (Put, 2026-06-09):
  - products.sku (INT) is GONE; sku_code (TEXT) is KEPT.
  - product create / edit / CSV-import / marketplace-mapping / search all key on
    product_id, never sku.
"""
import glob
import os
import sqlite3

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _migration_sql():
    """Locate the 097 forward migration wherever it currently lives
    (data/migrations/ once finalized, or data/_staging_* during dev)."""
    for pat in ('data/migrations/097_drop_products_sku.sql',
                'data/_staging_097_drop_products_sku.sql'):
        hits = glob.glob(os.path.join(REPO_ROOT, pat))
        if hits:
            with open(hits[0], encoding='utf-8') as f:
                return f.read()
    raise FileNotFoundError("097 migration file not found")


@pytest.fixture
def migrated_db(tmp_db):
    """tmp_db, guaranteed to have migration 097 applied."""
    has_sku = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM pragma_table_info('products') WHERE name='sku'"
    ).fetchone()[0]
    if has_sku:
        conn = sqlite3.connect(tmp_db)
        conn.executescript(_migration_sql())
        conn.close()
    return tmp_db


# ── Schema ────────────────────────────────────────────────────────────────────

def test_products_has_no_sku_column(migrated_db):
    cols = [r[1] for r in sqlite3.connect(migrated_db).execute(
        "PRAGMA table_info(products)").fetchall()]
    assert 'sku' not in cols
    assert 'sku_code' in cols  # the useful one stays


def test_products_full_view_has_no_sku(migrated_db):
    cols = [r[1] for r in sqlite3.connect(migrated_db).execute(
        "PRAGMA table_info(products_full)").fetchall()]
    assert 'sku' not in cols


def test_legacy_archive_maps_id_to_sku(migrated_db):
    row = sqlite3.connect(migrated_db).execute(
        "SELECT COUNT(*) FROM legacy_product_sku_map").fetchone()
    assert row[0] > 0


def test_no_trigger_references_sku(migrated_db):
    bad = sqlite3.connect(migrated_db).execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='products' "
        "AND (sql LIKE '%NEW.sku%' OR sql LIKE '%OLD.sku%')").fetchall()
    assert bad == []


# ── models layer ──────────────────────────────────────────────────────────────

def test_create_product_without_sku(migrated_db):
    import models
    pid = models.create_product({
        'product_name': 'pytest no-sku', 'units_per_carton': 1, 'units_per_box': 1,
        'unit_type': 'ตัว', 'hard_to_sell': 0, 'cost_price': 0.0,
        'base_sell_price': 0.0, 'low_stock_threshold': 10,
        'shopee_stock': 0, 'lazada_stock': 0,
    })
    assert isinstance(pid, int) and pid > 0
    p = models.get_product(pid)
    assert p['product_name'] == 'pytest no-sku'


def test_update_product_without_sku(migrated_db):
    import models
    pid = sqlite3.connect(migrated_db).execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    models.update_product(pid, {
        'product_name': 'pytest renamed', 'units_per_carton': 1, 'units_per_box': 1,
        'unit_type': 'ตัว', 'hard_to_sell': 0, 'cost_price': 1.0,
        'base_sell_price': 2.0, 'low_stock_threshold': 5,
        'shopee_stock': 0, 'lazada_stock': 0,
    })
    assert models.get_product(pid)['product_name'] == 'pytest renamed'


def test_bulk_import_inserts_new_when_product_id_blank(migrated_db):
    import models
    before = sqlite3.connect(migrated_db).execute(
        "SELECT COUNT(*) FROM products").fetchone()[0]
    imported, skipped = models.bulk_import_products([{
        'product_id': None, 'product_name': 'bulk-new-row',
        'units_per_carton': 1, 'units_per_box': 1,
        'unit_type': 'ตัว', 'hard_to_sell': 0,
    }], overwrite=False)
    after = sqlite3.connect(migrated_db).execute(
        "SELECT COUNT(*) FROM products").fetchone()[0]
    assert imported == 1
    assert after == before + 1


def test_bulk_import_updates_existing_by_product_id(migrated_db):
    import models
    pid = sqlite3.connect(migrated_db).execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    models.bulk_import_products([{
        'product_id': pid, 'product_name': 'bulk-overwritten-name',
        'units_per_carton': 3, 'units_per_box': 6,
        'unit_type': 'ตัว', 'hard_to_sell': 0,
    }], overwrite=True)
    assert models.get_product(pid)['product_name'] == 'bulk-overwritten-name'


# ── marketplace mapping (money path) ─────────────────────────────────────────

def test_apply_platform_mapping_resolves_by_product_id(migrated_db):
    import models
    conn = sqlite3.connect(migrated_db)
    conn.row_factory = sqlite3.Row
    pid = conn.execute("SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    ps_id = conn.execute("SELECT id FROM platform_skus LIMIT 1").fetchone()
    if ps_id is None:
        pytest.skip("no platform_skus rows to map")
    ps_id = ps_id[0]
    conn.close()

    updated, not_found = models.apply_platform_mapping([
        {'platform_sku_id': ps_id, 'product_id': pid, 'qty_per_sale': 1}
    ])
    assert updated == 1 and not_found == 0
    got = sqlite3.connect(migrated_db).execute(
        "SELECT internal_product_id FROM platform_skus WHERE id=?", (ps_id,)).fetchone()[0]
    assert got == pid


def test_apply_platform_mapping_unknown_product_id_not_found(migrated_db):
    import models
    conn = sqlite3.connect(migrated_db)
    ps = conn.execute("SELECT id FROM platform_skus LIMIT 1").fetchone()
    if ps is None:
        pytest.skip("no platform_skus rows")
    ps_id = ps[0]
    conn.close()
    updated, not_found = models.apply_platform_mapping([
        {'platform_sku_id': ps_id, 'product_id': 999999999, 'qty_per_sale': 1}
    ])
    assert not_found == 1


def test_apply_listing_mapping_resolves_by_product_id(migrated_db):
    import models
    conn = sqlite3.connect(migrated_db)
    pid = conn.execute("SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    lst = conn.execute("SELECT id FROM ecommerce_listings WHERE is_ignored=0 LIMIT 1").fetchone()
    if lst is None:
        pytest.skip("no ecommerce_listings rows")
    lid = lst[0]
    conn.close()
    updated, not_found = models.apply_listing_mapping([
        {'listing_id': lid, 'product_id': pid, 'qty_per_sale': 1.0}
    ])
    assert updated == 1 and not_found == 0
    got = sqlite3.connect(migrated_db).execute(
        "SELECT product_id FROM ecommerce_listings WHERE id=?", (lid,)).fetchone()[0]
    assert got == pid


# ── search by product_id ──────────────────────────────────────────────────────

@pytest.fixture
def admin_client(migrated_db):
    """Test client (admin session) on the migrated temp DB. migrated_db pulls
    in tmp_db first, so config.DATABASE_PATH is patched before app import."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def test_api_products_search_matches_product_id(migrated_db, admin_client):
    pid = sqlite3.connect(migrated_db).execute(
        "SELECT id FROM products WHERE is_active=1 LIMIT 1").fetchone()[0]
    resp = admin_client.get(f'/api/products/search?q={pid}')
    assert resp.status_code == 200
    items = resp.get_json()['items']
    assert any(it['id'] == pid for it in items)
    # the int sku must not leak back into the payload
    assert all('sku' not in it for it in items)
