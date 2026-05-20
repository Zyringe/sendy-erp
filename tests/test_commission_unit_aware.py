"""Commission engine — unit-aware product resolution (GH issue #30).

Locks the 2 high findings from Codex adversarial review of PR #29 (2026-05-20):

  1. commission._BASE_QUERY joins product_code_mapping by bsn_code alone.
     After mig 061 a bsn_code can map to multiple product_ids via different
     bsn_units → each express_sales row gets multiplied by the number of
     mapping rows → line_net summed multiple times → overstated commission.
     Fix: replace by-code JOIN with the unit-aware scalar resolver
     (mirrors mig 063/064 predicate).

  2. scripts/import_express.py _import_sales INSERT omits brand_kind, and
     _product_id_by_code() always returns None. New sales rows after deploy
     keep brand_kind NULL → commission falls back to regex classification →
     split-code lines may pay the wrong own/third-party rate.
     Fix: resolve product_id from (product_code, unit) at INSERT time and
     populate brand_kind from that product's brand.

Both fixes share the canonical resolver predicate:
    WHERE bsn_code = ? AND bsn_unit IN (COALESCE(unit, ''), '')
      AND product_id IS NOT NULL
    ORDER BY (bsn_unit = '')   -- exact unit (0) before catch-all (1)
    LIMIT 1

tmp_db copies the live DB so mig 061/063/064 are already applied.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


# ── Setup helpers ───────────────────────────────────────────────────────────
TEST_DATE = "2099-01-15"   # future date — won't collide with real receipts
TEST_BSN_CODE = "TEST-UA-30"
TEST_SP = "TS"            # "TestSp"; we'll create + assign Tier A


def _two_products_diff_brands(conn):
    """Two products with DIFFERENT brand_kind (one own-brand, one third-party)."""
    own_brand = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1"
    ).fetchone()[0]
    third_brand = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=0 LIMIT 1"
    ).fetchone()[0]
    p_own = conn.execute(
        "SELECT id FROM products WHERE brand_id=? AND is_active=1 LIMIT 1",
        (own_brand,),
    ).fetchone()[0]
    p_third = conn.execute(
        "SELECT id FROM products WHERE brand_id=? AND is_active=1 LIMIT 1",
        (third_brand,),
    ).fetchone()[0]
    return p_own, p_third


def _setup_split_mapping(conn, p_exact, p_catchall):
    """Create unit-split mapping: bsn_unit='ตัว' → p_exact, bsn_unit='' → p_catchall."""
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (TEST_BSN_CODE,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit, product_id) "
        "VALUES (?, 'test-name', 'ตัว', ?)",
        (TEST_BSN_CODE, p_exact),
    )
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit, product_id) "
        "VALUES (?, 'test-name', '', ?)",
        (TEST_BSN_CODE, p_catchall),
    )
    conn.commit()


def _setup_salesperson(conn):
    """Create test salesperson assigned to Tier A (10% own / 5% third)."""
    conn.execute(
        "INSERT OR IGNORE INTO salespersons (code, name) VALUES (?, 'Test SP')",
        (TEST_SP,),
    )
    tier_a = conn.execute(
        "SELECT id FROM commission_tiers WHERE code='A'"
    ).fetchone()[0]
    conn.execute("DELETE FROM commission_assignments WHERE salesperson_code=?", (TEST_SP,))
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES (?, ?, ?)",
        (TEST_SP, tier_a, TEST_DATE),
    )
    conn.commit()


def _insert_sale_and_receipt(conn, unit, line_net, doc_no="IVTEST001"):
    """Insert 1 express_sales line + 1 receipt that pays it.
    brand_kind left NULL on purpose to force resolver path."""
    conn.execute("DELETE FROM express_sales WHERE doc_no=?", (doc_no,))
    conn.execute("DELETE FROM express_payment_in_invoice_refs WHERE invoice_no=?", (doc_no,))
    conn.execute("DELETE FROM express_payments_in WHERE doc_no LIKE 'TESTPI%'")
    conn.execute(
        """
        INSERT INTO express_sales
            (batch_id, doc_no, line_no, doc_type, date_iso, company_id,
             product_code, product_name_raw, qty, unit, unit_price, net, total,
             brand_kind)
        VALUES (1, ?, 1, 'IV', ?, 1, ?, 'test product', 1, ?, ?, ?, ?, NULL)
        """,
        (doc_no, TEST_DATE, TEST_BSN_CODE, unit, line_net, line_net, line_net),
    )
    cur = conn.execute(
        """
        INSERT INTO express_payments_in
            (batch_id, doc_no, date_iso, company_id, customer_name,
             salesperson_code, is_void, cash_amount)
        VALUES (1, 'TESTPI001', ?, 1, 'test cust', ?, 0, ?)
        """,
        (TEST_DATE, TEST_SP, line_net),
    )
    pid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO express_payment_in_invoice_refs
            (payment_in_id, invoice_no, invoice_date_iso, amount)
        VALUES (?, ?, ?, ?)
        """,
        (pid, doc_no, TEST_DATE, line_net),
    )
    conn.commit()


