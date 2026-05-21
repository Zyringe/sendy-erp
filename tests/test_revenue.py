"""Tests for inventory_app/revenue.py — Phase 3 Revenue dashboard logic.

Synthetic data only (built on empty_db_conn schema clone). Mirrors the style
of tests/test_cashflow.py.

Key rules under test
────────────────────
revenue_summary:
  - excludes doc_base IS NULL, doc_base LIKE 'SR%', doc_base LIKE 'HS%'
  - AOV = total_revenue / total_invoices, 0 when no invoices
  - total_customers groups by COALESCE(customer_code, customer)
  - date_iso filters

top_customers_by_revenue:
  - groups by COALESCE(customer_code, customer) so rows missing a code on
    one line don't split the bucket
  - sorted by revenue DESC, capped by `limit`
  - revenue == Σ net for matching rows

top_brands_by_revenue:
  - LEFT JOIN products → brands; rows with brand_id IS NULL collapse to
    a single 'ไม่ระบุแบรนด์' bucket
  - brand_display prefers name_th, falls back to name
  - Σ top_brands.revenue (large limit) == revenue_summary.total_revenue
"""
import pytest
import sqlite3

import revenue as rev


# ── synthetic data helpers ───────────────────────────────────────────────────

def _ins_sale(conn, doc_base, customer, customer_code, date_iso, net,
              line=1, vat_type=1, product_id=None, product_name_raw=None):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net,
            product_id, product_name_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-{line}", doc_base, customer, customer_code,
         1, 'ตัว', net, vat_type, net, net,
         product_id, product_name_raw),
    )


def _ins_brand(conn, code, name, name_th=None, is_own_brand=0):
    cur = conn.execute(
        """INSERT INTO brands (code, name, name_th, is_own_brand)
           VALUES (?,?,?,?)""",
        (code, name, name_th, is_own_brand),
    )
    return cur.lastrowid


def _ins_product(conn, sku, product_name, brand_id=None, unit_type='ตัว'):
    cur = conn.execute(
        """INSERT INTO products (sku, product_name, brand_id, unit_type)
           VALUES (?,?,?,?)""",
        (sku, product_name, brand_id, unit_type),
    )
    return cur.lastrowid


# ── revenue_summary ──────────────────────────────────────────────────────────

def test_summary_empty_db_returns_zeros(empty_db_conn):
    c = empty_db_conn
    s = rev.revenue_summary(conn=c)
    assert s == {
        'total_revenue':   0.0,
        'total_invoices':  0,
        'total_customers': 0,
        'aov':             0.0,
    }


