"""#514 — an HS document in the BSN5657 book is a cash sale (ขายสด), real
revenue and real price evidence. Not an "opening balance" (that reading was
an assumption and was wrong — see the issue).

Covers the ONE shared definition (`sales_filters.revenue_filter`, which
`price_lookup.price_evidence_filter` / `purchase_population_filter` are both
built directly on top of) and the one
revenue surface that hand-types its own copy of the SR/HS exclusion instead
of importing the shared filter (`models/sales.py::get_trade_dashboard`,
/trade-dashboard).

Sites that KEEP excluding HS on purpose (AR/settlement — a cash sale is paid
on the spot, so it is never a receivable) are covered where they already
live: `tests/test_payments_alloc.py::test_hs_cash_sale_never_appears_in_settlement`,
`tests/test_bp_mobile_routes.py::test_sales_trip_outstanding_still_excludes_hs_cash_sale`.
`tests/test_ar_diagnostic.py::test_sr_and_hs_docs_are_classified_because_derived_excludes_them`
already pins that payments_alloc side, unchanged.
"""
import sqlite3

import pytest

import sales_filters
import models


# ── sales_filters.revenue_filter — the shared definition ────────────────────

def test_revenue_filter_no_longer_excludes_hs():
    f = sales_filters.revenue_filter()
    assert "NOT LIKE 'HS%'" not in f, 'HS must count as revenue now (#514)'
    # Controls — the OTHER two exclusions must survive untouched.
    assert "NOT LIKE 'SR%'" in f
    assert 'excludes_revenue = 1' in f


def test_revenue_filter_keeps_hs_out_only_via_sr_or_writeoff(empty_db_conn):
    """DB-level control, independent of the string check above: a bare HS
    line must pass the filter (be counted), an SR line and a written-off line
    must not."""
    c = empty_db_conn
    c.execute("""INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, customer, customer_code,
                    qty, unit, unit_price, vat_type, total, net)
                 VALUES ('2026-06-10','HS9001-1','HS9001','A','C01',
                         1,'ตัว',100,1,100,100)""")
    c.execute("""INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, customer, customer_code,
                    qty, unit, unit_price, vat_type, total, net)
                 VALUES ('2026-06-10','SR9002-1','SR9002','A','C01',
                         1,'ตัว',50,1,50,50)""")
    c.execute("""INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, customer, customer_code,
                    qty, unit, unit_price, vat_type, total, net)
                 VALUES ('2026-06-10','IV9003-1','IV9003','A','C01',
                         1,'ตัว',300,1,300,300)""")
    c.execute("INSERT INTO ar_writeoffs (doc_no, customer_code, amount, type, "
              "writeoff_date, excludes_revenue) VALUES ('IV9003','C01',300,'expense',"
              "'2026-06-11',1)")
    c.commit()

    rows = c.execute(
        f"SELECT doc_base FROM sales_transactions WHERE {sales_filters.revenue_filter()}"
    ).fetchall()
    kept = {r['doc_base'] for r in rows}
    assert kept == {'HS9001'}, 'HS counted; SR and the written-off IV must not be'


# ── /trade-dashboard (models/sales.py::get_trade_dashboard) ─────────────────

def _ins_sale(conn, doc_base, customer, date_iso, net, line=1):
    conn.execute(
        """INSERT INTO sales_transactions
           (date_iso, doc_no, doc_base, customer, customer_code,
            qty, unit, unit_price, vat_type, total, net)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date_iso, f"{doc_base}-{line}", doc_base, customer, 'C01',
         1, 'ตัว', net, 1, net, net),
    )


def test_trade_dashboard_summary_counts_hs(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV5001', 'ลูกค้า514', '2026-06-10', 1000)   # control
    _ins_sale(c, 'SR5001', 'ลูกค้า514', '2026-06-11', 200)    # control: subtracted (#627)
    _ins_sale(c, 'HS5001', 'ลูกค้า514', '2026-06-12', 300)    # counted now
    c.commit()

    d = models.get_trade_dashboard('2026-06-01', '2026-06-30', conn=c)
    assert d['sales']['total_net'] == 1100.0   # 1,000 − 200 + 300
    assert d['sales']['doc_count'] == 2   # IV5001 + HS5001; a credit note is not an invoice


def test_trade_dashboard_weekly_trend_counts_hs(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV5002', 'ลูกค้า514', '2026-06-10', 1000)
    _ins_sale(c, 'HS5002', 'ลูกค้า514', '2026-06-10', 300)
    c.commit()

    d = models.get_trade_dashboard('2026-06-01', '2026-06-30', conn=c)
    total_weekly_sales = sum(w['sales'] for w in d['weekly_trend'])
    assert total_weekly_sales == 1300.0


def test_trade_dashboard_top_products_counts_hs(empty_db_conn):
    c = empty_db_conn
    pid_cur = c.execute(
        "INSERT INTO products (product_name, unit_type) VALUES ('สินค้า514', 'ตัว')")
    pid = pid_cur.lastrowid
    c.execute("""INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, customer, customer_code,
                    qty, unit, unit_price, vat_type, total, net, product_id)
                 VALUES ('2026-06-10','IV5003-1','IV5003','ลูกค้า514','C01',
                         1,'ตัว',1000,1,1000,1000,?)""", (pid,))
    c.execute("""INSERT INTO sales_transactions
                   (date_iso, doc_no, doc_base, customer, customer_code,
                    qty, unit, unit_price, vat_type, total, net, product_id)
                 VALUES ('2026-06-11','HS5003-1','HS5003','ลูกค้า514','C01',
                         1,'ตัว',300,1,300,300,?)""", (pid,))
    c.commit()

    d = models.get_trade_dashboard('2026-06-01', '2026-06-30', conn=c)
    top = {p['product_id']: p['total_net'] for p in d['top_products']}
    assert top[pid] == 1300.0


def test_trade_dashboard_top_customers_counts_hs(empty_db_conn):
    c = empty_db_conn
    _ins_sale(c, 'IV5004', 'ลูกค้า514ก', '2026-06-10', 1000)
    _ins_sale(c, 'HS5004', 'ลูกค้า514ก', '2026-06-11', 300)
    c.commit()

    d = models.get_trade_dashboard('2026-06-01', '2026-06-30', conn=c)
    top = {t['customer']: t['total_net'] for t in d['top_customers']}
    assert top['ลูกค้า514ก'] == 1300.0
