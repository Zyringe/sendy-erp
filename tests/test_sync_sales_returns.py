"""SR (sales-return) handling in models._sync_bsn_to_stock.

Mirror of the GR purchase-return fix. SR rows (ใบลดหนี้ / customer returned
goods) were posted as OUT (reducing stock) like a normal sale; they must post
IN (goods come back → raise stock) and be excluded from WACC purchase averaging.

Express prints SR qty as a positive number; sales_transactions has no
return_flag column, so we key on the SR doc-no prefix.
"""


def _seed_product(conn, sku, name, unit_type='ตัว', cost_price=0.0):
    cur = conn.execute(
        "INSERT INTO products (sku, product_name, unit_type, cost_price) "
        "VALUES (?, ?, ?, ?)",
        (sku, name, unit_type, cost_price),
    )
    pid = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
        (pid,),
    )
    return pid


def _seed_sale(conn, product_id, doc_no, qty, unit='ตัว', *, net, customer='ร้านทดสอบ'):
    conn.execute(
        """
        INSERT INTO sales_transactions
            (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
             customer, customer_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, ?, 'BSN001', 'test', ?, 'c-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (doc_no, doc_no.split('-')[0], product_id, customer, qty, unit, net, net),
    )


def test_sr_return_posts_in_not_out(empty_db_conn):
    """Sell 100, customer returns 30 → net stock -70 (the SR adds +30 back)."""
    import models

    pid = _seed_product(empty_db_conn, 96001, "SR Return Test", unit_type="ตัว")
    _seed_sale(empty_db_conn, pid, "IV9600001-1", qty=100, net=1500.0)
    _seed_sale(empty_db_conn, pid, "SR9600001-1", qty=30, net=450.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()

    iv = empty_db_conn.execute(
        "SELECT txn_type, quantity_change, note FROM transactions "
        "WHERE reference_no='IV9600001-1'"
    ).fetchone()
    assert iv['txn_type'] == 'OUT'
    assert iv['quantity_change'] == -100
    assert iv['note'] == 'BSN ขาย'

    sr = empty_db_conn.execute(
        "SELECT txn_type, quantity_change, note FROM transactions "
        "WHERE reference_no='SR9600001-1'"
    ).fetchone()
    assert sr['txn_type'] == 'IN', "SR return must post IN, not OUT"
    assert sr['quantity_change'] == 30, "SR return must RAISE stock"
    assert sr['note'] == 'BSN ขาย-คืน'

    stock = empty_db_conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()['quantity']
    assert stock == -70, f"-100 sold + 30 returned should be -70, got {stock}"


def test_sr_return_not_counted_as_purchase_in_wacc(empty_db_conn):
    """An SR return raises stock but must NOT become a WACC purchase lot."""
    import models

    pid = _seed_product(empty_db_conn, 96002, "SR WACC", unit_type="ตัว", cost_price=10.0)
    _seed_sale(empty_db_conn, pid, "IV9600002-1", qty=50, net=1000.0)
    _seed_sale(empty_db_conn, pid, "SR9600002-1", qty=10, net=200.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'sales_transactions', 'sales')
    empty_db_conn.commit()
    models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()

    purchase_refs = [
        r['reference_no'] for r in empty_db_conn.execute(
            "SELECT reference_no FROM product_cost_ledger "
            "WHERE product_id=? AND event_type='PURCHASE'", (pid,)
        ).fetchall()
    ]
    assert 'SR9600002-1' not in purchase_refs, \
        "SR return must not appear as a PURCHASE lot in the WACC ledger"
