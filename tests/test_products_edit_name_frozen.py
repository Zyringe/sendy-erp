"""Phase 2 deprecation: the old product edit form (/products/<id>/edit) no
longer renames products — naming is owned by /naming. The name field is frozen
(POST ignores it) while every non-naming field (price, stock, units) still saves.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(empty_db):
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    pid = conn.execute(
        "INSERT INTO products(product_name, base_sell_price, units_per_carton, "
        "                     units_per_box, sku_code) "
        "VALUES ('ชื่อเดิม', 10, 1, 1, 'SK1')"
    ).lastrowid
    conn.commit()
    conn.close()
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c, empty_db, pid


def test_old_edit_ignores_rename_but_saves_price(admin_client):
    c, db, pid = admin_client
    c.post(f'/products/{pid}/edit', data={
        'product_name': 'แก้ชื่อมั่ว',         # should be ignored
        'units_per_carton': '1', 'units_per_box': '1', 'unit_type': 'ตัว',
        'base_sell_price': '99', 'cost_price': '0',
        'low_stock_threshold': '10', 'shopee_stock': '0', 'lazada_stock': '0',
    })
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT product_name, base_sell_price FROM products WHERE id=?",
                       (pid,)).fetchone()
    conn.close()
    assert row['product_name'] == 'ชื่อเดิม'   # rename ignored — name is frozen
    assert row['base_sell_price'] == 99        # non-naming field still saved


def test_old_edit_form_renders_name_readonly_with_link(admin_client):
    c, _, pid = admin_client
    body = c.get(f'/products/{pid}/edit').get_data(as_text=True)
    assert 'naming.index' in body or '/naming' in body   # link to the naming page
    # the product_name input is rendered read-only (not freely editable)
    assert 'readonly' in body.lower()
