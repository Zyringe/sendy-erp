"""Zero-rate tiers must not collect per-product / per-brand overrides.

Tier C is the "รอตัดสินใจ" placeholder — its description says
"placeholder — Put ยังไม่ได้คิดอัตรา commission, ทุกอย่าง 0%". The per-invoice
path already enforces that (`commission.py`, `tier_pays_commission`):

    # If the salesperson's tier is a zero-rate placeholder (Tier C — TBD),
    # they earn no commission at all. Per-product / per-brand overrides
    # are NOT meant to leak commission to those sps.

`get_commission_for_month` did NOT, adding `override_inv[inv_key]`
unconditionally — while its own comment claimed the result "always equals what
the user pays when ticking every invoice on the drill-down". For a Tier-C rep it
did not: the drilldown said ฿0.00 and the aggregate said otherwise.

Live impact when found (2026-07-31): ฿7,044.06 across 2026 for reps 02 and 03,
visible on BOTH the /commission summary and the per-salesperson drilldown
header, which reads the same aggregate.

This is a bug against a documented contract, not a policy change: the rule was
already decided and already implemented on the path that governs payment.
Whether Tier-C reps SHOULD earn overrides remains Put's call — changing that
means giving them a real tier, not leaking through a placeholder.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


def _seed(conn, rate_own, rate_third, threshold=None):
    """One rep, one invoice of 100 units of a product carrying a ฿5/unit
    override. With a paying tier the override yields 500.00; with a zero-rate
    placeholder it must yield 0.00."""
    conn.execute("INSERT INTO brands (id, code, name, is_own_brand) VALUES (1,'T','ทดสอบ',1)")
    conn.execute("INSERT INTO products (id, product_name, brand_id) VALUES (1,'แผ่นตัดทดสอบ',1)")
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('02','ทดสอบ 02')")
    conn.execute(
        "INSERT INTO commission_tiers (id, code, name_th, rate_own_pct, rate_third_pct, "
        "threshold_amount) VALUES (1,'X','เทียร์ทดสอบ',?,?,?)",
        (rate_own, rate_third, threshold))
    conn.execute("INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
                 "VALUES ('02',1,'2024-01-01')")
    conn.execute(
        "INSERT INTO commission_overrides (product_id, fixed_per_unit, apply_when_price_gt, "
        "apply_when_price_lte, is_active, effective_from) VALUES (1,5.0,0.0,200.0,1,'2024-01-01')")
    conn.execute("INSERT INTO customers (code, name, salesperson) VALUES ('C1','ลูกค้า','02')")
    conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) "
        "VALUES (1,'RE-1','2026-06-05','ลูกค้า','02',0)")
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (1,'IV-1','IV',10000.0)")
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit, unit_price, net, total) VALUES "
        "('2026-06-01','IV-1-1','IV-1',1,'ลูกค้า','C1',100,'ตัว',100.0,10000.0,10000.0)")
    conn.commit()


def test_zero_rate_tier_earns_nothing_from_a_product_override(empty_db, empty_db_conn):
    _seed(empty_db_conn, rate_own=0.0, rate_third=0.0)   # the Tier-C shape
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    r = next(x for x in rows if x["salesperson_code"] == "02")
    assert r["total_net"] == 10000.00, "revenue is still attributed"
    assert r["total_commission"] == 0.00, (
        f"zero-rate tier must earn 0, got {r['total_commission']} — the ฿5/unit "
        f"override leaked through the placeholder tier")


def test_aggregate_equals_the_drilldown_for_a_zero_rate_tier(empty_db, empty_db_conn):
    """The aggregate's own comment promises it 'always equals what the user pays
    when ticking every invoice on the drill-down'. Hold it to that."""
    _seed(empty_db_conn, rate_own=0.0, rate_third=0.0)
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    aggregate = next(x for x in rows if x["salesperson_code"] == "02")["total_commission"]
    per_invoice = round(sum(
        i["commission_due"] for i in
        commission.get_invoice_commission_for_sp("2026-06", "02", db_path=empty_db)), 2)
    assert aggregate == per_invoice == 0.00, (
        f"aggregate {aggregate} != drilldown {per_invoice}")


def test_a_paying_tier_still_gets_its_override(empty_db, empty_db_conn):
    """Guard against over-correcting: a real tier must keep its overrides."""
    _seed(empty_db_conn, rate_own=10.0, rate_third=5.0)
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    r = next(x for x in rows if x["salesperson_code"] == "02")
    assert r["total_commission"] == 500.00, (
        f"100 units x ฿5 override = 500.00, got {r['total_commission']} — the "
        f"fix must not strip overrides from tiers that DO pay")


def test_threshold_only_tier_counts_as_paying(empty_db, empty_db_conn):
    """A tier with 0% base rates but a real threshold still pays (mirrors
    `tier_pays_commission` in the per-invoice path) — it must keep overrides."""
    _seed(empty_db_conn, rate_own=0.0, rate_third=0.0, threshold=50000.0)
    import commission

    rows = commission.get_commission_for_month("2026-06", db_path=empty_db)
    r = next(x for x in rows if x["salesperson_code"] == "02")
    assert r["total_commission"] == 500.00, (
        "a threshold-bearing tier is a paying tier; overrides must survive")
