"""ecommerce-revamp Phase 3 — aggregation model layer (models/ecommerce_overview.py).

Covers the core-math block from projects/ecommerce-revamp/plan.md:
    listing_units, platform_file_units, snapshot_date, sold_since, platform_est,
    true_available, combined_open, ALERT_RED/AMBER, DEAD.

Seeds a clean-schema DB directly (empty_db_conn), same style as
tests/test_conversion_buildable.py. Oracle = hand-computed expected numbers.
"""
import sqlite3

import pytest

import models


# ── seed helpers ──────────────────────────────────────────────────────────────

def _product(conn, pid, name, sku_code=None, unit_type='ตัว', base_sell_price=0):
    conn.execute(
        "INSERT INTO products (id, product_name, sku_code, unit_type, base_sell_price) VALUES (?,?,?,?,?)",
        (pid, name, sku_code, unit_type, base_sell_price),
    )


def _stock(conn, pid, qty):
    conn.execute(
        "INSERT INTO stock_levels(product_id, quantity) VALUES (?,?) "
        "ON CONFLICT(product_id) DO UPDATE SET quantity=excluded.quantity",
        (pid, qty),
    )


_vid_counter = [0]


def _ps(conn, platform, pid, product_id_str='P1', stock=0, qty_per_sale=1,
        is_ignored=0, imported_at=None, product_name='listing', variation_id=None):
    if variation_id is None:
        _vid_counter[0] += 1
        variation_id = f'v{_vid_counter[0]}'
    conn.execute(
        """INSERT INTO platform_skus
             (platform, variation_id, product_id_str, product_name, internal_product_id,
              stock, qty_per_sale, is_ignored, imported_at)
           VALUES (?,?,?,?,?,?,?,?, COALESCE(?, datetime('now','localtime')))""",
        (platform, variation_id, product_id_str, product_name, pid,
         stock, qty_per_sale, is_ignored, imported_at),
    )


def _sale(conn, pid, date_iso, qty, unit='ตัว', customer='หน้าร้านS', doc_no='IV1'):
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, product_id, customer, qty, unit) "
        "VALUES (?,?,?,?,?,?)",
        (date_iso, doc_no, pid, customer, qty, unit),
    )


def _uc(conn, pid, bsn_unit, ratio):
    conn.execute(
        "INSERT INTO unit_conversions(product_id, bsn_unit, ratio) VALUES (?,?,?)",
        (pid, bsn_unit, ratio),
    )


def _formula(conn, name, output_pid, output_qty, inputs, is_active=1):
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, is_active) VALUES (?,?,?,?)",
        (name, output_pid, output_qty, is_active),
    )
    fid = cur.lastrowid
    for ipid, iqty in inputs:
        conn.execute(
            "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
            (fid, ipid, iqty),
        )
    return fid


def _row(rows, pid):
    return next(r for r in rows if r['product_id'] == pid)


# ── get_marketplace_overview: core math ──────────────────────────────────────

def test_qty_per_sale_multiplies_file_stock(empty_db_conn):
    c = empty_db_conn
    _product(c, 1, 'สินค้าโหล')
    _ps(c, 'shopee', 1, stock=5, qty_per_sale=12, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 1)
    assert r['platforms']['shopee']['est'] == 60  # 5 * 12, no sales after snapshot


def test_sold_since_applies_unit_conversion_ratio(empty_db_conn):
    c = empty_db_conn
    _product(c, 2, 'สินค้าแพ็ค')
    _ps(c, 'shopee', 2, stock=100, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _uc(c, 2, 'แพ็ค', 5)
    _sale(c, 2, '2026-07-05', qty=2, unit='แพ็ค', customer='หน้าร้านS')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 2)
    # file_units 100, sold_since = 2 * ratio(5) = 10 -> est 90
    assert r['platforms']['shopee']['est'] == 90


def test_sr_return_docs_excluded_from_sold_since(empty_db_conn):
    c = empty_db_conn
    _product(c, 3, 'สินค้า SR test')
    _ps(c, 'shopee', 3, stock=50, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _sale(c, 3, '2026-07-05', qty=20, customer='หน้าร้านS', doc_no='SR12345')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 3)
    assert r['platforms']['shopee']['est'] == 50  # SR excluded -> no deduction