# ── Tests: commission._BASE_QUERY duplication ──────────────────────────────
def test_base_query_returns_one_row_per_sales_line_on_split_code(tmp_db):
    """REGRESSION: pre-fix, by-code JOIN multiplies 1 sale line into 2 rows
    (one per mapping). After fix, unit-aware resolver returns 1 row."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_exact, p_catchall = _two_products_diff_brands(conn)
    _setup_split_mapping(conn, p_exact, p_catchall)
    _setup_salesperson(conn)
    _insert_sale_and_receipt(conn, unit="ตัว", line_net=100.0)
    conn.close()

    lines = commission.get_lines_for_salesperson("2099-01", TEST_SP, db_path=tmp_db)
    assert len(lines) == 1, (
        f"expected 1 row (one sales line), got {len(lines)} — by-code join "
        f"is duplicating across mapping rows"
    )


def test_base_query_resolves_to_unit_specific_product(tmp_db):
    """Unit 'ตัว' must resolve to the unit-specific mapping (p_exact),
    NOT the catch-all (p_catchall)."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_exact, p_catchall = _two_products_diff_brands(conn)
    _setup_split_mapping(conn, p_exact, p_catchall)
    _setup_salesperson(conn)
    _insert_sale_and_receipt(conn, unit="ตัว", line_net=100.0)
    conn.close()

    lines = commission.get_lines_for_salesperson("2099-01", TEST_SP, db_path=tmp_db)
    assert lines[0]["sendy_product_id"] == p_exact, (
        f"sale with unit 'ตัว' should resolve to p_exact={p_exact}, "
        f"got {lines[0]['sendy_product_id']}"
    )


