"""platform_skus.is_ignored (mig 136) — ignored SKU rows leave every display
surface (tab list, summary, mapping export, fuzzy-suggest) but stay in the
table as an audit trail, and the import upsert never resurrects them."""
import sqlite3

import pytest


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, product_name,"
        " variation_name, price, stock, is_ignored)"
        " VALUES ('shopee','v-live','สินค้าปกติ','ตัวเลือก A',100,5,0)"
    )
    conn.execute(
        "INSERT INTO platform_skus (platform, variation_id, product_name,"
        " variation_name, price, stock, is_ignored)"
        " VALUES ('shopee','v-ign','สินค้าที่ปิดแล้ว','ตัวเลือก B',50,3,1)"
    )
    conn.commit()
    conn.close()


def test_tab_list_excludes_ignored(empty_db):
    _seed(empty_db)
    import models
    rows, total = models.get_platform_skus('shopee')
    assert total == 1
    assert [r['variation_id'] for r in rows] == ['v-live']
    assert [r['variation_id'] for r in models.get_platform_skus_all('shopee')] == ['v-live']


def test_summary_excludes_ignored(empty_db):
    _seed(empty_db)
    import models
    summary = models.get_platform_summary()
    assert summary['shopee']['sku_count'] == 1
    assert summary['shopee']['total_stock'] == 5


def test_mapping_export_excludes_ignored(empty_db):
    _seed(empty_db)
    import models
    keys = [r['variation_id'] for r in models.get_platform_mapping_data()]
    assert 'v-live' in keys and 'v-ign' not in keys


def test_import_upsert_preserves_is_ignored(empty_db):
    _seed(empty_db)
    import models
    # Re-import of the same variation (as from a fresh Seller Center file)
    # must NOT clear the ignore flag — safe-upsert contract.
    count, _ = models.import_platform_skus('shopee', [{
        'product_id_str': '999', 'product_name': 'สินค้าที่ปิดแล้ว',
        'variation_id': 'v-ign', 'variation_name': 'ตัวเลือก B',
        'parent_sku': None, 'seller_sku': None,
        'price': 55.0, 'special_price': None, 'stock': 9, 'raw_json': '{}',
    }])
    assert count == 1
    conn = sqlite3.connect(empty_db)
    ign, price = conn.execute(
        "SELECT is_ignored, price FROM platform_skus WHERE variation_id='v-ign'"
    ).fetchone()
    assert ign == 1          # flag survives the upsert
    assert price == 55.0     # while normal columns still update
