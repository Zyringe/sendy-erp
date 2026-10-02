def test_new_typed_brand_preview_matches_canonical_creation(empty_db_conn):
    from models.products import create_structured_product, get_product
    from sku_code_utils import preview_sku_code

    conn = empty_db_conn
    category_id = conn.execute(
        "INSERT INTO categories (code, name_th, short_code, sort_order) "
        "VALUES ('chemical', 'สารเคมี', 'CHM', 100)"
    ).lastrowid
    conn.commit()
    fields = {
        'product_name': 'น้ำยา SONAX 500ml',
        'category_id': category_id,
        'sub_category_short_code': 'LIQ',
        'size': '500ml',
        'unit_type': 'ตัว',
        'brand_other_name': 'SONAX',
        'brand_other_short_code': ' sonax ',
    }

    changes_before = conn.total_changes
    preview = preview_sku_code(conn, fields)
    assert conn.total_changes == changes_before

    product_id = create_structured_product(fields, 'manual')
    product = get_product(product_id)
    assert product is not None
    assert product['sku_code'] == 'CHM-LIQ-SONAX-500ml'
    assert preview == 'CHM-LIQ-SONAX-500ml'
