"""TDD for models.create_structured_product — the canonical product-creation
path (Phase 3 of the product-creation-consolidation plan).

Both real create entry points (`product_new` hand form, `approve_pending_suggestion`
Smart Suggest) route through this single function. Covers:
  - structured create with no explicit name -> derived product_name + real sku_code
  - explicit product_name override -> kept verbatim, sku_code still generated
  - sku_code collision -> second identical-spec product gets -<id> suffix
  - inline "other" brand/color -> new FK rows created + linked
  - invalid packaging_th -> CHECK trigger raises, no orphan product/stock_levels row
  - created_via stamped correctly for both 'manual' and 'smart_mapping'
"""
import sqlite3

import pytest


def _row(db_path, pid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row


def _seed_category(db_path, code='hinge', name_th='บานพับ', short_code='HG'):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO categories (code, name_th, sort_order, short_code) VALUES (?, ?, 100, ?)",
        (code, name_th, short_code),
    )
    cid = conn.execute("SELECT id FROM categories WHERE code=?", (code,)).fetchone()[0]
    conn.commit()
    conn.close()
    return cid


def _seed_brand(db_path, code='SENDAI', name='Sendai', short_code='SD'):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO brands (code, name, name_th, is_own_brand, sort_order, short_code) "
        "VALUES (?, ?, ?, 1, 100, ?)",
        (code, name, name, short_code),
    )
    bid = conn.execute("SELECT id FROM brands WHERE code=?", (code,)).fetchone()[0]
    conn.commit()
    conn.close()
    return bid


def test_structured_create_derives_name_and_generates_sku(empty_db):
    import models
    cat_id = _seed_category(empty_db)
    brand_id = _seed_brand(empty_db)

    pid = models.create_structured_product({
        'brand_id': brand_id,
        'category_id': cat_id,
        'sub_category': 'บานพับ',
        'model': 'A1',
        'size': '3นิ้ว',
        'unit_type': 'ตัว',
        'cost_price': 12.5,
    }, 'manual')

    row = _row(empty_db, pid)
    assert row['product_name']  # non-empty, derived
    assert row['sku_code'] is not None
    assert not row['sku_code'].startswith('INT-')
    assert row['created_via'] == 'manual'
    assert row['opening_cost'] == 12.5
    assert row['cost_price'] == 12.5

    conn = sqlite3.connect(empty_db)
    stock = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()
    conn.close()
    assert stock is not None
    assert stock[0] == 0


def test_explicit_product_name_override_is_kept(empty_db):
    import models
    pid = models.create_structured_product({
        'product_name': 'ชื่อที่พิมพ์เอง',
        'unit_type': 'ตัว',
    }, 'manual')

    row = _row(empty_db, pid)
    assert row['product_name'] == 'ชื่อที่พิมพ์เอง'
    # still gets a sku_code (fallback INT-<id> since no spec fields given)
    assert row['sku_code'] == f'INT-{pid}'


def test_identical_spec_products_get_collision_suffixed_sku(empty_db):
    import models
    cat_id = _seed_category(empty_db)
    brand_id = _seed_brand(empty_db)
    fields = {
        'brand_id': brand_id,
        'category_id': cat_id,
        'model': 'DUP1',
        'unit_type': 'ตัว',
    }
    pid1 = models.create_structured_product(dict(fields), 'manual')
    pid2 = models.create_structured_product(dict(fields), 'manual')

    row1 = _row(empty_db, pid1)
    row2 = _row(empty_db, pid2)
    assert row1['sku_code'] is not None
    assert row2['sku_code'] == f"{row1['sku_code']}-{pid2}"


def test_inline_new_brand_creates_and_links_brand_row(empty_db):
    import models
    pid = models.create_structured_product({
        'brand_other_name': 'แบรนด์ใหม่ทดสอบ',
        'unit_type': 'ตัว',
    }, 'manual')

    row = _row(empty_db, pid)
    assert row['brand_id'] is not None

    conn = sqlite3.connect(empty_db)
    brand = conn.execute(
        "SELECT name FROM brands WHERE id=?", (row['brand_id'],)
    ).fetchone()
    conn.close()
    assert brand[0] == 'แบรนด์ใหม่ทดสอบ'


def test_inline_new_color_creates_and_links_color_row(empty_db):
    import models
    pid = models.create_structured_product({
        'color_code_other': 'ZZ',
        'color_th': 'สีทดสอบ',
        'unit_type': 'ตัว',
    }, 'manual')

    row = _row(empty_db, pid)
    assert row['color_code'] == 'ZZ'

    conn = sqlite3.connect(empty_db)
    color = conn.execute(
        "SELECT name_th FROM color_finish_codes WHERE code=?", (row['color_code'],)
    ).fetchone()
    conn.close()
    assert color[0] == 'สีทดสอบ'


def test_invalid_packaging_rolls_back_no_orphan_rows(empty_db):
    import models
    conn = sqlite3.connect(empty_db)
    before_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    before_stock = conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0]
    conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        models.create_structured_product({
            'packaging_th': 'ไม่มีจริง',
            'unit_type': 'ตัว',
        }, 'manual')

    conn = sqlite3.connect(empty_db)
    after_products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    after_stock = conn.execute("SELECT COUNT(*) FROM stock_levels").fetchone()[0]
    conn.close()
    assert after_products == before_products
    assert after_stock == before_stock


def test_created_via_manual_and_smart_mapping_stamped(empty_db):
    import models
    pid_manual = models.create_structured_product({'unit_type': 'ตัว'}, 'manual')
    pid_smart = models.create_structured_product({'unit_type': 'ตัว'}, 'smart_mapping')

    assert _row(empty_db, pid_manual)['created_via'] == 'manual'
    assert _row(empty_db, pid_smart)['created_via'] == 'smart_mapping'


def test_category_free_text_resolves_to_id(empty_db):
    import models
    cat_id = _seed_category(empty_db, code='hinge', name_th='บานพับ')

    pid = models.create_structured_product({
        'category': 'บานพับ',
        'unit_type': 'ตัว',
    }, 'manual')

    row = _row(empty_db, pid)
    assert row['category_id'] == cat_id


def test_units_per_carton_box_default_to_one_when_absent(empty_db):
    import models
    pid = models.create_structured_product({'unit_type': 'ตัว'}, 'manual')
    row = _row(empty_db, pid)
    assert row['units_per_carton'] == 1
    assert row['units_per_box'] == 1
