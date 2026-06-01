"""Commission 'settled-through' cutoff (2026-04-30).

Business rule confirmed by Put 2026-06-01: every commission earned on
receipts COLLECTED on or before 2026-04-30 is already fully paid —
- before Feb 2026 ต๋อ was an employee (closed by fiat),
- Feb–Apr 2026 freelance commission was paid in cash through end of April.

The recorded commission_payouts history for that period is an auto-backfill
(1,678 'system/auto' rows inserted 2026-05-02) computed at a stale rate, so it
UNDER-records what was really paid and the dashboard shows phantom ค้างจ่าย /
บางส่วน. The cutoff forces remaining=0 for receipts <= the settled-through date,
immune to any rate the engine recomputes. Only receipts collected from May 2026
onward are open commission.
"""
from __future__ import annotations

import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))

TEST_SP = "TSCUT"
TEST_BSN_CODE = "TEST-CUT-1"
OWN_NET = 1000.0          # Tier A own-brand 10% -> commission_due = 100.00


def _own_product(conn):
    own_brand = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1"
    ).fetchone()[0]
    return conn.execute(
        "SELECT id FROM products WHERE brand_id=? AND is_active=1 LIMIT 1",
        (own_brand,),
    ).fetchone()[0]


def _setup_sp_tier_a(conn):
    conn.execute(
        "INSERT OR IGNORE INTO salespersons (code, name) VALUES (?, 'Test Cutoff SP')",
        (TEST_SP,),
    )
    tier_a = conn.execute("SELECT id FROM commission_tiers WHERE code='A'").fetchone()[0]
    conn.execute("DELETE FROM commission_assignments WHERE salesperson_code=?", (TEST_SP,))
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES (?, ?, '2024-01-01')",
        (TEST_SP, tier_a),
    )


def _insert_own_sale_and_receipt(conn, pid, doc_no, receipt_date, net=OWN_NET):
    """One own-brand sales_transactions line + a received_payments receipt that
    pays it (paid_invoices IV link). Canonical tables — mirrors the re-pointed
    commission engine. No commission_payouts row -> paid=0."""
    conn.execute("DELETE FROM sales_transactions WHERE doc_base=?", (doc_no,))
    conn.execute("DELETE FROM received_payments WHERE re_no=?", (f"RE-{doc_no}",))
    conn.execute(
        """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, product_id, bsn_code,
                product_name_raw, customer, qty, unit, unit_price,
                vat_type, discount, total, net)
           VALUES (?, ?, ?, ?, ?, 'test product', 'test cust', 1, 'ตัว', ?, 0, 0, ?, ?)""",
        (receipt_date, f"{doc_no}-1", doc_no, pid, TEST_BSN_CODE, net, net, net),
    )
    cur = conn.execute(
        """INSERT INTO received_payments
               (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?, ?, 'test cust', ?, 0, ?)""",
        (f"RE-{doc_no}", receipt_date, TEST_SP, net),
    )
    conn.execute(
        """INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount)
           VALUES (?, ?, 'IV', ?)""",
        (cur.lastrowid, doc_no, net),
    )
    conn.commit()


def test_receipt_on_or_before_settled_through_has_zero_remaining(tmp_db):
    """Receipt collected 2026-04-20 (<= 2026-04-30), own net 1000, NO payout.
    Without the cutoff commission_due=100 / remaining=100 / pending.
    With the cutoff: remaining must be 0 (settled)."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    pid = _own_product(conn)
    _setup_sp_tier_a(conn)
    _insert_own_sale_and_receipt(conn, pid, "IVCUT-APR", "2026-04-20")
    conn.close()

    rows = commission.get_invoice_commission_for_sp(
        "2026-04", TEST_SP, db_path=tmp_db, through_month=True)
    row = next(r for r in rows if r["invoice_no"] == "IVCUT-APR")

    assert row["commission_due"] == 100.0, "sanity: Tier A 10% of 1000"
    assert row["remaining"] == 0.0, (
        f"receipt collected on/before settled-through (2026-04-30) must be "
        f"closed, got remaining={row['remaining']} status={row['paid_status']}"
    )
    assert row["paid_status"] == "settled"


def test_receipt_after_settled_through_stays_open(tmp_db):
    """Receipt collected 2026-05-10 (> cutoff), own net 1000, NO payout.
    Must stay open: remaining=100 / pending (May is the new period)."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    pid = _own_product(conn)
    _setup_sp_tier_a(conn)
    _insert_own_sale_and_receipt(conn, pid, "IVCUT-MAY", "2026-05-10")
    conn.close()

    rows = commission.get_invoice_commission_for_sp(
        "2026-05", TEST_SP, db_path=tmp_db, through_month=True)
    row = next(r for r in rows if r["invoice_no"] == "IVCUT-MAY")

    assert row["commission_due"] == 100.0
    assert row["remaining"] == 100.0, (
        f"May receipt must remain open, got remaining={row['remaining']}"
    )
    assert row["paid_status"] == "pending"
