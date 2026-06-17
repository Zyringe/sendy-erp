"""Marketplace per-order margin: ratio resolver + COGS/margin.

The order line carries NO variation_id/seller_sku, so qty_per_sale (base units per
1 marketplace sale unit) is resolved from platform_skus by product_id, then
disambiguated by variation_name when a product is sold at multiple pack sizes.
Getting the ratio wrong is the per-โหล/pack trap (COGS off by up to 100×), so the
resolver is TDD'd and an unresolved ratio yields margin=None, never a wrong total.
"""
import models


def _listing(c, pid, var_name, ratio):
    c.execute(
        """INSERT INTO platform_skus (platform, product_name, variation_name,
                                       internal_product_id, qty_per_sale)
           VALUES ('shopee', 'p', ?, ?, ?)""", (var_name, pid, ratio))


# ── ratio resolver ────────────────────────────────────────────────────────────

def test_resolve_ratio_single_unit(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name) VALUES (901,'p901')")
    _listing(c, 901, 'A', 1.0); _listing(c, 901, 'B', 1.0); c.commit()
    assert models.resolve_line_ratio(c, 'shopee', 901, 'anything') == (1.0, 'single')


def test_resolve_ratio_single_pack(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name) VALUES (902,'p902')")
    _listing(c, 902, 'โหล', 12.0); c.commit()
    assert models.resolve_line_ratio(c, 'shopee', 902, None) == (12.0, 'single')


def test_resolve_ratio_multi_matched_by_variation_name(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name) VALUES (903,'p903')")
    _listing(c, 903, '4-4 ดำ (50ตัว)', 50.0); _listing(c, 903, '4-4 ดำ (100ตัว)', 100.0); c.commit()
    assert models.resolve_line_ratio(c, 'shopee', 903, '4-4 ดำ (100ตัว)') == (100.0, 'matched')


def test_resolve_ratio_multi_ambiguous_when_no_name_match(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name) VALUES (904,'p904')")
    _listing(c, 904, '50ตัว', 50.0); _listing(c, 904, '100ตัว', 100.0); c.commit()
    assert models.resolve_line_ratio(c, 'shopee', 904, 'ไม่ตรงสักอัน') == (None, 'ambiguous')


def test_resolve_ratio_no_listing(empty_db_conn):
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name) VALUES (905,'p905')")
    c.commit()
    assert models.resolve_line_ratio(c, 'shopee', 905, None) == (None, 'no_listing')


# ── order margin = net − COGS ───────────────────────────────────────────────────

def _order_with_line(c, sn, pid, var_name, qty, net):
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee', ?)", (sn,))
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn=?", (sn,)).fetchone()['id']
    c.execute(
        """INSERT INTO marketplace_order_items
           (order_id, platform, order_sn, line_key, internal_product_id, variation_name, qty, unit_price, item_subtotal)
           VALUES (?, 'shopee', ?, 'L1', ?, ?, ?, 0, 0)""", (oid, sn, pid, var_name, qty))
    models.upsert_marketplace_fees(c, [{'order_sn': sn, 'item_value': 0, 'net_payout': net, 'fee_total': 0}], 'f.xlsx')
    c.commit()
    return oid


def test_order_margin_pack_ratio_applied(empty_db_conn):
    # cost 2.0/base unit, ratio 12 (1 โหล = 12 base), qty 2 โหล → COGS = 2×2×12 = 48
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name, cost_price) VALUES (910,'p',2.0)")
    _listing(c, 910, 'โหล', 12.0)
    oid = _order_with_line(c, 'M1', 910, 'โหล', 2, 400.0)
    m = models.get_order_margin(c, oid)
    assert m['cogs'] == 48.0
    assert m['net'] == 400.0
    assert m['margin'] == 352.0
    assert m['unresolved'] == 0 and m['cost_gap'] == 0


def test_order_margin_none_when_ratio_unresolved(empty_db_conn):
    # multi-ratio product, line variation doesn't match → ratio unresolved → margin None
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name, cost_price) VALUES (911,'p',2.0)")
    _listing(c, 911, '50ตัว', 50.0); _listing(c, 911, '100ตัว', 100.0)
    oid = _order_with_line(c, 'M2', 911, 'ไม่ตรง', 1, 100.0)
    m = models.get_order_margin(c, oid)
    assert m['unresolved'] == 1
    assert m['margin'] is None       # never report a wrong total


def test_order_margin_flags_cost_gap(empty_db_conn):
    # product with no cost (cost_price=0; column is NOT NULL) → cost_gap, margin None
    c = empty_db_conn
    c.execute("INSERT INTO products (id, product_name, cost_price) VALUES (912,'p',0)")
    _listing(c, 912, 'x', 1.0)
    oid = _order_with_line(c, 'M3', 912, 'x', 3, 90.0)
    m = models.get_order_margin(c, oid)
    assert m['cost_gap'] == 1
    assert m['margin'] is None


def test_api_order_detail_includes_margin(tmp_db_conn):
    """The order-detail JSON carries the margin block for the modal."""
    import os
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn='MARGAPI1'")
    c.execute("INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee','MARGAPI1')")
    oid = c.execute("SELECT id FROM marketplace_orders WHERE order_sn='MARGAPI1'").fetchone()['id']
    c.commit()
    cl = flask_app.test_client()
    with cl.session_transaction() as s:
        s['user_id'] = 4; s['username'] = 'staffer'; s['role'] = 'staff'
    data = cl.get(f'/marketplace/api/order/{oid}').get_json()
    assert 'margin' in data and 'cogs' in data['margin'] and 'margin' in data['margin']
