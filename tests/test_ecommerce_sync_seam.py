"""The seam between the BSN stock sync, the /ecommerce estimate, and a
marketplace ORDER import (order-driven-platform-deduction plan, Phase 1).

Historically `_sync_bsn_to_stock`'s walk decremented platform_skus.stock for
a synced หน้าร้าน sale, so by the time `_sold_since_by_pid` excluded that same
sale from sold_since, the mirror already carried it — one 100-unit Shopee
sale, counted exactly once. That walk is retired (task 1.4): the sync now
ONLY posts the warehouse ledger and marks the row synced; it never touches
platform_skus.stock. `_sold_since_by_pid`'s exclusion is unchanged, so it
still stops counting a synced sale — but nothing else counts it either,
until a marketplace ORDER import runs the new diff engine
(`_apply_order_stock_effect`) against it.

This is D5's accepted gap, by design: between a BSN sync and the next order
import, a synced sale is invisible to BOTH signals and `est` reads HIGH by
that sale's amount (never toward oversell — the opposite of the old
false-RED failure this file used to pin). The gap is bounded (self-heals at
the next order import or platform snapshot) and Put accepted it as the price
of option A (weekly order-file cadence). This test walks the seam through
all three stages and is the one place the whole BSN sync -> overview ->
order-import chain is exercised together.
"""
import models
from models.bsn_sync import PLATFORM_STOCK_DEDUCT_CUSTOMERS, _sync_bsn_to_stock
from models.marketplace import import_marketplace_orders

SNAP = '2026-07-01 00:00:00'
SNAP_DAY = '2026-07-01'
AFTER = '2026-07-05'
ORDER_DATE = '2026-07-05 10:00'


def _seed(conn, pid, *, stock, ps_stock, qty_per_sale=1, unit='ตัว'):
    conn.execute(
        "INSERT INTO products (id, product_name, unit_type) VALUES (?,?,?)",
        (pid, f'สินค้า seam {pid}', unit),
    )
    conn.execute("INSERT INTO stock_levels(product_id, quantity) VALUES (?,?)", (pid, stock))
    conn.execute(
        """INSERT INTO platform_skus
             (platform, variation_id, product_id_str, product_name, internal_product_id,
              stock, qty_per_sale, is_ignored, imported_at)
           VALUES ('shopee', ?, 'P1', 'listing', ?, ?, ?, 0, ?)""",
        (f'v{pid}', pid, ps_stock, qty_per_sale, SNAP),
    )


def _sale(conn, pid, qty, customer='หน้าร้านS', unit='ตัว', doc_no='IV900'):
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, product_id, customer, qty,"
        " unit, synced_to_stock) VALUES (?,?,?,?,?,?,0)",
        (AFTER, doc_no, pid, customer, qty, unit),
    )


def _est(pid):
    rows, _, _ = models.get_marketplace_overview()
    r = next(x for x in rows if x['product_id'] == pid)
    return r['platforms']['shopee']['est']


def _order_for(pid, qty, *, order_sn='ORDER900', status='shipped'):
    """A minimal parsed order (parse_orders.py shape) whose single line
    resolves to pid's listing via variation_id, exactly as _seed wrote it."""
    return {
        'platform': 'shopee', 'order_sn': order_sn, 'status': status,
        'order_date': ORDER_DATE,
        'items': [{
            'line_key': f'{order_sn}-1', 'seller_sku': None, 'variation_id': f'v{pid}',
            'item_name': 'listing', 'variation_name': None,
            'qty': qty, 'unit_price': 10.0, 'item_subtotal': qty * 10.0,
        }],
    }


def test_the_bsn_sync_to_overview_to_order_import_seam(empty_db_conn):
    """The seam, all three stages, one product:

    (a) the BSN sync posts the warehouse OUT -- (b) and leaves
    platform_skus.stock untouched -- (c) the sale stays EXCLUDED from
    sold_since (unchanged behaviour, still correct) -- so (d) est reads HIGH
    by the sale's own amount, the D5 gap, asserted explicitly, not merely
    tolerated -- then (e) CONTROL: an order import for the SAME sale runs
    the diff engine and est settles back to the expected post-sale figure.
    """
    c = empty_db_conn
    _seed(c, 70, stock=500, ps_stock=500)
    c.commit()
    assert _est(70) == 500                        # CONTROL: baseline

    _sale(c, 70, qty=100)
    c.commit()
    # Before the sync: unsynced, so sold_since still subtracts it directly.
    assert _est(70) == 400

    _sync_bsn_to_stock(c, 'sales_transactions', 'sales')
    c.commit()

    # (a) the warehouse ledger moved.
    warehouse_qty = c.execute(
        "SELECT quantity FROM stock_levels WHERE product_id = 70"
    ).fetchone()['quantity']
    assert warehouse_qty == 400, 'the warehouse OUT must still post'

    # (b) platform_skus.stock is untouched by the sync -- no walk left to move it.
    ps_stock = c.execute(
        "SELECT stock FROM platform_skus WHERE internal_product_id = 70"
    ).fetchone()['stock']
    assert ps_stock == 500, 'the sync must not touch platform_skus.stock'

    # (c)+(d) the D5 gap, asserted explicitly: the sale is now synced (excluded
    # from sold_since, unchanged behaviour) but nothing else has deducted it
    # from the mirror yet, so est reads HIGH -- back up to the pre-sale figure,
    # not the 400 a reader might expect. This is intended, not a bug: it heals
    # at the next order import (below) or platform snapshot.
    assert _est(70) == 500, (
        'D5 gap: a synced-but-not-yet-order-imported sale must read est HIGH, '
        'never toward oversell')

    # (e) CONTROL: the order file arrives. The diff engine deducts the listing
    # exactly once, closing the gap -- est settles to the true post-sale figure.
    stats = import_marketplace_orders(c, [_order_for(70, 100)], source_file='test')
    assert stats['deducted'] == 100, stats         # CONTROL: the diff engine really ran
    assert _est(70) == 400, 'the order import must close the D5 gap'


def test_customer_the_sync_never_deducts_for_is_still_adjusted(empty_db_conn):
    """หน้าร้านB books Shopee sales but is absent from the deduct map, so the
    sync leaves platform_skus alone and sold_since must still do the work.
    Unaffected by the walk's retirement: the sync never touched this
    customer's sales either way."""
    assert 'หน้าร้านB' not in PLATFORM_STOCK_DEDUCT_CUSTOMERS
    c = empty_db_conn
    _seed(c, 72, stock=500, ps_stock=500)
    _sale(c, 72, qty=100, customer='หน้าร้านB', doc_no='IV902')
    c.commit()
    _sync_bsn_to_stock(c, 'sales_transactions', 'sales')
    c.commit()
    ps_stock = c.execute(
        "SELECT stock FROM platform_skus WHERE internal_product_id = 72"
    ).fetchone()['stock']
    assert ps_stock == 500, "sync must not deduct platform stock for หน้าร้านB"
    assert _est(72) == 400, "so sold_since must still deduct it"
