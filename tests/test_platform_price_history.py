"""Behavior tests for the marketplace price-history trigger (mig 137).

The trigger `platform_skus_price_history_update` fires AFTER UPDATE ON
platform_skus and records one platform_price_history row per changed price
field (price / special_price), gated by a WHEN clause so stock- or
mapping-only updates never log. This mirrors 008_product_price_history for
internal prices.

Tests run against `empty_db_conn` (a clone of the live schema, which now
carries the mig-137 table + trigger).
"""


def _insert_sku(conn, variation_id='V1', price=100, special_price=90,
                internal_product_id=None, platform='shopee'):
    cur = conn.execute(
        """INSERT INTO platform_skus
               (platform, variation_id, product_name, price, special_price,
                stock, internal_product_id)
           VALUES (?,?,?,?,?,?,?)""",
        (platform, variation_id, 'test', price, special_price, 5,
         internal_product_id),
    )
    return cur.lastrowid


def _hist(conn):
    return conn.execute(
        """SELECT platform, variation_id, internal_product_id, field_name,
                  old_value, new_value, source
             FROM platform_price_history ORDER BY id"""
    ).fetchall()


def test_insert_alone_logs_nothing(empty_db_conn):
    conn = empty_db_conn
    _insert_sku(conn)
    conn.commit()
    assert _hist(conn) == []  # first-seen listing is not a "change"


