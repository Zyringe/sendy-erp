import os
from pathlib import Path
import subprocess
import sys

import pytest


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


@pytest.fixture
def typed_identity(empty_db_conn):
    conn = empty_db_conn
    category_id = conn.execute(
        "INSERT INTO categories (code, name_th, short_code, sort_order) "
        "VALUES ('chemical', 'สารเคมี', 'CHM', 100)"
    ).lastrowid
    conn.commit()
    return conn, {
        'product_name': 'น้ำยา SONAX 500ml',
        'category_id': category_id,
        'sub_category_short_code': 'LIQ',
        'size': '500ml',
        'unit_type': 'ตัว',
        'brand_other_name': 'SONAX',
        'brand_other_short_code': ' sonax ',
    }


def _preview_without_writes(conn, fields):
    from sku_code_utils import preview_sku_code

    changes = conn.total_changes
    data_version = conn.execute('PRAGMA data_version').fetchone()[0]
    original_fields = dict(fields)
    preview = preview_sku_code(conn, fields)
    assert conn.total_changes == changes
    assert conn.execute('PRAGMA data_version').fetchone()[0] == data_version
    assert fields == original_fields
    return preview


@pytest.mark.parametrize('stored_code,expected', [
    ('sNx', 'CHM-LIQ-sNx-500ml'),
    (None, 'CHM-LIQ-500ml'),
])
def test_existing_typed_brand_keeps_stored_code(typed_identity, stored_code, expected):
    from models.products import create_structured_product, get_product

    conn, fields = typed_identity
    brand_id = conn.execute(
        "INSERT INTO brands (code, name, short_code) VALUES ('sonax', ' SONAX ', ?)",
        (stored_code,),
    ).lastrowid
    conn.commit()
    fields.update(brand_other_name='  sonax  ', brand_other_short_code='WRONG',
                  brand_short_code='STALE')
    preview = _preview_without_writes(conn, fields)
    product = get_product(create_structured_product(fields, 'manual'))
    assert product is not None
    assert product['brand_id'] == brand_id
    assert product['sku_code'] == expected
    assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 1
    assert conn.execute('SELECT short_code FROM brands WHERE id=?',
                        (brand_id,)).fetchone()[0] == stored_code
    assert preview == expected


def test_oldest_typed_brand_wins_with_descending_name_index(typed_identity):
    from models.products import create_structured_product, get_product

    conn, fields = typed_identity
    oldest_id = conn.execute(
        "INSERT INTO brands (code, name, short_code) VALUES ('sonax', 'SONAX', 'OLD')"
    ).lastrowid
    conn.execute(
        "INSERT INTO brands (code, name, short_code) VALUES ('sonax_2', ' sonax ', 'NEW')"
    )
    conn.execute(
        'CREATE INDEX typed_brand_name_desc ON brands (lower(trim(name)), id DESC)'
    )
    conn.commit()
    preview = _preview_without_writes(conn, fields)
    product = get_product(create_structured_product(fields, 'manual'))
    assert product is not None
    assert product['brand_id'] == oldest_id
    assert product['sku_code'] == 'CHM-LIQ-OLD-500ml'
    assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 2
    assert preview == 'CHM-LIQ-OLD-500ml'


@pytest.mark.parametrize('short_code', [None, '', '   '])
def test_new_typed_brand_without_code_overrides_stale_segment(typed_identity, short_code):
    from models.products import create_structured_product, get_product

    conn, fields = typed_identity
    fields.pop('brand_other_short_code')
    if short_code is not None:
        fields['brand_other_short_code'] = short_code
    fields['brand_short_code'] = 'STALE'
    preview = _preview_without_writes(conn, fields)
    product = get_product(create_structured_product(fields, 'manual'))
    assert product is not None
    assert product['sku_code'] == 'CHM-LIQ-500ml'
    brands = conn.execute('SELECT id, short_code FROM brands').fetchall()
    assert len(brands) == 1
    assert product['brand_id'] == brands[0]['id']
    assert brands[0]['short_code'] is None
    assert preview == 'CHM-LIQ-500ml'


