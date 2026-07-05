"""Regression: the product edit form silently zeroed hard_to_sell, shopee_stock,
and lazada_stock on EVERY save.

templates/products/form.html's edit block never renders those three fields (they
only exist on the `new` branch), but blueprints/products.py::product_edit built
its `data` dict with all three defaulting to 0/False when absent from the POST
body, and models.update_product did a FIXED full-column UPDATE — so every real
edit clobbered them.

Fix: models.update_product now builds its SET clause only from whitelisted keys
actually present in `data`, and product_edit stops including the three fields
it can't actually collect. Money/schema-adjacent write path → TDD (project rule).
"""
import sqlite3

import pytest


def _seed_product(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    pid = conn.execute(
        "INSERT INTO products(product_name, units_per_carton, units_per_box, "
        "                     unit_type, hard_to_sell, cost_price, base_sell_price, "
        "                     low_stock_threshold, sku_code) "
        "VALUES ('สินค้าทดสอบแก้ไข', 1, 1, 'ตัว', 1, 10.0, 20.0, 10, 'SK-EDIT-TEST')"
    ).lastrowid
    conn.execute(
        "UPDATE products SET shopee_stock = 50, lazada_stock = 30 WHERE id = ?",
        (pid,),
    )
    conn.commit()
    conn.close()
    return pid


# ── unit: models.update_product only touches whitelisted keys present in data ─

def test_update_product_preserves_omitted_columns(empty_db):
    import models

    pid = _seed_product(empty_db)

    # Mirrors what product_edit now sends: the fields the edit form actually
    # renders, WITHOUT hard_to_sell / shopee_stock / lazada_stock.
    models.update_product(pid, {
        'product_name': 'สินค้าทดสอบแก้ไข',
        'units_per_carton': 1,
        'units_per_box': 1,
        'unit_type': 'ตัว',
        'cost_price': 15.0,
        'base_sell_price': 25.0,
        'low_stock_threshold': 10,
    }, source='manual:tester')

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT hard_to_sell, shopee_stock, lazada_stock, cost_price, base_sell_price "
        "FROM products WHERE id = ?", (pid,)
    ).fetchone()
    hist = conn.execute(
        "SELECT source FROM product_price_history "
        "WHERE product_id = ? AND field_name = 'base_sell_price' "
        "ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone()
    conn.close()

    # updated
    assert row['cost_price'] == 15.0
    assert row['base_sell_price'] == 25.0
    # preserved — NOT zeroed by the omitted keys
    assert row['hard_to_sell'] == 1
    assert row['shopee_stock'] == 50
    assert row['lazada_stock'] == 30
    # the price-history trigger still fires and still gets source-stamped
    assert hist is not None
    assert hist['source'] == 'manual:tester'


def test_update_product_no_whitelisted_keys_is_noop(empty_db):
    """Guard: an empty/irrelevant data dict must not raise or touch the row."""
    import models

    pid = _seed_product(empty_db)
    models.update_product(pid, {'not_a_column': 'x'})

    conn = sqlite3.connect(empty_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT hard_to_sell, shopee_stock, lazada_stock, cost_price, base_sell_price "
        "FROM products WHERE id = ?", (pid,)
    ).fetchone()
    conn.close()
    assert row['hard_to_sell'] == 1
    assert row['shopee_stock'] == 50
    assert row['lazada_stock'] == 30
    assert row['cost_price'] == 10.0
    assert row['base_sell_price'] == 20.0


# ── route: a real edit-form POST must not clobber the three columns ───────────

@pytest.fixture
def admin_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c, empty_db


def test_edit_post_does_not_clobber_hard_to_sell_and_marketplace_stock(admin_client):
    c, db = admin_client
    pid = _seed_product(db)

    # Exactly the fields the edit form actually renders (see
    # templates/products/form.html's {% if action == 'edit' %} block) — no
    # hard_to_sell / shopee_stock / lazada_stock inputs exist there.
    resp = c.post(f'/products/{pid}/edit', data={
        'product_name': 'ignored-name',
        'unit_type': 'ตัว',
        'units_per_carton': '1',
        'units_per_box': '1',
        'cost_price': '10',
        'base_sell_price': '99',
        'low_stock_threshold': '10',
    })
    assert resp.status_code in (302, 200)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT base_sell_price, hard_to_sell, shopee_stock, lazada_stock "
        "FROM products WHERE id = ?", (pid,)
    ).fetchone()
    conn.close()

    assert row['base_sell_price'] == 99  # the actually-edited field saved
    assert row['hard_to_sell'] == 1      # preserved, not zeroed
    assert row['shopee_stock'] == 50     # preserved, not zeroed
    assert row['lazada_stock'] == 30     # preserved, not zeroed
