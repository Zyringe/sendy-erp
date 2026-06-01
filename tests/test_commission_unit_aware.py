"""Commission line-attribution guarantees on the CANONICAL path.

History: GH issue #30 / PR #29 fixed a duplication bug where commission._BASE_QUERY
joined product_code_mapping by bsn_code alone and multiplied each express_sales
line by the number of mapping rows. That fix used a unit-aware scalar-subquery
resolver against express_sales (whose product_id is always NULL).

The commission engine was re-pointed to canonical tables (sales_transactions +
received_payments + paid_invoices). sales_transactions.product_id is resolved at
import, so the engine joins products/brands directly — the by-code resolver (and
its duplication failure mode) is gone. These tests now lock the equivalent
guarantees on the canonical path:
  1. one sales line -> exactly one commission attribution row (no blow-up),
  2. brand_kind derived from sales_transactions.product_id,
  3. commission computed once on the real net, not doubled.

(The strongest guard against regressions is the live-data parity check in
test_commission_canonical.py — April canonical total must equal the old express
engine to the baht. These synthetic tests pin the mechanics independently.)

tmp_db copies the live DB so all migrations/tiers are present.
"""
from __future__ import annotations

import os
import sqlite3
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))

TEST_DATE = "2099-01-15"   # future — never collides with real data or the cutoff
TEST_BSN_CODE = "TEST-UA-30"
TEST_SP = "TS"             # assigned Tier A below


def _own_and_third_products(conn):
    own_brand = conn.execute("SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1").fetchone()[0]
    third_brand = conn.execute("SELECT id FROM brands WHERE is_own_brand=0 LIMIT 1").fetchone()[0]
    p_own = conn.execute(
        "SELECT id FROM products WHERE brand_id=? AND is_active=1 LIMIT 1", (own_brand,)
    ).fetchone()[0]
    p_third = conn.execute(
        "SELECT id FROM products WHERE brand_id=? AND is_active=1 LIMIT 1", (third_brand,)
    ).fetchone()[0]
    return p_own, p_third


def _setup_salesperson(conn):
    """Create TEST_SP assigned to Tier A (10% own / 5% third)."""
    conn.execute("INSERT OR IGNORE INTO salespersons (code, name) VALUES (?, 'Test SP')", (TEST_SP,))
    tier_a = conn.execute("SELECT id FROM commission_tiers WHERE code='A'").fetchone()[0]
    conn.execute("DELETE FROM commission_assignments WHERE salesperson_code=?", (TEST_SP,))
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES (?, ?, ?)",
        (TEST_SP, tier_a, TEST_DATE),
    )
    conn.commit()


def _insert_canonical_sale_and_receipt(conn, pid, line_net=100.0, doc_no="IVTEST001"):
    """One sales_transactions line (product_id=pid) + a received_payments receipt
    (paid_invoices IV link) that pays it. No commission_payouts row -> paid=0."""
    conn.execute("DELETE FROM sales_transactions WHERE doc_base=?", (doc_no,))
    conn.execute("DELETE FROM received_payments WHERE re_no=?", (f"RE-{doc_no}",))
    conn.execute(
        """INSERT INTO sales_transactions
               (date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
                customer, qty, unit, unit_price, vat_type, discount, total, net)
           VALUES (?, ?, ?, ?, ?, 'test product', 'test cust', 1, 'ตัว', ?, 0, 0, ?, ?)""",
        (TEST_DATE, f"{doc_no}-1", doc_no, pid, TEST_BSN_CODE, line_net, line_net, line_net),
    )
    cur = conn.execute(
        """INSERT INTO received_payments
               (re_no, date_iso, customer, salesperson, cancelled, total)
           VALUES (?, ?, 'test cust', ?, 0, ?)""",
        (f"RE-{doc_no}", TEST_DATE, TEST_SP, line_net),
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES (?, ?, 'IV', ?)",
        (cur.lastrowid, doc_no, line_net),
    )
    conn.commit()


def test_one_commission_row_per_sales_line(tmp_db):
    """Canonical 1:1: one sales_transactions line → exactly one attribution row
    (the receipt→invoice→line join must not Cartesian-multiply)."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, _ = _own_and_third_products(conn)
    _setup_salesperson(conn)
    _insert_canonical_sale_and_receipt(conn, p_own, 100.0)
    conn.close()

    lines = commission.get_lines_for_salesperson("2099-01", TEST_SP, db_path=tmp_db)
    assert len(lines) == 1, f"expected 1 row per sales line, got {len(lines)}"


def test_brand_kind_resolved_from_sales_line_product_id(tmp_db):
    """brand_kind comes from sales_transactions.product_id → products → brands,
    not a by-code mapping resolver."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, _ = _own_and_third_products(conn)
    _setup_salesperson(conn)
    _insert_canonical_sale_and_receipt(conn, p_own, 100.0)
    conn.close()

    lines = commission.get_lines_for_salesperson("2099-01", TEST_SP, db_path=tmp_db)
    assert lines[0]["sendy_product_id"] == p_own
    assert lines[0]["brand_kind"] == "own"


def test_commission_total_not_doubled(tmp_db):
    """End-to-end: net=100 own-brand at Tier A → 10% = 10 baht (computed once)."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, _ = _own_and_third_products(conn)
    _setup_salesperson(conn)
    _insert_canonical_sale_and_receipt(conn, p_own, 100.0)
    conn.close()

    result = commission.get_commission_for_month("2099-01", salesperson_code=TEST_SP, db_path=tmp_db)
    assert len(result) == 1
    assert result[0]["total_net"] == 100.0, f"total_net should be 100, got {result[0]['total_net']}"
    assert result[0]["total_commission"] == 10.0, (
        f"commission should be 10 (10% own of 100), got {result[0]['total_commission']}"
    )