def test_commission_total_not_doubled_on_split_code(tmp_db):
    """End-to-end: net=100 sale at Tier A → 10% own = 10 baht.
    Pre-fix the duplicated rows would compute commission on 200 baht (= 20).
    brand_kind is set on the sale row, simulating the post-mig-063/064 state
    where the one-time backfill has already run."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_exact, p_catchall = _two_products_diff_brands(conn)
    own_brand_id = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1"
    ).fetchone()[0]
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own_brand_id, p_exact))
    _setup_split_mapping(conn, p_exact, p_catchall)
    _setup_salesperson(conn)
    _insert_sale_and_receipt(conn, unit="ตัว", line_net=100.0)
    # Post-063/064 invariant: resolvable rows have brand_kind populated.
    conn.execute("UPDATE express_sales SET brand_kind='own' WHERE doc_no='IVTEST001'")
    conn.commit()
    conn.close()

    result = commission.get_commission_for_month(
        "2099-01", salesperson_code=TEST_SP, db_path=tmp_db
    )
    assert len(result) == 1
    row = result[0]
    # net should be 100 (the one sale), not 200 (duplicated)
    assert row["total_net"] == 100.0, (
        f"total_net should be 100 (one sale), got {row['total_net']}"
    )
    # commission at 10% own = 10
    assert row["total_commission"] == 10.0, (
        f"commission should be 10 (10% of 100), got {row['total_commission']}"
    )


# ── Tests: import_express.py sales path ─────────────────────────────────────
def test_import_sales_leaves_brand_kind_null_for_unbranded_product(tmp_db, monkeypatch):
    """REGRESSION (Codex adversarial review pass 2, 2026-05-20):
    A resolved product whose brand_id IS NULL must produce brand_kind = NULL
    on the sale row, NOT 'third_party'. The mig 063 trigger contract
    intentionally leaves these rows NULL so commission's regex fallback can
    still run — defaulting to 'third_party' would underpay own-brand-looking
    unbranded products."""
    import import_express
    import parse_express_sales

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_unbranded, _ = _two_products_diff_brands(conn)
    conn.execute("UPDATE products SET brand_id=NULL WHERE id=?", (p_unbranded,))
    # Single-row mapping (no split needed for this test)
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (TEST_BSN_CODE,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit, product_id) "
        "VALUES (?, 'test-name', '', ?)",
        (TEST_BSN_CODE, p_unbranded),
    )
    conn.execute("DELETE FROM express_sales WHERE doc_no='IVTEST901'")
    conn.commit()

    class _Rec:
        doc_no = "IVTEST901"
        line_no = 1
        date_iso = TEST_DATE
        customer_code = ""
        customer_name = "x"
        product_code = TEST_BSN_CODE
        product_name = "test"
        qty = 1
        unit = "ตัว"
        return_flag = ""
        unit_price = 100.0
        vat_type = 0
        discount = 0
        total = 100.0
        total_discount = 0
        net = 100.0
        ref_doc = ""
        is_warning = 0

    monkeypatch.setattr(parse_express_sales, "parse_sales", lambda path: [_Rec()])
    import_express._import_sales(conn, path="ignored", batch_id=1, company_id=1)
    conn.commit()

    row = conn.execute(
        "SELECT brand_kind, product_id FROM express_sales WHERE doc_no='IVTEST901'"
    ).fetchone()
    assert row["product_id"] == p_unbranded, "product should still be resolved"
    assert row["brand_kind"] is None, (
        f"resolved-but-unbranded product must yield brand_kind=NULL, "
        f"got {row['brand_kind']!r} (would block commission's regex fallback)"
    )
    conn.close()


def _assign_brand(conn, product_id, is_own):
    """Pick a brand with the desired is_own_brand value and assign it to product_id.
    Also asserts the mig 063 trigger refreshes express_sales.brand_kind for rows
    whose (code, unit) currently resolve to this product."""
    brand_id = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=? LIMIT 1", (1 if is_own else 0,)
    ).fetchone()[0]
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (brand_id, product_id))
    conn.commit()
    return brand_id


# ── Tests: Codex pass 8 (2026-05-21) — stale brand_kind cache ──────────────
# After PR #32 the resolver returns the right sendy_product_id, but
# commission rates were still derived from es.brand_kind (cached at import
# time). When product_code_mapping is later remapped to a product with a
# DIFFERENT brand (e.g. via scripts/apply_unit_aware_remap.py or
# scripts/cleanup_split_mapping_stubs.py), the UI/details show the new
# resolved brand but commission still pays the OLD rate.
#
# Fix: derive brand_kind from the resolved product's b.is_own_brand at read
# time. NULL (unresolved or unbranded) preserves regex fallback contract.

def test_commission_uses_derived_brand_kind_after_remap(tmp_db):
    """REGRESSION: stale es.brand_kind='own' must NOT pay own-brand rate
    when the mapping has been remapped to a third-party product.

    Setup mirrors a real workflow:
      1. Mapping (TEST_BSN_CODE, 'ตัว') → p_own (own-brand).
      2. Sale imported — mig 063 trigger sets es.brand_kind='own'.
      3. Operator runs apply_unit_aware_remap → mapping repoints to p_third
         (third-party brand). No trigger fires; es.brand_kind stays 'own'.
      4. Commission must follow the RESOLVED product (third_party, 5%),
         not the stale cache (own, 10%).
    """
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, p_third = _two_products_diff_brands(conn)
    _assign_brand(conn, p_own, is_own=True)
    _assign_brand(conn, p_third, is_own=False)
    # Step 1: mapping → p_own (own-brand)
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (TEST_BSN_CODE,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit, product_id) "
        "VALUES (?, 'test-name', 'ตัว', ?)",
        (TEST_BSN_CODE, p_own),
    )
    _setup_salesperson(conn)
    _insert_sale_and_receipt(conn, unit="ตัว", line_net=100.0)
    # Step 2: simulate post-import state — mig 063 trigger would have set
    # brand_kind='own' since mapping resolved to p_own.
    conn.execute("UPDATE express_sales SET brand_kind='own' WHERE doc_no='IVTEST001'")
    conn.commit()
    # Step 3: remap to p_third (third-party). This is what apply_unit_aware_remap
    # does — UPDATE product_code_mapping. The mig 063 trigger only fires on
    # products.brand_id changes, so es.brand_kind STAYS 'own' (stale).
    conn.execute(
        "UPDATE product_code_mapping SET product_id=? "
        "WHERE bsn_code=? AND bsn_unit='ตัว'",
        (p_third, TEST_BSN_CODE),
    )
    conn.commit()
    # Confirm staleness invariant — if this fails, an unrelated trigger
    # is auto-fixing brand_kind and the test no longer reproduces the bug.
    stale_kind = conn.execute(
        "SELECT brand_kind FROM express_sales WHERE doc_no='IVTEST001'"
    ).fetchone()[0]
    assert stale_kind == "own", (
        f"test precondition broken: brand_kind should be stale 'own', got {stale_kind!r}"
    )
    conn.close()

    result = commission.get_commission_for_month(
        "2099-01", salesperson_code=TEST_SP, db_path=tmp_db
    )
    assert len(result) == 1
    row = result[0]
    assert row["total_net"] == 100.0
    # FIXED: third-party 5% of 100 = 5.0 (NOT 10.0 from stale 'own' cache)
    assert row["total_commission"] == 5.0, (
        f"commission should follow RESOLVED product (third_party 5%=5.0), "
        f"got {row['total_commission']} — stale es.brand_kind='own' is leaking "
        f"through, paying own-brand rate (10%=10.0)"
    )
    assert row["third_net"] == 100.0 and row["own_net"] == 0.0, (
        f"net should bucket as third_party, got own={row['own_net']} "
        f"third={row['third_net']}"
    )


def test_invoice_breakdown_uses_derived_brand_kind_after_remap(tmp_db):
    """The drill-down view (get_invoice_line_breakdown) has its OWN query
    separate from _BASE_QUERY. It must also derive brand_kind from the
    resolved product — otherwise the UI shows the new brand+name but the
    rate column still reflects the old cached brand_kind, paying the
    wrong amount.

    Same staleness setup as the previous test, but asserts on the
    per-invoice breakdown rather than the monthly summary."""
    import commission

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, p_third = _two_products_diff_brands(conn)
    _assign_brand(conn, p_own, is_own=True)
    _assign_brand(conn, p_third, is_own=False)
    conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (TEST_BSN_CODE,))
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, bsn_unit, product_id) "
        "VALUES (?, 'test-name', 'ตัว', ?)",
        (TEST_BSN_CODE, p_own),
    )
    _setup_salesperson(conn)
    _insert_sale_and_receipt(conn, unit="ตัว", line_net=100.0)
    conn.execute("UPDATE express_sales SET brand_kind='own' WHERE doc_no='IVTEST001'")
    conn.execute(
        "UPDATE product_code_mapping SET product_id=? "
        "WHERE bsn_code=? AND bsn_unit='ตัว'",
        (p_third, TEST_BSN_CODE),
    )
    conn.commit()
    conn.close()

    header, rows = commission.get_invoice_line_breakdown(
        "2099-01", salesperson_code=TEST_SP, invoice_no="IVTEST001", db_path=tmp_db
    )
    assert len(rows) == 1
    line = rows[0]
    assert line["sendy_product_id"] == p_third, "drill-down must show resolved product"
    assert line["brand_kind"] == "third_party", (
        f"drill-down must derive brand_kind from resolved product, "
        f"got {line['brand_kind']!r} (stale cache leaking through)"
    )
    assert line["rate_pct"] == 5, (
        f"rate should be third-party 5%, got {line['rate_pct']} (stale own=10)"
    )
    assert line["commission"] == 5.0, (
        f"line commission should be 5.0 (5% of 100), got {line['commission']}"
    )


def test_import_sales_populates_brand_kind_from_unit_resolver(tmp_db, monkeypatch):
    """A new sales row imported via import_express._import_sales must have
    brand_kind set based on the product its (code, unit) resolves to."""
    import import_express
    import parse_express_sales

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    p_own, p_third = _two_products_diff_brands(conn)
    own_brand_id = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=1 LIMIT 1"
    ).fetchone()[0]
    third_brand_id = conn.execute(
        "SELECT id FROM brands WHERE is_own_brand=0 LIMIT 1"
    ).fetchone()[0]
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (own_brand_id, p_own))
    conn.execute("UPDATE products SET brand_id=? WHERE id=?", (third_brand_id, p_third))
    # Split mapping: unit 'ตัว' → own-brand product; catch-all → third-party
    _setup_split_mapping(conn, p_exact=p_own, p_catchall=p_third)
    # Clean test doc
    conn.execute("DELETE FROM express_sales WHERE doc_no='IVTEST900'")
    conn.commit()

    # Build a synthetic parsed-sales record
    class _Rec:
        doc_no = "IVTEST900"
        line_no = 1
        date_iso = TEST_DATE
        customer_code = ""
        customer_name = "x"
        product_code = TEST_BSN_CODE
        product_name = "test"
        qty = 1
        unit = "ตัว"
        return_flag = ""
        unit_price = 100.0
        vat_type = 0
        discount = 0
        total = 100.0
        total_discount = 0
        net = 100.0
        ref_doc = ""
        is_warning = 0

    monkeypatch.setattr(parse_express_sales, "parse_sales", lambda path: [_Rec()])
    import_express._import_sales(conn, path="ignored", batch_id=1, company_id=1)
    conn.commit()

    row = conn.execute(
        "SELECT brand_kind, product_id FROM express_sales WHERE doc_no='IVTEST900'"
    ).fetchone()
    assert row is not None, "sale was not inserted"
    assert row["brand_kind"] == "own", (
        f"unit 'ตัว' resolves to own-brand product → brand_kind should be 'own', "
        f"got {row['brand_kind']!r}"
    )
    assert row["product_id"] == p_own, (
        f"product_id should be {p_own}, got {row['product_id']}"
    )
    conn.close()
