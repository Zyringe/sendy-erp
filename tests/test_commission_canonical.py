"""Commission engine anchored to the CANONICAL tables (sales_transactions +
received_payments + paid_invoices) instead of the stale express_* mirror.

express_sales / express_payments_in froze at 2026-04-30; the canonical tables
carry live data through the current month, so the engine reads canonical. The
original express↔canonical parity (April == ฿10,098.07) held only at migration
time — canonical legitimately diverges as more payments are imported, so that
frozen-parity assertion is retired in favour of the live anchors below.

test_april_total_anchor / test_april_sp06_excludes_null_amount_phantom_links
are HERMETIC (issue #264): they seed a small deterministic scenario into
empty_db (see _seed_april_scenario) instead of reading live April data, so
they no longer drift as real payments/overrides get imported — the durable
fix the old docstring warning below asked for.

test_may_commission_computes_from_canonical / test_may_month_appears_in_payment_activity
are UNCHANGED — still live-data anchors, out of scope for #264's known-9 (they
were not in the failing list). ⚠️ LIVE-DATA anchors: the tmp_db fixture copies
the real inventory.db, so these totals MOVE when commission-affecting data
changes (payments imported, commission_overrides edited). They are NOT frozen
oracles. When one fails, first confirm it is data drift (not an engine
regression), then re-verify and update the constant.

Last re-verified 2026-06-03 against live data:
  - sp 06 May ฿4,935.32 = ฿5,940.32 (tier-only) − ฿1,005.00, the ฿5/piece
    cutting-disc overrides (commission_overrides id 1/7/8/9 = pid 394/395/396/398,
    added 2026-06-02). Confirms May surfaces from canonical AND that the
    cutting-disc override rule is applied.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "inventory_app"))


def _total(rows):
    return round(sum(r["total_commission"] for r in rows), 2)


def _sp(rows, code):
    return round(sum(r["total_commission"] for r in rows
                     if r["salesperson_code"] == code), 2)


# ── Hermetic April scenario (issue #264) ─────────────────────────────────────
# Two salespeople, one flat no-threshold tier (5% own / 3% third) so
# commission_below == the whole per-invoice commission and above_own/
# above_third stay 0 — keeps this hand-verified math exact:
#
#   sp 06: IVT-0001 (real invoice, paid_invoices.amount=1500) —
#            own line   net 1000 @5% = 50.00
#            third line net  500 @3% = 15.00
#            => commission 65.00
#          IVT-PHANTOM-1 (LISTED on a different receipt, RCPT-T-003, but
#          paid_invoices.amount IS NULL there — the "listed but unallocated,
#          paid by a different receipt" case). own line net 500. MUST be
#          excluded entirely; if the NULL-amount filter regresses this leaks
#          in as +25.00 (500 @5%) => sp 06 would read 90.00 instead of 65.00.
#   sp 07: IVT-0002 (real invoice, amount=2000) —
#            third line net 2000 @3% = 60.00
#
#   sp 06 total   = 65.00  (anchor for the phantom-link guard)
#   overall total = 65.00 + 60.00 = 125.00 (anchor for the whole-engine total)
def _seed_april_scenario(conn):
    conn.execute(
        "INSERT INTO brands (id, code, name, is_own_brand) VALUES "
        "(1, 'T_OWN', 'Test Own Brand', 1), (2, 'T_3RD', 'Test Third Party', 0)"
    )
    conn.execute(
        "INSERT INTO products (id, product_name, brand_id) VALUES "
        "(1, 'สินค้าทดสอบ own', 1), (2, 'สินค้าทดสอบ third', 2)"
    )
    conn.execute(
        "INSERT INTO salespersons (code, name) VALUES "
        "('06', 'ทดสอบ 06'), ('07', 'ทดสอบ 07')"
    )
    conn.execute(
        "INSERT INTO commission_tiers "
        "(id, code, name_th, rate_own_pct, rate_third_pct, threshold_amount) "
        "VALUES (1, 'T_FLAT', 'เทียร์ทดสอบ (ไม่มีเพดาน)', 5.0, 3.0, NULL)"
    )
    conn.execute(
        "INSERT INTO commission_assignments (salesperson_code, tier_id, effective_from) "
        "VALUES ('06', 1, '2026-01-01'), ('07', 1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO received_payments (id, re_no, date_iso, customer, salesperson, cancelled) VALUES "
        "(1, 'RCPT-T-001', '2026-04-05', 'ลูกค้าทดสอบ 1', '06', 0), "
        "(2, 'RCPT-T-002', '2026-04-10', 'ลูกค้าทดสอบ 2', '07', 0), "
        "(3, 'RCPT-T-003', '2026-04-15', 'ลูกค้าทดสอบ 3', '06', 0)"
    )
    conn.execute(
        "INSERT INTO paid_invoices (re_id, doc_no, doc_kind, amount) VALUES "
        "(1, 'IVT-0001', 'IV', 1500.00), "
        "(2, 'IVT-0002', 'IV', 2000.00), "
        # RP3 LISTS IVT-PHANTOM-1 but allocates it NULL (paid by a different
        # receipt) — the load-bearing filter must drop this row entirely.
        "(3, 'IVT-PHANTOM-1', 'IV', NULL)"
    )
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, qty, unit_price, net, total) VALUES "
        "('2026-04-05', 'IVT-0001-1', 'IVT-0001', 1, 'ลูกค้าทดสอบ 1', 10, 100.0, 1000.00, 1000.00), "
        "('2026-04-05', 'IVT-0001-2', 'IVT-0001', 2, 'ลูกค้าทดสอบ 1', 5, 100.0, 500.00, 500.00), "
        "('2026-04-10', 'IVT-0002-1', 'IVT-0002', 2, 'ลูกค้าทดสอบ 2', 20, 100.0, 2000.00, 2000.00), "
        "('2026-04-15', 'IVT-PHANTOM-1-1', 'IVT-PHANTOM-1', 1, 'ลูกค้าทดสอบ 3', 5, 100.0, 500.00, 500.00)"
    )
    conn.commit()


def test_april_total_anchor(empty_db, empty_db_conn):
    """Hermetic (issue #264): seeded 2-salesperson April scenario — see
    _seed_april_scenario for the hand-verified math. Catches an engine
    regression that moves the total (own/third split, per-invoice rounding,
    multi-salesperson aggregation)."""
    _seed_april_scenario(empty_db_conn)
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=empty_db)
    assert abs(_total(rows) - 125.00) < 0.01, (
        f"April total commission = {_total(rows)} (expected 125.00 from the "
        f"seeded scenario — see _seed_april_scenario)"
    )


def test_april_sp06_excludes_null_amount_phantom_links(empty_db, empty_db_conn):
    """Hermetic (issue #264): load-bearing guard — paid_invoices NULL-amount
    rows (invoice LISTED on a receipt but allocated 0, paid by a different
    receipt) must be filtered out. The seeded scenario plants exactly one such
    phantom link on sp 06 (IVT-PHANTOM-1, net 500 @5% = 25.00 if it leaked
    in); see _seed_april_scenario."""
    _seed_april_scenario(empty_db_conn)
    import commission

    rows = commission.get_commission_for_month("2026-04", db_path=empty_db)
    assert abs(_sp(rows, "06") - 65.00) < 0.01, (
        f"sp 06 April = {_sp(rows, '06')} (expected 65.00; 90.00 means the "
        f"NULL-amount paid_invoices rows were not filtered)"
    )


def test_may_commission_computes_from_canonical(tmp_db):
    """May must surface from canonical (was 0 on the frozen express feed) AND
    reflect the ฿5/piece cutting-disc overrides: ฿4,935.32 = ฿5,940.32 tier-only
    − ฿1,005.00 (overrides id 1/7/8/9). See module docstring (live-data)."""
    import commission

    rows = commission.get_commission_for_month("2026-05", db_path=tmp_db)
    assert _sp(rows, "06") > 0, "sp 06 May commission must compute from canonical"
    assert abs(_sp(rows, "06") - 4935.32) < 0.05, (
        f"sp 06 May = {_sp(rows, '06')} (anchor 4935.32 = 5940.32 tier-only "
        f"− 1005.00 cutting-disc override; see module docstring)"
    )


def test_may_month_appears_in_payment_activity(tmp_db, monkeypatch):
    """The /commission month dropdown is built from
    _months_with_payment_activity, which must also read canonical receipts so
    May/June surface as selectable months."""
    import config
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_db)
    import importlib
    import blueprints.commission_bp as commission_bp_mod
    importlib.reload(commission_bp_mod)

    months = commission_bp_mod._months_with_payment_activity()
    assert "2026-05" in months, "May must appear in the commission month dropdown"
    assert "2026-06" in months, "June must appear too"
