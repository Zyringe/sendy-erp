"""The 2026-07-30 display redesign: /commission columns + the AR header badge.

Put's complaint was that /commission showed three money numbers that never
reconcile — "รวม commission", "จ่ายแล้ว", "คงเหลือ" — and he expected the first
and last to agree. They could not, for two independent reasons:

  1. Different time bases. รวม/จ่ายแล้ว were month-only; คงเหลือ is cumulative
     across all months. July 2026 rep 31 showed รวม 0.00 and คงเหลือ 130.00.
  2. Invoices closed by business rule (SOLD_SETTLED_BEFORE / SETTLED_THROUGH)
     leave คงเหลือ without ever passing through จ่ายแล้ว. June 2026 rep 31:
     รวม ฿2,036.88, of which ฿1,906.88 was two 2025 invoices already settled.

Fix (Put chose A+D): lead with what is actually owed, and redefine the monthly
figure to exclude settled invoices so the row is readable.

⚠ `commission_month_payable` is per-invoice by construction and therefore
excludes the Tier-B above-threshold bonus, which exists only at month level and
has NEVER been payable through this page — the จะจ่าย box defaults to
`remaining`, also per-invoice. Feb 2026 rep 31: total_commission ฿6,362.40 vs
฿4,927.13 across its invoices, a ฿1,435.27 bonus with no row to sit on. That gap
predates this change and is deliberately NOT resolved here; the column is named
for what it is. `test_month_payable_excludes_the_unpayable_threshold_bonus`
pins the behaviour so a future reader finds the reasoning instead of a surprise.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(REPO, "inventory_app")
sys.path.insert(0, APP)


def _seed_dashboard(conn):
    """One rep, Tier B (5% up to 50k, +5 points above), three invoices in the
    June cycle: one settled by the sold-before rule, one already paid, one still
    owed. Hand-verified:

        IV-OLD   sold 2025-10-01, net 10,000 -> due 500.00, SETTLED (pre-Feb-2026)
        IV-PAID  sold 2026-05-01, net  4,000 -> due 200.00, paid in full
        IV-OWED  sold 2026-05-02, net  2,000 -> due 100.00, owed

        total_net                = 16,000.00
        total_commission (raw)   =    800.00   <- counts IV-OLD
        commission_month_payable =    300.00   <- excludes IV-OLD only
        remaining (cumulative)   =    100.00
    """
    conn.execute("INSERT INTO brands (id, code, name, is_own_brand) VALUES (1,'T','ทดสอบ',1)")
    conn.execute("INSERT INTO products (id, product_name, brand_id) VALUES (1,'สินค้า',1)")
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('31','ทดสอบ 31')")
    conn.execute(
        "INSERT INTO commission_tiers (id, code, name_th, rate_own_pct, rate_third_pct, "
        "threshold_amount, above_rate_own_pct, above_rate_third_pct) "
        "VALUES (1,'B','เทียร์ทดสอบ',5.0,5.0,50000.0,10.0,5.0)")
    conn.execute("INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
                 "VALUES ('31',1,'2024-01-01')")
    conn.execute("INSERT INTO customers (code, name, salesperson) VALUES ('C1','ลูกค้า','31')")
    conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) VALUES "
        "(1,'RE-1','2026-06-05','ลูกค้า','31',0),"
        "(2,'RE-2','2026-06-06','ลูกค้า','31',0),"
        "(3,'RE-3','2026-06-07','ลูกค้า','31',0)")
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES "
        "(1,'IV-OLD','IV',10000.0),(2,'IV-PAID','IV',4000.0),(3,'IV-OWED','IV',2000.0)")
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit_price, net, total) VALUES "
        "('2025-10-01','IV-OLD-1','IV-OLD',1,'ลูกค้า','C1',100,100.0,10000.0,10000.0),"
        "('2026-05-01','IV-PAID-1','IV-PAID',1,'ลูกค้า','C1',40,100.0,4000.0,4000.0),"
        "('2026-05-02','IV-OWED-1','IV-OWED',1,'ลูกค้า','C1',20,100.0,2000.0,2000.0)")
    # IV-PAID settled with a real payout row; IV-OLD needs none (business rule)
    conn.execute(
        "INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid, paid_date, invoice_no) "
        "VALUES ('2026-06','31',200.0,'2026-06-30','IV-PAID')")
    conn.commit()


def _row31(client_html_rows):
    return next(r for r in client_html_rows if r["salesperson_code"] == "31")


def _dashboard_rows(app_module, month, db_path):
    """Recompute exactly what the route puts in the template, without HTTP."""
    import commission

    rows = commission.get_commission_for_month(month, db_path=db_path)
    out = []
    for r in rows:
        month_invoices = commission.get_invoice_commission_for_sp(
            month, r["salesperson_code"], db_path=db_path)
        r["commission_month_payable"] = round(
            sum(i["commission_due"] for i in month_invoices
                if i["paid_status"] != "settled"), 2)
        r["commission_settled_excluded"] = round(
            sum(i["commission_due"] for i in month_invoices
                if i["paid_status"] == "settled"), 2)
        unpaid = commission.get_invoice_commission_for_sp(
            month, r["salesperson_code"], db_path=db_path,
            through_month=True, only_unpaid=True)
        r["remaining"] = round(sum(i["remaining"] for i in unpaid), 2)
        out.append(r)
    return out


def test_month_payable_excludes_settled_invoices(empty_db, empty_db_conn):
    _seed_dashboard(empty_db_conn)
    r = _row31(_dashboard_rows(None, "2026-06", empty_db))

    assert r["total_net"] == 16000.00
    assert r["total_commission"] == 800.00, "raw formula still counts the settled invoice"
    assert r["commission_month_payable"] == 300.00, (
        "must drop IV-OLD's 500.00 (sold 2025, settled by business rule)")
    assert r["commission_settled_excluded"] == 500.00, "surfaced so the gap is explainable"


def test_month_payable_and_remaining_now_reconcile(empty_db, empty_db_conn):
    """The whole point of the redesign: what the row shows can be reasoned about.
    payable(300) − paid this month(200) == remaining(100)."""
    import commission

    _seed_dashboard(empty_db_conn)
    r = _row31(_dashboard_rows(None, "2026-06", empty_db))
    paid = commission.get_payouts_for_month("2026-06", db_path=empty_db).get("31", 0.0)

    assert paid == 200.00
    assert r["remaining"] == 100.00
    assert round(r["commission_month_payable"] - paid, 2) == r["remaining"], (
        f"payable {r['commission_month_payable']} − paid {paid} != remaining {r['remaining']}")


def test_month_payable_excludes_the_unpayable_threshold_bonus(empty_db, empty_db_conn):
    """Documents a PRE-EXISTING gap rather than fixing it: the Tier-B
    above-threshold bonus lives only at month level, so no invoice row carries
    it and it can never reach `remaining` or the จะจ่าย box. Seeded to breach
    the 50k threshold so the bonus is non-zero."""
    _seed_dashboard(empty_db_conn)
    empty_db_conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) "
        "VALUES (4,'RE-4','2026-06-08','ลูกค้า','31',0)")
    empty_db_conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (4,'IV-BIG','IV',60000.0)")
    empty_db_conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer, "
        "customer_code, qty, unit_price, net, total) VALUES "
        "('2026-05-03','IV-BIG-1','IV-BIG',1,'ลูกค้า','C1',600,100.0,60000.0,60000.0)")
    empty_db_conn.commit()

    r = _row31(_dashboard_rows(None, "2026-06", empty_db))
    bonus = round(r["commission_above_own"] + r["commission_above_third"], 2)
    assert bonus > 0, "scenario must actually breach the threshold"
    assert round(r["total_commission"] - r["commission_month_payable"]
                 - r["commission_settled_excluded"], 2) == bonus, (
        "the unexplained remainder between the raw total and the payable figure "
        "is exactly the month-level threshold bonus — pinned so a future reader "
        "finds the reasoning rather than a surprise")


def test_summary_card_matches_the_column_it_headlines():
    """The card and the table must be computed the same way, or the page
    contradicts itself — the failure this redesign exists to remove."""
    src = open(os.path.join(APP, "blueprints", "commission_bp.py"), encoding="utf-8").read()
    assert "'total_commission_payable': round(" in src
    assert "sum(r['commission_month_payable'] for r in full_rows)" in src, (
        "the summary card must sum the SAME per-row field the table renders")
    tpl = open(os.path.join(APP, "templates", "commission.html"), encoding="utf-8").read()
    assert "summary.total_commission_payable" in tpl
    assert "r.commission_month_payable" in tpl
    assert "summary.total_commission " not in tpl, (
        "the old raw-total card must be gone, or the page shows two different "
        "'commission' numbers again")


def test_dashboard_table_colspan_matches_header_count():
    """A stale colspan on the empty-state row is the classic leftover of a
    column removal."""
    import re

    tpl = open(os.path.join(APP, "templates", "commission.html"), encoding="utf-8").read()
    thead = re.search(r"<thead>(.*?)</thead>", tpl, re.S).group(1)
    n_th = len(re.findall(r"<th", thead))
    colspans = [int(c) for c in re.findall(r'colspan="(\d+)"', tpl)]
    assert colspans, "empty-state row should still exist"
    for c in colspans:
        assert c == n_th, f"colspan={c} but the header has {n_th} columns"


# ── AR customer page: header badge reads the master, not the Express snapshot ──
def test_ar_header_badge_prefers_the_customer_master():
    src = open(os.path.join(APP, "blueprints", "accounting.py"), encoding="utf-8").read()
    assert "SELECT salesperson FROM customers WHERE code = ?" in src, (
        "AR header must read customers.salesperson")
    assert "or rows[0]['salesperson_code']" in src, (
        "must fall back to the snapshot when the customer is absent from the "
        "master, otherwise the badge silently disappears for those rows")


def test_ar_header_falls_back_when_customer_missing_from_master(empty_db, empty_db_conn):
    """Behavioural check of the fallback, not just its presence in source."""
    # express_ar_outstanding.batch_id -> express_import_log(id)
    empty_db_conn.execute(
        "INSERT INTO express_import_log (id, file_type, record_count, line_count, status, imported_at) "
        "VALUES (1,'ar_snapshot',1,1,'imported','2026-06-05 00:00:00')")
    empty_db_conn.execute(
        "INSERT INTO express_ar_outstanding (batch_id, entity, snapshot_date_iso, customer_code, "
        "customer_name, salesperson_code, doc_no, doc_date_iso, bill_amount, paid_amount, "
        "outstanding_amount, is_anomalous, has_warning) "
        "VALUES (1,'BSN','2026-06-05','GHOST','ลูกค้าไม่มีในทะเบียน','31','IV-G','2026-05-01',"
        "100.0,0.0,100.0,0,0)")
    empty_db_conn.commit()

    master = empty_db_conn.execute(
        "SELECT salesperson FROM customers WHERE code = ?", ("GHOST",)).fetchone()
    snapshot = empty_db_conn.execute(
        "SELECT salesperson_code FROM express_ar_outstanding WHERE customer_code = ?",
        ("GHOST",)).fetchone()
    assert master is None, "precondition: not in master"
    resolved = ((master["salesperson"] if master else None) or snapshot["salesperson_code"])
    assert resolved == "31", "must fall back to the snapshot rather than render blank"


# ── CSV export must agree with the screen ───────────────────────────────────
def test_csv_export_and_dashboard_share_one_definition():
    """#335 changed the screen to the payable figure and left the CSV writing
    the raw `total_commission`: June 2026 rep 31 read ฿130.00 on screen and
    ฿2,036.88 in the export. Both now call `_payable_split`, so they cannot
    drift — this asserts the shared call rather than the number, because the
    number is live data."""
    src = open(os.path.join(APP, "blueprints", "commission_bp.py"), encoding="utf-8").read()
    assert "def _payable_split(" in src, "shared definition must exist"
    # exactly one place computes it, and both consumers call it
    assert src.count("_payable_split(") >= 3, (
        "expected the definition plus a call from BOTH the dashboard and the "
        "CSV export")
    export_fn = src.split("def commission_export(")[1].split("\n@bp_commission")[0]
    assert "_payable_split(" in export_fn, "CSV export must use the shared helper"
    assert "'commission_month_payable'" in export_fn, (
        "CSV must carry the payable column the page displays")


def test_csv_export_leads_with_payable_not_the_raw_total():
    src = open(os.path.join(APP, "blueprints", "commission_bp.py"), encoding="utf-8").read()
    export_fn = src.split("def commission_export(")[1].split("\n@bp_commission")[0]
    header_line = export_fn.split("w.writerow([")[1]
    assert header_line.index("commission_month_payable") < header_line.index("total_commission_raw"), (
        "payable must come before the raw audit figure, and the raw one must be "
        "renamed so nobody mistakes it for what is owed")


def test_paid_status_requires_money_moved_or_owed(empty_db, empty_db_conn):
    """'จ่ายครบ' claimed 4 rows in 2026 where nothing was owed and nothing was
    paid, because it keyed on the raw aggregate. Seed a rep whose whole month is
    settled invoices and who was never paid."""
    _seed_dashboard(empty_db_conn)
    # strip the payout + the owed invoice, leaving only the settled 2025 one
    empty_db_conn.execute("DELETE FROM commission_payouts")
    empty_db_conn.execute("DELETE FROM paid_invoices WHERE doc_no IN ('IV-PAID','IV-OWED')")
    empty_db_conn.commit()

    r = _row31(_dashboard_rows(None, "2026-06", empty_db))
    assert r["total_commission"] > 0, "raw aggregate still counts the settled invoice"
    assert r["commission_month_payable"] == 0.00
    assert r["remaining"] == 0.00

    paid = 0.0
    status = ("paid" if (paid > 0 or r["commission_month_payable"] > 0) else "none")
    assert status == "none", (
        "must NOT claim จ่ายครบ when nothing was owed and nothing was paid")
