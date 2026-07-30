"""The seam between the BSN stock sync and the /ecommerce estimate.

Both subsystems deduct a marketplace sale, and neither knew about the other:
`_sync_bsn_to_stock` decrements platform_skus.stock, while
`_sold_since_by_pid` subtracts the same sale from file_units. One 100-unit
Shopee sale used to make `est` fall by 200 -> false RED alerts.

Every existing test covers ONE side (tests/test_ecommerce_overview.py seeds
platform_skus by hand and never runs the sync; tests/test_platform_deduct_
rounding.py runs the sync and never reads the overview), which is exactly why
the double count survived. These tests cross the boundary.
"""
import models
from models.bsn_sync import PLATFORM_STOCK_DEDUCT_CUSTOMERS, _sync_bsn_to_stock

SNAP = '2026-07-01 00:00:00'
SNAP_DAY = '2026-07-01'
AFTER = '2026-07-05'


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


def test_one_sale_is_deducted_exactly_once_across_the_sync(empty_db_conn):
    c = empty_db_conn
    _seed(c, 70, stock=500, ps_stock=500)
    c.commit()
    assert _est(70) == 500

    _sale(c, 70, qty=100)
    c.commit()
    # before the sync: platform_skus untouched, so sold_since carries it
    assert _est(70) == 400

    _sync_bsn_to_stock(c, 'sales_transactions', 'sales')
    c.commit()
    # after the sync: platform_skus now carries it, sold_since must stand down.
    # Still 400 — the same sale, counted once. Was 300 before the fix.
    assert _est(70) == 400

    ps_stock = c.execute(
        "SELECT stock FROM platform_skus WHERE internal_product_id = 70"
    ).fetchone()['stock']
    assert ps_stock == 400, "the sync should have moved the deduction into platform_skus"


def test_sync_does_not_flip_a_healthy_listing_to_red(empty_db_conn):
    """The user-visible symptom: a listing that is open and adequately stocked
    must not show as RED just because a sale was imported."""
    c = empty_db_conn
    _seed(c, 71, stock=200, ps_stock=100)
    _sale(c, 71, qty=100, doc_no='IV901')
    c.commit()
    _sync_bsn_to_stock(c, 'sales_transactions', 'sales')
    c.commit()
    rows, _, _ = models.get_marketplace_overview()
    r = next(x for x in rows if x['product_id'] == 71)
    # 100 file units - 100 sold would floor to 0 -> RED. Correct answer is 0
    # left on the platform only because the sync took it, not twice over.
    assert r['platforms']['shopee']['sold_since'] == 0
    assert r['platforms']['shopee']['est'] == 0


def test_customer_the_sync_never_deducts_for_is_still_adjusted(empty_db_conn):
    """หน้าร้านB books Shopee sales but is absent from the deduct map, so the
    sync leaves platform_skus alone and sold_since must still do the work."""
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
