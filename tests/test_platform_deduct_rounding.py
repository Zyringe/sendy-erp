"""platform_skus stock deduction in models._sync_bsn_to_stock.

The deduction loop converts a BSN sales qty (base units) into "platform
units" (qty_per_sale packs) and deducts that from platform_skus.stock — an
ONLINE COUNTER only (not the stock ledger; it self-heals on the next
marketplace upload). A forced minimum of 1 unit over-drew the counter when
the remaining base qty was under half a platform unit (e.g. selling 2 ตัว
against a 12-pack listing wrongly deducted a whole 12-pack).
"""


def _seed_product(conn, name, unit_type='ตัว', cost_price=0.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, ?)",
        (name, unit_type, cost_price),
    )
    pid = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
        (pid,),
    )
    return pid


def _seed_sale(conn, product_id, doc_no, qty, *, net, customer='หน้าร้านS'):
    conn.execute(
        """
        INSERT INTO sales_transactions
            (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, ?, 'BSN001', 'test', ?, 'c-code',
                ?, 'ตัว', 1.0, 0, '', ?, ?, 0)
        """,
        (doc_no, doc_no.split('-')[0], product_id, customer, qty, net, net),
    )


def _seed_platform_sku(conn, product_id, qps, stock, *, platform='shopee', variation_id=None):
    cur = conn.execute(
        """
        INSERT INTO platform_skus
            (platform, product_name, variation_id, internal_product_id,
             qty_per_sale, stock)
        VALUES (?, 'test listing', ?, ?, ?, ?)
        """,
        (platform, variation_id or f"v-{product_id}-{qps}", product_id, qps, stock),
    )
    return cur.lastrowid


def test_remainder_under_half_unit_deducts_zero(empty_db_conn):
    """Sell 2 ตัว against a qty_per_sale=12 listing: round(2/12)=0, must NOT
    force a whole 12-pack off the online counter."""
    import models

    pid = _seed_product(empty_db_conn, "Under-half unit test")
    sku_id = _seed_platform_sku(empty_db_conn, pid, qps=12, stock=50)
    _seed_sale(empty_db_conn, pid, "IV9700001-1", qty=2, net=100.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_id,)
    ).fetchone()['stock']
    assert stock == 50, f"remainder under half a unit should deduct 0, got stock={stock}"


def test_remainder_over_half_unit_rounds_up_one(empty_db_conn):
    """Sell 7 ตัว against qty_per_sale=12: round(7/12)=round(0.583)=1 (unchanged)."""
    import models

    pid = _seed_product(empty_db_conn, "Over-half unit test")
    sku_id = _seed_platform_sku(empty_db_conn, pid, qps=12, stock=50)
    _seed_sale(empty_db_conn, pid, "IV9700002-1", qty=7, net=350.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_id,)
    ).fetchone()['stock']
    assert stock == 49, f"round(7/12)=1 should deduct 1, got stock={stock}"


def test_exact_multiple_unchanged(empty_db_conn):
    """Sell 24 ตัว against qty_per_sale=12: exact 2 packs (unchanged)."""
    import models

    pid = _seed_product(empty_db_conn, "Exact multiple test")
    sku_id = _seed_platform_sku(empty_db_conn, pid, qps=12, stock=50)
    _seed_sale(empty_db_conn, pid, "IV9700003-1", qty=24, net=1200.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    stock = empty_db_conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_id,)
    ).fetchone()['stock']
    assert stock == 48, f"24/12=2 exactly should deduct 2, got stock={stock}"


def test_cascade_across_two_skus_still_works(empty_db_conn):
    """Two mapped SKUs (qty_per_sale=12 and =1), ordered by stock DESC per the
    query. Sell 14 ตัว: the 12-pack SKU takes round(14/12)=1 (=12 units),
    leaving remaining=2 to flow to the qty_per_sale=1 SKU, which deducts 2
    (round(2/1)=2, unchanged path). Confirms the loop still cascades after
    the rounding fix."""
    import models

    pid = _seed_product(empty_db_conn, "Cascade test")
    # Higher stock first so ORDER BY stock DESC draws the 12-pack SKU first.
    sku_pack = _seed_platform_sku(empty_db_conn, pid, qps=12, stock=100)
    sku_single = _seed_platform_sku(empty_db_conn, pid, qps=1, stock=20)
    _seed_sale(empty_db_conn, pid, "IV9700004-1", qty=14, net=700.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    pack_stock = empty_db_conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_pack,)
    ).fetchone()['stock']
    single_stock = empty_db_conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_single,)
    ).fetchone()['stock']
    assert pack_stock == 99, f"12-pack SKU should deduct 1 (round(14/12)), got {pack_stock}"
    assert single_stock == 18, f"remainder 2 should deduct 2 from the qty=1 SKU, got {single_stock}"