def test_price_change_logs_one_row(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn, price=100)
    conn.commit()
    conn.execute('UPDATE platform_skus SET price=117 WHERE id=?', (sku_id,))
    conn.commit()
    rows = _hist(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r['field_name'] == 'price'
    assert r['old_value'] == 100
    assert r['new_value'] == 117
    assert r['platform'] == 'shopee'
    assert r['variation_id'] == 'V1'
    assert r['source'] == 'platform_skus.update'


def test_special_price_change_logs_one_row(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn, special_price=90)
    conn.commit()
    conn.execute('UPDATE platform_skus SET special_price=103 WHERE id=?', (sku_id,))
    conn.commit()
    rows = _hist(conn)
    assert len(rows) == 1
    assert rows[0]['field_name'] == 'special_price'
    assert rows[0]['old_value'] == 90
    assert rows[0]['new_value'] == 103


def test_stock_only_update_logs_nothing(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn)
    conn.commit()
    conn.execute('UPDATE platform_skus SET stock=999 WHERE id=?', (sku_id,))
    conn.commit()
    assert _hist(conn) == []  # WHEN gate: non-price update must not fire


def test_reimport_same_price_logs_nothing(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn, price=100)
    conn.commit()
    # simulate an import that writes the SAME price (upsert always touches the row)
    conn.execute('UPDATE platform_skus SET price=100, imported_at=datetime("now") WHERE id=?', (sku_id,))
    conn.commit()
    assert _hist(conn) == []  # IS NOT gate: unchanged price must not fire


def test_both_fields_change_logs_two_rows(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn, price=100, special_price=90)
    conn.commit()
    conn.execute('UPDATE platform_skus SET price=120, special_price=110 WHERE id=?', (sku_id,))
    conn.commit()
    fields = sorted(r['field_name'] for r in _hist(conn))
    assert fields == ['price', 'special_price']


def test_null_special_price_transition_logs(empty_db_conn):
    conn = empty_db_conn
    sku_id = _insert_sku(conn, special_price=None)
    conn.commit()
    conn.execute('UPDATE platform_skus SET special_price=50 WHERE id=?', (sku_id,))
    conn.commit()
    rows = _hist(conn)
    assert len(rows) == 1
    assert rows[0]['old_value'] is None
    assert rows[0]['new_value'] == 50


def test_internal_product_id_propagates(empty_db_conn):
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (9001, 'mapped')")
    sku_id = _insert_sku(conn, internal_product_id=9001)
    conn.commit()
    conn.execute('UPDATE platform_skus SET price=200 WHERE id=?', (sku_id,))
    conn.commit()
    rows = _hist(conn)
    assert len(rows) == 1
    assert rows[0]['internal_product_id'] == 9001


# ── get_marketplace_listings_with_history (the 'ราคา marketplace' card helper) ──

def _seed_listing(conn, platform, vid, price, special, pid, qps=1):
    conn.execute(
        """INSERT INTO platform_skus (platform, variation_id, product_name, price,
               special_price, stock, internal_product_id, qty_per_sale)
           VALUES (?,?,?,?,?,?,?,?)""",
        (platform, vid, 'p', price, special, 5, pid, qps))


def _seed_hist(conn, platform, vid, pid, field, old, new, date):
    conn.execute(
        """INSERT INTO platform_price_history (platform, variation_id,
               internal_product_id, field_name, old_value, new_value, changed_at, source)
           VALUES (?,?,?,?,?,?,?,'seed')""",
        (platform, vid, pid, field, old, new, date))


def test_listings_helper_groups_and_flags_history(empty_db_conn):
    import models
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (500,'p')")
    _seed_listing(conn, 'shopee', 'S1', 117, None, 500)          # has history
    _seed_listing(conn, 'shopee', 'S2', 15, None, 500)           # no history
    _seed_listing(conn, 'lazada', 'L1', 125, 95, 500, qps=12)    # special < price + history
    _seed_hist(conn, 'shopee', 'S1', 500, 'price', 100, 117, '2026-07-16')
    _seed_hist(conn, 'lazada', 'L1', 500, 'special_price', 80, 95, '2026-07-17')
    conn.commit()

    out = models.get_marketplace_listings_with_history(500)
    sh = {l['variation_id']: l for l in out['shopee']['listings']}
    assert sh['S1']['has_history'] is True
    assert sh['S1']['last_changed'] == '2026-07-16'
    assert sh['S1']['effective'] == 117 and sh['S1']['list_price'] is None
    assert sh['S2']['has_history'] is False and sh['S2']['last_changed'] is None

    lz = out['lazada']['listings'][0]
    assert lz['effective'] == 95 and lz['list_price'] == 125   # special < price → struck list
    assert lz['has_history'] is True
    assert lz['history'][0]['field_label'] == 'ราคาพิเศษ'
    assert lz['history'][0]['date'] == '2026-07-17'


def test_listings_helper_no_strike_when_special_ge_price(empty_db_conn):
    import models
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (501,'p')")
    _seed_listing(conn, 'lazada', 'L2', 100, 100, 501)   # special == price → no strike
    conn.commit()
    lz = models.get_marketplace_listings_with_history(501)['lazada']['listings'][0]
    assert lz['effective'] == 100 and lz['list_price'] is None


def test_listings_helper_excludes_ignored(empty_db_conn):
    import models
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (502,'p')")
    _seed_listing(conn, 'shopee', 'S9', 50, None, 502)
    conn.execute("UPDATE platform_skus SET is_ignored=1 WHERE variation_id='S9'")
    conn.commit()
    out = models.get_marketplace_listings_with_history(502)
    assert out['shopee']['listings'] == [] and out['lazada']['listings'] == []


def test_listings_helper_label_fallback_chain(empty_db_conn):
    """label = option name → seller SKU → listing title → variation code."""
    import models
    conn = empty_db_conn
    conn.execute("INSERT INTO products (id, product_name) VALUES (503,'p')")

    def ins(vid, vname, sku, pname):
        conn.execute(
            """INSERT INTO platform_skus (platform, variation_id, variation_name,
                   seller_sku, product_name, price, stock, internal_product_id, qty_per_sale)
               VALUES ('shopee',?,?,?,?,10,5,503,1)""",
            (vid, vname, sku, pname))

    ins('V_name', 'รุ่นหนา #666', None, 'listing title A')      # → option name
    ins('V_sku',  None, '666-Pro', 'listing title B')          # → seller sku
    ins('V_title', None, None, 'มือจับบัว 5นิ้ว SENDAI')        # → listing title
    ins('V_code', None, None, '')                              # → variation code
    conn.commit()

    lst = {l['variation_id']: l
           for l in models.get_marketplace_listings_with_history(503)['shopee']['listings']}
    assert lst['V_name']['label'] == 'รุ่นหนา #666'
    assert lst['V_sku']['label'] == '666-Pro'
    assert lst['V_title']['label'] == 'มือจับบัว 5นิ้ว SENDAI'
    assert lst['V_code']['label'].startswith('รหัส ')


def test_thaidate_filter():
    from filters import thaidate
    assert thaidate('2026-07-16') == '16 ก.ค. 2026'
    assert thaidate('2026-07-21 12:24:08') == '21 ก.ค. 2026'   # datetime → date part
    assert thaidate(None) == ''
    assert thaidate('garbage') == 'garbage'