@pytest.mark.parametrize('name', [None, '', '   '])
def test_blank_typed_brand_does_not_create_brand_or_segment(typed_identity, name):
    from models.products import create_structured_product, get_product

    conn, fields = typed_identity
    fields['brand_other_name'] = name
    preview = _preview_without_writes(conn, fields)
    product = get_product(create_structured_product(fields, 'manual'))
    assert product is not None
    assert product['brand_id'] is None
    assert product['sku_code'] == 'CHM-LIQ-500ml'
    assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 0
    assert preview == 'CHM-LIQ-500ml'


@pytest.mark.parametrize('stored_code,expected', [
    ('FK', 'CHM-LIQ-FK-500ml'),
    (None, 'CHM-LIQ-500ml'),
])
def test_brand_fk_wins_over_typed_brand(typed_identity, stored_code, expected):
    from models.products import create_structured_product, get_product

    conn, fields = typed_identity
    brand_id = conn.execute(
        "INSERT INTO brands (code, name, short_code) VALUES ('kept', 'Kept', ?)",
        (stored_code,),
    ).lastrowid
    conn.commit()
    fields.update(brand_id=brand_id, brand_short_code='STALE')
    preview = _preview_without_writes(conn, fields)
    product = get_product(create_structured_product(fields, 'manual'))
    assert product is not None
    assert product['brand_id'] == brand_id
    assert product['sku_code'] == expected
    assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 1
    assert preview == expected


def test_missing_brand_fk_preview_does_not_resolve_typed_brand(typed_identity):
    conn, fields = typed_identity
    fields.update(brand_id=999999, brand_short_code='STALE')
    assert _preview_without_writes(conn, fields) == 'CHM-LIQ-500ml'
    assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 0


def test_typed_brand_http_preview_keeps_name_and_writes_nothing(typed_identity, monkeypatch):
    conn, fields = typed_identity
    fields['sub_category'] = 'น้ำยา'
    monkeypatch.setenv('SKIP_DB_INIT', '1')
    from app import app

    app.config['TESTING'] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=1, username='test-staff', role='staff')
    response = client.post('/products/preview-identity', json=fields)
    assert response.status_code == 200
    assert response.get_json() == {
        'name': 'น้ำยา 500ml', 'sku_code': 'CHM-LIQ-SONAX-500ml',
    }
    for table in ('brands', 'products', 'pending_product_suggestions', 'audit_log'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('order', ['utility_only', 'utility_first', 'models_first', 'deferred_first'])
def test_brand_preview_import_orders_without_app_or_implicit_connection(tmp_path, order):
    app_path = Path(__file__).resolve().parents[1] / 'inventory_app'
    script = """
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
conn.execute('CREATE TABLE brands (id INTEGER PRIMARY KEY, name TEXT, short_code TEXT)')

def unexpected_connection(*args, **kwargs):
    raise AssertionError('import or preview opened an unexpected database connection')

sqlite3.connect = unexpected_connection
order = sys.argv[2]
if order == 'models_first':
    import models.brands
import sku_code_utils
if order != 'models_first':
    assert 'models' not in sys.modules
if order != 'utility_only':
    if order != 'deferred_first':
        import models.brands
    assert sku_code_utils.preview_sku_code(conn, {
        'brand_other_name': 'SONAX', 'brand_other_short_code': ' sonax ',
    }) == 'SONAX'
    assert 'models.brands' in sys.modules
assert 'app' not in sys.modules
assert 'inventory_app.app' not in sys.modules
assert conn.execute('SELECT COUNT(*) FROM brands').fetchone()[0] == 0
conn.close()
"""
    env = dict(os.environ, DATA_DIR=str(tmp_path / 'data'), SKIP_DB_INIT='0')
    result = subprocess.run(
        [sys.executable, '-c', script, str(app_path), order],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []
