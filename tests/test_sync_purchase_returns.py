"""GR (purchase-return) handling in models._sync_bsn_to_stock.

Finding #3 on PR #87: GR rows (ใบลดหนี้ / goods returned to supplier) were
posted as +IN (adding stock) and averaged into WACC as positive purchase lots.
They must instead post OUT (reducing stock) and be excluded from WACC averaging.

Express prints GR qty as a positive number; the GR doc-type is always a
credit/return (the purchase-history parser's validate() subtracts every GR row
from the grand total). We rely on the GR doc-no prefix because the parser's
return_flag is not persisted on the stored purchase_transactions row.
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


def _seed_purchase(conn, product_id, doc_no, qty, unit='ตัว', *, net):
    conn.execute(
        """
        INSERT INTO purchase_transactions
            (date_iso, doc_no, product_id, bsn_code, product_name_raw,
             supplier, supplier_code, qty, unit, unit_price, vat_type,
             discount, total, net, synced_to_stock)
        VALUES ('2026-04-24', ?, ?, 'BSN001', 'test', 'sup', 'sup-code',
                ?, ?, 1.0, 0, '', ?, ?, 0)
        """,
        (doc_no, product_id, qty, unit, net, net),
    )


def test_gr_return_posts_out_not_in(empty_db_conn):
    """Buy 100, return 30 → net stock 70. The GR row must be OUT -30."""
    import models

    pid = _seed_product(empty_db_conn, 95001, "Return Test", unit_type="ตัว")
    _seed_purchase(empty_db_conn, pid, "HP9500001", qty=100, net=1000.0)
    _seed_purchase(empty_db_conn, pid, "GR9500001", qty=30, net=300.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    hp = empty_db_conn.execute(
        "SELECT txn_type, quantity_change, note FROM transactions "
        "WHERE reference_no='HP9500001'"
    ).fetchone()
    assert hp['txn_type'] == 'IN'
    assert hp['quantity_change'] == 100
    assert hp['note'] == 'BSN ซื้อ'

    gr = empty_db_conn.execute(
        "SELECT txn_type, quantity_change, note FROM transactions "
        "WHERE reference_no='GR9500001'"
    ).fetchone()
    assert gr['txn_type'] == 'OUT', "GR return must post OUT, not IN"
    assert gr['quantity_change'] == -30, "GR return must REDUCE stock"
    assert gr['note'] == 'BSN ซื้อ-คืน'

    stock = empty_db_conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)
    ).fetchone()['quantity']
    assert stock == 70, f"100 bought − 30 returned should be 70, got {stock}"


def test_gr_return_excluded_from_wacc(empty_db_conn):
    """A GR return must not be averaged into WACC as a purchase lot.

    Buy 100 @ net 1000 (unit cost 10). Return 30 @ net 600 (unit cost 20).
    Correct WACC = 10 (only the HP lot sets cost; the return reduces stock at
    the running average). The old +IN bug would have blended in the GR lot:
    (100*10 + 30*20)/130 = 12.31.
    """
    import models

    pid = _seed_product(empty_db_conn, 95002, "WACC Return", unit_type="ตัว")
    _seed_purchase(empty_db_conn, pid, "HP9500002", qty=100, net=1000.0)
    _seed_purchase(empty_db_conn, pid, "GR9500002", qty=30, net=600.0)
    empty_db_conn.commit()

    models._sync_bsn_to_stock(empty_db_conn, 'purchase_transactions', 'purchase')
    empty_db_conn.commit()

    wacc = models.recalculate_product_wacc(pid, empty_db_conn)
    empty_db_conn.commit()
    assert abs(wacc - 10.0) < 1e-6, f"WACC should be 10 (HP lot only); got {wacc}"

    purchase_refs = [
        r['reference_no'] for r in empty_db_conn.execute(
            "SELECT reference_no FROM product_cost_ledger "
            "WHERE product_id=? AND event_type='PURCHASE'", (pid,)
        ).fetchall()
    ]
    assert 'HP9500002' in purchase_refs
    assert 'GR9500002' not in purchase_refs, \
        "GR return must not appear as a PURCHASE lot in the WACC ledger"