def test_sale_on_or_before_snapshot_date_not_deducted(empty_db_conn):
    c = empty_db_conn
    _product(c, 4, 'สินค้า boundary')
    _ps(c, 'shopee', 4, stock=30, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _sale(c, 4, '2026-07-01', qty=10, customer='หน้าร้านS')  # same day, strict > excludes it
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 4)
    assert r['platforms']['shopee']['est'] == 30


def test_estimate_clamped_at_zero_not_negative(empty_db_conn):
    c = empty_db_conn
    _product(c, 5, 'สินค้าขายเกินไฟล์')
    _ps(c, 'shopee', 5, stock=10, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _sale(c, 5, '2026-07-05', qty=999, customer='หน้าร้านS')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 5)
    assert r['platforms']['shopee']['est'] == 0


def test_null_stock_listing_treated_as_zero(empty_db_conn):
    c = empty_db_conn
    _product(c, 6, 'สินค้า stock NULL')
    _ps(c, 'shopee', 6, stock=None, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 6)
    assert r['platforms']['shopee']['est'] == 0
    assert r['platforms']['shopee']['listing_count'] == 1


def test_red_alert_is_per_platform(empty_db_conn):
    c = empty_db_conn
    _product(c, 7, 'สินค้า red เฉพาะ shopee')
    _stock(c, 7, 5)  # Sendy still has stock
    _ps(c, 'shopee', 7, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _ps(c, 'lazada', 7, stock=20, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 7)
    assert r['status'] == 'red'
    assert r['red_platforms'] == ['shopee']
    assert counts['red'] == 1


def test_no_red_when_sendy_also_out_of_stock(empty_db_conn):
    """RED requires true_available > 0 -- a listing at 0 while Sendy is ALSO
    at 0 is DEAD territory, not RED (nothing to restock the listing from)."""
    c = empty_db_conn
    _product(c, 8, 'สินค้าหมดทุกที่')
    _stock(c, 8, 0)
    _ps(c, 'shopee', 8, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 8)
    assert r['status'] != 'red'


def test_amber_when_combined_open_exceeds_true_available(empty_db_conn):
    c = empty_db_conn
    _product(c, 9, 'สินค้า amber')
    _stock(c, 9, 10)
    _ps(c, 'shopee', 9, stock=8, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _ps(c, 'lazada', 9, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 9)
    # combined_open = 8+5=13 > true_available 10 -> amber, excess 3
    assert r['status'] == 'amber'
    assert r['combined_open'] == 13
    assert r['amber_excess'] == 3
    assert counts['amber'] == 1


def test_dead_when_nothing_available_and_nothing_open(empty_db_conn):
    c = empty_db_conn
    _product(c, 11, 'สินค้าตาย')
    _stock(c, 11, 0)
    _ps(c, 'shopee', 11, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _ps(c, 'lazada', 11, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 11)
    assert r['status'] == 'dead'
    assert counts['dead'] == 1
    assert counts['red'] == 0
    assert counts['amber'] == 0


def test_pack_rescue_true_available_prevents_false_alert(empty_db_conn):
    """Mirrors the กลอน #360 shape from the plan: loose product has 0 own
    stock but is buildable from a pack via an active [แกะ] formula, so it
    must NOT alert even though its own stock_levels row is 0."""
    c = empty_db_conn
    _product(c, 20, 'แผง กลอน #360')
    _product(c, 21, 'ตัว กลอน #360')
    _stock(c, 20, 85)   # pack stock
    _stock(c, 21, 0)    # loose stock (own) is empty
    _formula(c, '[แกะ] กลอน #360', output_pid=21, output_qty=2, inputs=[(20, 1)])
    _ps(c, 'shopee', 21, stock=60, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview()
    r = _row(rows, 21)
    assert r['buildable'] == 170            # floor(85/1) * 2
    assert r['true_available'] == 170       # 0 + 170
    assert r['status'] == 'ok'


def test_search_filters_by_product_name(empty_db_conn):
    c = empty_db_conn
    _product(c, 30, 'กุญแจประตูบานเลื่อน')
    _product(c, 31, 'มือจับบัวเหล็ก')
    _ps(c, 'shopee', 30, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _ps(c, 'shopee', 31, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(search='กุญแจ')
    assert {r['product_id'] for r in rows} == {30}


def test_search_filters_by_sku_code(empty_db_conn):
    c = empty_db_conn
    _product(c, 32, 'สินค้า A', sku_code='ABC-123')
    _product(c, 33, 'สินค้า B', sku_code='XYZ-999')
    _ps(c, 'shopee', 32, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _ps(c, 'shopee', 33, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(search='abc')
    assert {r['product_id'] for r in rows} == {32}


def test_filter_by_status_red(empty_db_conn):
    c = empty_db_conn
    _product(c, 40, 'red product'); _stock(c, 40, 5)
    _ps(c, 'shopee', 40, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 41, 'ok product'); _stock(c, 41, 5)
    _ps(c, 'shopee', 41, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(flt='red')
    assert {r['product_id'] for r in rows} == {40}


def test_filter_by_platform(empty_db_conn):
    c = empty_db_conn
    _product(c, 42, 'shopee only'); _stock(c, 42, 5)
    _ps(c, 'shopee', 42, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 43, 'lazada only'); _stock(c, 43, 5)
    _ps(c, 'lazada', 43, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(flt='lazada')
    assert {r['product_id'] for r in rows} == {43}


def test_counts_reflect_full_set_independent_of_flt(empty_db_conn):
    c = empty_db_conn
    _product(c, 50, 'red'); _stock(c, 50, 5)
    _ps(c, 'shopee', 50, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 51, 'ok'); _stock(c, 51, 5)
    _ps(c, 'lazada', 51, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    _, _, counts = models.get_marketplace_overview(flt='red')
    assert counts['all'] == 2
    assert counts['red'] == 1
    assert counts['shopee'] == 1
    assert counts['lazada'] == 1


def test_pagination_slices_and_reports_total(empty_db_conn):
    c = empty_db_conn
    for pid in range(60, 65):
        _product(c, pid, f'สินค้า {pid}')
        _ps(c, 'shopee', pid, stock=1, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(page=1, per_page=2)
    assert total == 5
    assert len(rows) == 2
    rows2, total2, _ = models.get_marketplace_overview(page=3, per_page=2)
    assert len(rows2) == 1  # 5 rows, page 3 of size 2 -> 1 leftover


def test_sort_order_red_before_amber_before_ok_before_dead(empty_db_conn):
    c = empty_db_conn
    _product(c, 70, 'zzz dead'); _stock(c, 70, 0)
    _ps(c, 'shopee', 70, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 71, 'aaa ok'); _stock(c, 71, 100)
    _ps(c, 'shopee', 71, stock=1, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 72, 'mmm amber'); _stock(c, 72, 1)
    _ps(c, 'shopee', 72, stock=5, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _product(c, 73, 'bbb red'); _stock(c, 73, 5)
    _ps(c, 'shopee', 73, stock=0, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    c.commit()
    rows, total, counts = models.get_marketplace_overview(per_page=100)
    statuses_in_order = [r['status'] for r in rows]
    assert statuses_in_order == ['red', 'amber', 'ok', 'dead']


def test_no_listings_at_all_returns_empty(empty_db_conn):
    rows, total, counts = models.get_marketplace_overview()
    assert rows == [] and total == 0
    assert counts['all'] == 0


# ── get_marketplace_freshness ─────────────────────────────────────────────────

def test_freshness_snapshot_date_and_sales_through(empty_db_conn):
    c = empty_db_conn
    _product(c, 80, 'p')
    _ps(c, 'shopee', 80, stock=1, imported_at='2026-07-20 10:00:00')
    _sale(c, 80, '2026-07-25', qty=1, customer='หน้าร้านS')
    c.commit()
    fresh = models.get_marketplace_freshness()
    assert fresh['shopee']['snapshot_date'] == '2026-07-20'
    assert fresh['shopee']['sales_through'] == '2026-07-25'
    assert isinstance(fresh['shopee']['days_old'], int)


def test_freshness_platform_with_no_data_returns_none(empty_db_conn):
    fresh = models.get_marketplace_freshness()
    assert fresh['tiktok']['snapshot_date'] is None
    assert fresh['tiktok']['days_old'] is None
    assert fresh['tiktok']['sales_through'] is None


# ── get_unmapped_counts / get_unmapped_rows ───────────────────────────────────

def test_unmapped_counts_platform_skus_and_listings(empty_db_conn):
    c = empty_db_conn
    _ps(c, 'shopee', None, stock=1, product_name='ยังไม่ผูก')
    c.execute(
        "INSERT INTO ecommerce_listings (platform, item_name, listing_key) "
        "VALUES ('lazada', 'ไม่ผูกเช่นกัน', 'lk1')"
    )
    c.commit()
    counts = models.get_unmapped_counts()
    assert counts['platform_skus'] == 1
    assert counts['ecommerce_listings'] == 1


def test_unmapped_rows_drilldown(empty_db_conn):
    c = empty_db_conn
    _ps(c, 'shopee', None, stock=1, product_name='ยังไม่ผูก')
    c.commit()
    rows = models.get_unmapped_rows()
    assert len(rows) == 1
    assert rows[0]['source'] == 'platform_skus'
    assert rows[0]['name'] == 'ยังไม่ผูก'


# ── get_product_marketplace_detail ────────────────────────────────────────────

def test_product_detail_returns_none_for_missing_product(empty_db_conn):
    assert models.get_product_marketplace_detail(99999) is None


def test_product_detail_groups_items_and_itemless_bucket(empty_db_conn):
    c = empty_db_conn
    _product(c, 90, 'กุญแจประตู', sku_code='LCK-1')
    _stock(c, 90, 1)
    _ps(c, 'shopee', 90, product_id_str='PID90', stock=5, product_name='listing หลัก')
    c.execute(
        "INSERT INTO platform_products (platform, product_id_str, description) "
        "VALUES ('shopee', 'PID90', 'รายละเอียดสินค้า')"
    )
    # item-less stub (propagated freebie-bundle row): product_id_str = ''
    _ps(c, 'shopee', 90, product_id_str='', stock=None, product_name='ของแถม')
    c.commit()
    detail = models.get_product_marketplace_detail(90)
    assert detail['product']['product_name'] == 'กุญแจประตู'
    assert detail['product']['stock'] == 1
    shopee_items = detail['platforms']['shopee']['items']
    assert len(shopee_items) == 2
    named_item = next(g for g in shopee_items if g['item'] is not None)
    assert named_item['item']['description'] == 'รายละเอียดสินค้า'
    itemless = next(g for g in shopee_items if g['item'] is None)
    assert len(itemless['skus']) == 1
    assert itemless['skus'][0]['product_name'] == 'ของแถม'


def test_product_detail_sold_since_per_platform(empty_db_conn):
    c = empty_db_conn
    _product(c, 91, 'สินค้า detail sold_since')
    _stock(c, 91, 10)
    _ps(c, 'shopee', 91, stock=50, qty_per_sale=1, imported_at='2026-07-01 00:00:00')
    _sale(c, 91, '2026-07-05', qty=4, customer='หน้าร้านS')
    c.commit()
    detail = models.get_product_marketplace_detail(91)
    assert detail['platforms']['shopee']['sold_since'] == 4
    assert detail['platforms']['lazada']['sold_since'] == 0


# ── independent-oracle spot-check against the live local DB (skips cleanly) ──

def test_pid22_true_available_matches_stock_plus_buildable_on_live_db(tmp_db):
    """Per plan's verified facts: pid 22 has a [แกะ] formula (แผง pid 26 x2).
    Assert the FORMULA holds (stock + buildable), not a literal number that
    will drift as real inventory moves."""
    detail = models.get_product_marketplace_detail(22)
    if detail is None:
        pytest.skip("pid 22 not present in this DB snapshot")
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    stock_row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=22").fetchone()
    stock = stock_row['quantity'] if stock_row else 0
    conn.close()
    buildable_map = models.get_buildable([22])
    buildable = buildable_map[22]['buildable'] if 22 in buildable_map else 0
    assert detail['product']['true_available'] == stock + buildable