def test_summary_excludes_sr_hs_and_null_docbase(empty_db_conn):
    c = empty_db_conn
    # 2 IV lines on one invoice + 1 SR + 1 HS + 1 row with NULL doc_base
    _ins_sale(c, 'IV001', 'ลูกค้า A', 'C001', '2026-01-10', 100, line=1)
    _ins_sale(c, 'IV001', 'ลูกค้า A', 'C001', '2026-01-10', 50, line=2)
    _ins_sale(c, 'SR001', 'ลูกค้า A', 'C001', '2026-01-11', -30)
    _ins_sale(c, 'HS001', 'ลูกค้า A', 'C001', '2026-01-12', 999)
    c.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES ('2026-01-13','no-base',NULL,'ลูกค้า A','C001',
                   1,'ตัว',77,1,77,77)"""
    )
    c.commit()

    s = rev.revenue_summary(conn=c)
    assert s['total_revenue'] == 150.0
    assert s['total_invoices'] == 1     # only IV001
    assert s['total_customers'] == 1


def test_summary_aov_division_safe(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 200)
    _ins_sale(c, 'IV002', 'B', 'C002', '2026-01-11', 400)
    c.commit()

    s = rev.revenue_summary(conn=c)
    assert s['total_revenue']  == 600.0
    assert s['total_invoices'] == 2
    assert s['aov']            == 300.0


def test_summary_date_filter(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV001', 'A', 'C001', '2025-12-31', 100)
    _ins_sale(c, 'IV002', 'A', 'C001', '2026-01-01', 200)
    _ins_sale(c, 'IV003', 'A', 'C001', '2026-01-31', 300)
    _ins_sale(c, 'IV004', 'A', 'C001', '2026-02-01', 400)
    c.commit()

    s = rev.revenue_summary(date_from='2026-01-01', date_to='2026-01-31', conn=c)
    assert s['total_revenue']  == 500.0
    assert s['total_invoices'] == 2


# ── top_customers_by_revenue ────────────────────────────────────────────────

def test_top_customers_groups_by_code_when_name_varies(empty_db_conn):
    c = empty_db_conn
    # Same code C001 on both rows, slightly different name spellings
    _ins_sale(c, 'IV001', 'ลูกค้า เอ',  'C001', '2026-01-10', 100)
    _ins_sale(c, 'IV002', 'ลูกค้า เอ ', 'C001', '2026-01-11', 200)
    _ins_sale(c, 'IV003', 'ลูกค้า บี',  'C002', '2026-01-12', 500)
    c.commit()

    rows = rev.top_customers_by_revenue(conn=c, limit=20)
    assert len(rows) == 2
    # Sorted by revenue DESC
    assert rows[0]['customer_code'] == 'C002'
    assert rows[0]['revenue']       == 500.0
    assert rows[0]['invoice_count'] == 1
    assert rows[1]['customer_code'] == 'C001'
    assert rows[1]['revenue']       == 300.0
    assert rows[1]['invoice_count'] == 2


def test_top_customers_respects_limit_and_order(empty_db_conn):
    c = empty_db_conn
    for i in range(5):
        _ins_sale(c, f'IV{i:03d}', f'cust{i}', f'C{i:03d}',
                  '2026-01-10', (i + 1) * 100)
    c.commit()

    rows = rev.top_customers_by_revenue(conn=c, limit=3)
    assert len(rows) == 3
    assert [r['customer_code'] for r in rows] == ['C004', 'C003', 'C002']
    assert [r['revenue'] for r in rows] == [500.0, 400.0, 300.0]


def test_top_customers_handles_missing_code_via_name(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV001', 'ลูกค้า X', None, '2026-01-10', 100)
    _ins_sale(c, 'IV002', 'ลูกค้า X', '',   '2026-01-11', 200)  # empty == NULL via NULLIF
    _ins_sale(c, 'IV003', 'ลูกค้า Y', None, '2026-01-12', 50)
    c.commit()

    rows = rev.top_customers_by_revenue(conn=c, limit=20)
    by_name = {r['customer']: r for r in rows}
    assert by_name['ลูกค้า X']['revenue']       == 300.0
    assert by_name['ลูกค้า X']['invoice_count'] == 2
    assert by_name['ลูกค้า Y']['revenue']       == 50.0


# ── top_brands_by_revenue ────────────────────────────────────────────────────

def test_top_brands_unbranded_collapses_to_one_bucket(empty_db_conn):
    c = empty_db_conn
    bid = _ins_brand(c, 'sendai', 'Sendai', name_th='เซ็นได', is_own_brand=1)
    pid_branded = _ins_product(c, 90001, 'P1', brand_id=bid)
    pid_no_brand = _ins_product(c, 90002, 'P2', brand_id=None)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 100, product_id=pid_branded)
    _ins_sale(c, 'IV002', 'A', 'C001', '2026-01-11', 200, product_id=pid_no_brand)
    _ins_sale(c, 'IV003', 'A', 'C001', '2026-01-12', 50, product_id=None)
    c.commit()

    rows = rev.top_brands_by_revenue(conn=c, limit=20)
    assert len(rows) == 2
    by = {r['brand_display']: r for r in rows}
    assert by['เซ็นได']['revenue']     == 100.0
    assert by['เซ็นได']['brand_code']  == 'sendai'
    assert by['เซ็นได']['line_count']  == 1
    assert by['ไม่ระบุแบรนด์']['revenue']     == 250.0
    assert by['ไม่ระบุแบรนด์']['brand_code']  is None
    assert by['ไม่ระบุแบรนด์']['line_count']  == 2


def test_top_brands_prefers_name_th_over_name(empty_db_conn):
    c = empty_db_conn
    b1 = _ins_brand(c, 'gl', 'Golden Lion', name_th='สิงห์ทอง')
    b2 = _ins_brand(c, 'aspec', 'A-SPEC', name_th=None)
    p1 = _ins_product(c, 90001, 'P1', brand_id=b1)
    p2 = _ins_product(c, 90002, 'P2', brand_id=b2)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 100, product_id=p1)
    _ins_sale(c, 'IV002', 'A', 'C001', '2026-01-11', 200, product_id=p2)
    c.commit()

    rows = rev.top_brands_by_revenue(conn=c, limit=20)
    displays = {r['brand_display'] for r in rows}
    assert 'สิงห์ทอง' in displays
    assert 'A-SPEC'   in displays


def test_top_brands_respects_limit_and_order(empty_db_conn):
    c = empty_db_conn
    pids = []
    for i in range(5):
        bid = _ins_brand(c, f'b{i}', f'Brand{i}')
        pids.append(_ins_product(c, 90000 + i, f'P{i}', brand_id=bid))
    for i, pid in enumerate(pids):
        _ins_sale(c, f'IV{i:03d}', 'A', 'C001', '2026-01-10',
                  (i + 1) * 100, product_id=pid)
    c.commit()

    rows = rev.top_brands_by_revenue(conn=c, limit=3)
    assert len(rows) == 3
    assert [r['revenue'] for r in rows] == [500.0, 400.0, 300.0]


def test_top_brands_excludes_sr_hs(empty_db_conn):
    c = empty_db_conn
    bid = _ins_brand(c, 'sendai', 'Sendai', name_th='เซ็นได')
    pid = _ins_product(c, 90001, 'P', brand_id=bid)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 100, product_id=pid)
    _ins_sale(c, 'SR001', 'A', 'C001', '2026-01-11', -30, product_id=pid)
    _ins_sale(c, 'HS001', 'A', 'C001', '2026-01-12', 999, product_id=pid)
    c.commit()

    rows = rev.top_brands_by_revenue(conn=c, limit=20)
    assert len(rows) == 1
    assert rows[0]['revenue']    == 100.0
    assert rows[0]['line_count'] == 1


def test_top_brands_date_filter(empty_db_conn):
    c = empty_db_conn
    bid = _ins_brand(c, 'sendai', 'Sendai', name_th='เซ็นได')
    pid = _ins_product(c, 90001, 'P', brand_id=bid)
    _ins_sale(c, 'IV001', 'A', 'C001', '2025-12-31', 100, product_id=pid)
    _ins_sale(c, 'IV002', 'A', 'C001', '2026-01-15', 200, product_id=pid)
    _ins_sale(c, 'IV003', 'A', 'C001', '2026-02-01', 400, product_id=pid)
    c.commit()

    rows = rev.top_brands_by_revenue(date_from='2026-01-01', date_to='2026-01-31',
                                     conn=c, limit=20)
    assert len(rows) == 1
    assert rows[0]['revenue'] == 200.0


# ── reconciliation ───────────────────────────────────────────────────────────

def test_sum_top_brands_matches_total_revenue(empty_db_conn):
    """Σ top_brands.revenue (large enough limit) == revenue_summary.total_revenue."""
    c = empty_db_conn
    b1 = _ins_brand(c, 'sendai', 'Sendai',      name_th='เซ็นได',   is_own_brand=1)
    b2 = _ins_brand(c, 'golden', 'Golden Lion', name_th='สิงห์ทอง', is_own_brand=1)
    p1 = _ins_product(c, 90001, 'P1', brand_id=b1)
    p2 = _ins_product(c, 90002, 'P2', brand_id=b2)
    p3 = _ins_product(c, 90003, 'P3', brand_id=None)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 100, product_id=p1)
    _ins_sale(c, 'IV002', 'B', 'C002', '2026-01-11', 200, product_id=p2)
    _ins_sale(c, 'IV003', 'A', 'C001', '2026-01-12', 300, product_id=p3)
    _ins_sale(c, 'IV004', 'C', 'C003', '2026-01-13', 400, product_id=None)
    c.commit()

    summary = rev.revenue_summary(conn=c)
    brand_rows = rev.top_brands_by_revenue(conn=c, limit=999)
    assert abs(sum(r['revenue'] for r in brand_rows) - summary['total_revenue']) < 0.01


def test_sum_top_customers_matches_total_revenue(empty_db_conn):
    c = empty_db_conn
    for i in range(7):
        _ins_sale(c, f'IV{i:03d}', f'cust{i}', f'C{i:03d}',
                  '2026-01-10', (i + 1) * 100)
    c.commit()

    summary = rev.revenue_summary(conn=c)
    cust_rows = rev.top_customers_by_revenue(conn=c, limit=999)
    assert abs(sum(r['revenue'] for r in cust_rows) - summary['total_revenue']) < 0.01


# ── unmapped_revenue_drilldown ───────────────────────────────────────────────

def _ins_sale_unmapped(conn, doc_base, customer_code, date_iso, net,
                       bsn_code, product_name_raw):
    """Sale with product_id NULL — the 'unmapped BSN code' case."""
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net,
            product_id, bsn_code, product_name_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-1", doc_base, 'cust', customer_code,
         1, 'ตัว', net, 1, net, net,
         None, bsn_code, product_name_raw),
    )


def test_drilldown_groups_unmapped_codes_by_bsn_and_raw_name(empty_db_conn):
    c = empty_db_conn
    # 2 unmapped sales for the same BSN code (same raw name) → 1 grouped row
    _ins_sale_unmapped(c, 'IV001', 'A', '2026-01-10', 100, '603ต2503-34', 'รีเวท')
    _ins_sale_unmapped(c, 'IV002', 'B', '2026-01-11', 250, '603ต2503-34', 'รีเวท')
    # different BSN code → separate row
    _ins_sale_unmapped(c, 'IV003', 'A', '2026-01-12', 50, '603ต2503-22', 'รีเวท สีน้ำตาล')
    c.commit()

    rows = rev.unmapped_revenue_drilldown(conn=c)
    by_code = {r['bsn_code']: r for r in rows}
    assert by_code['603ต2503-34']['revenue']            == 350.0
    assert by_code['603ต2503-34']['line_count']         == 2
    assert by_code['603ต2503-34']['distinct_customers'] == 2
    assert by_code['603ต2503-22']['revenue']            == 50.0


def test_drilldown_includes_no_brand_products(empty_db_conn):
    c = empty_db_conn
    p_nobrand = _ins_product(c, 90001, 'สินค้ายังไม่ระบุยี่ห้อ', brand_id=None)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 300, product_id=p_nobrand)
    _ins_sale(c, 'IV002', 'B', 'C002', '2026-01-11', 500, product_id=p_nobrand)
    c.commit()

    rows = rev.unmapped_revenue_drilldown(conn=c)
    by_pid = {r['product_id']: r for r in rows if r['product_id'] is not None}
    assert by_pid[p_nobrand]['revenue']     == 800.0
    assert by_pid[p_nobrand]['line_count']  == 2
    assert by_pid[p_nobrand]['source_type'] == 'no_brand'


def test_drilldown_excludes_branded_products(empty_db_conn):
    c = empty_db_conn
    bid = _ins_brand(c, 'sd', 'Sendai', name_th='เซ็นได')
    p_branded = _ins_product(c, 90001, 'P', brand_id=bid)
    _ins_sale(c, 'IV001', 'A', 'C001', '2026-01-10', 999, product_id=p_branded)
    c.commit()

    rows = rev.unmapped_revenue_drilldown(conn=c)
    assert rows == []


def test_drilldown_ordered_by_revenue_desc_capped_at_limit(empty_db_conn):
    c = empty_db_conn
    # 3 different unmapped codes with revenues 100, 300, 200
    _ins_sale_unmapped(c, 'IV001', 'A', '2026-01-10', 100, 'CODE-A', 'A')
    _ins_sale_unmapped(c, 'IV002', 'A', '2026-01-10', 300, 'CODE-B', 'B')
    _ins_sale_unmapped(c, 'IV003', 'A', '2026-01-10', 200, 'CODE-C', 'C')
    c.commit()

    rows = rev.unmapped_revenue_drilldown(conn=c, limit=2)
    assert len(rows) == 2
    assert [r['revenue'] for r in rows] == [300.0, 200.0]
    assert rows[0]['bsn_code'] == 'CODE-B'


def test_drilldown_excludes_sr_hs(empty_db_conn):
    c = empty_db_conn
    _ins_sale_unmapped(c, 'IV001', 'A', '2026-01-10', 100, 'CODE-A', 'name')
    _ins_sale_unmapped(c, 'SR001', 'A', '2026-01-11', -50, 'CODE-A', 'name')  # CN
    _ins_sale_unmapped(c, 'HS001', 'A', '2026-01-12', 999, 'CODE-A', 'name')  # opening
    c.commit()
    rows = rev.unmapped_revenue_drilldown(conn=c)
    assert rows[0]['revenue'] == 100.0  # SR + HS filtered out


def test_drilldown_date_filter(empty_db_conn):
    c = empty_db_conn
    _ins_sale_unmapped(c, 'IV001', 'A', '2025-12-31', 100, 'CODE-A', 'name')
    _ins_sale_unmapped(c, 'IV002', 'A', '2026-01-15', 200, 'CODE-A', 'name')
    _ins_sale_unmapped(c, 'IV003', 'A', '2026-02-01', 300, 'CODE-A', 'name')
    c.commit()
    rows = rev.unmapped_revenue_drilldown(
        date_from='2026-01-01', date_to='2026-01-31', conn=c)
    assert rows[0]['revenue'] == 200.0


def test_drilldown_sum_matches_unbranded_bucket(empty_db_conn):
    """Σ unmapped_revenue_drilldown.revenue == top_brands 'ไม่ระบุแบรนด์' bucket."""
    c = empty_db_conn
    # mix of unmapped codes + no-brand products
    _ins_sale_unmapped(c, 'IV001', 'A', '2026-01-10', 100, 'CODE-A', 'A')
    _ins_sale_unmapped(c, 'IV002', 'A', '2026-01-11', 200, 'CODE-B', 'B')
    p_nb = _ins_product(c, 90001, 'P', brand_id=None)
    _ins_sale(c, 'IV003', 'A', 'C', '2026-01-12', 300, product_id=p_nb)
    c.commit()

    drill_total = sum(r['revenue'] for r in rev.unmapped_revenue_drilldown(conn=c))
    brand_rows = rev.top_brands_by_revenue(conn=c, limit=999)
    unbranded = next(r for r in brand_rows if r['brand_display'] == 'ไม่ระบุแบรนด์')
    assert abs(drill_total - unbranded['revenue']) < 0.01
