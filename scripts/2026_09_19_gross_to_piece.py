"""2026-09-19 — base unit กุรุส -> piece (pids 1050 แผ่น, 1320 แท่ง).

WHY. Issue #581. `unit_conversions` says `แผ่น = 1.0` and `แท่ง = 1.0` on
products whose base unit is really a กุรุส (gross = 12 dozen = 144), so a bill
written in pieces posts 144x its true quantity to the stock ledger and a
back-solved opening ADJUST hides the difference exactly.

Put ruled 2026-09-18/19:
  * 1 กุรุส = 144 — confirmed on prod from BOTH axes independently. pid 1320's
    IV6801282-4 sells 72 แท่ง @ ฿8.68 while its own purchase leg RR6800218 buys
    0.5 กร: 72/144 = 0.5 (quantity) and 8.68*144 = ฿1,249.92 ~ the ฿1,250 กุรุส
    tier (price). Neither axis needs the other.
  * Option ข — rebase the base unit rather than store a fractional 1/144 ratio.
    Same policy as the 2026-08-17 โหล->ตัว batch: base unit = the SMALLEST unit,
    the catalogue keeps quoting the pack through `product_price_tiers`.
  * CONTRACT: preserve the physical quantity, change only what the number means.
    Stock is not re-verified and not moved; 0 กุรุส becomes 0 แผ่น.

⚠ NOT a blanket `quantity_change * 144`. pid 1320's sales arrive in TWO units
for the same physical amount — `ตัว` (actually a gross) at qty 0.5/1.0 and
`แท่ง` (the piece) at qty 72. Multiplying the ledger would scale the already-
correct `แท่ง` rows too. Each leg is re-derived from its OWN bill's unit through
the app's replay, after which both spellings converge on -72. That convergence
is the fix.

⚠ NOT `scripts/apply_unit_type_change.py` (DEPRECATED): it hand-writes
`stock_levels`, which double-counts since mig 080 added
`after_transaction_update` (measured 288 vs 24), and it converts the ledger but
not the prices.

The replay engine is the app's own (`models.bsn_sync._sync_bsn_to_stock`, the
same Reconciliation Procedure `update_unit_conversion_ratio` runs). This script
only orchestrates: it cannot replay one unit at a time because unit_type, three
ratios and two prices must move together or the ledger is momentarily incoherent.

FREE EVIDENCE. The opening ADJUST is recomputed after the replay rather than
rescaled. If it lands near zero, the old plug was pure ledger error and there was
no real opening stock — the independent check that the plug itself could never
provide, because `opening = oracle - net` absorbs any error by construction.

    python3 scripts/2026_09_19_gross_to_piece.py --db PATH --threshold keep
    python3 scripts/2026_09_19_gross_to_piece.py --db PATH --threshold keep --apply
"""
import argparse
import os
import sqlite3
import sys

SOURCE = 'script:2026_09_19_gross_to_piece'
RATIO = 144
OPENING_NOTE = 'ยอดยกมา (back-solved)'

# pid: (label, new base unit, new per-piece base_sell_price — /144 rounded UP,
#       Put's batch-1 convention: cost divides exactly, sell price rounds up)
PLAN = {
    1050: ('กระดาษทรายขัดไม้ จระเข้ #3', 'แผ่น', 6.46),
    1320: ('ดินสอช่างไม้พระจันทร์แท้', 'แท่ง', 8.69),
}


class RebaseRefused(Exception):
    """A precondition failed. Nothing has been written."""


def _stock(conn, pid):
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    return row[0] if row else 0.0


def _ledger_sum(conn, pid):
    return conn.execute(
        "SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?",
        (pid,)).fetchone()[0]


def rebase(conn, pid, new_unit, ratio, new_base_sell, preserve_stock):
    """Rebase one product onto `new_unit`, preserving its physical quantity.

    Returns the recomputed opening quantity — the free-evidence number.
    Raises RebaseRefused before writing anything if the product is not in the
    expected pre-state.
    """
    from models import bsn_sync

    row = conn.execute(
        "SELECT unit_type, cost_price, base_sell_price, opening_cost FROM products WHERE id=?",
        (pid,)).fetchone()
    if row is None:
        raise RebaseRefused("pid %s does not exist — wrong DB?" % pid)
    unit_type, cost, base, opening_cost = row
    if unit_type == new_unit:
        raise RebaseRefused("pid %s is already %r — refusing to convert twice" % (pid, new_unit))
    if not cost or cost <= 0:
        raise RebaseRefused("pid %s: cost_price %r — a zero breaks the value check" % (pid, cost))

    # 1. product: cost divides EXACTLY (rounding loses stock value), sell price
    #    is the caller's rounded-up figure, threshold is left alone (Put 08-17:
    #    it is base-unit-expressed, so converting changes its physical meaning).
    conn.execute(
        "UPDATE products SET unit_type=?, cost_price=?, opening_cost=?, base_sell_price=? "
        "WHERE id=?",
        (new_unit, cost / ratio, (opening_cost or cost) / ratio, new_base_sell, pid))

    # 2. ratios. Every existing unit was expressed in the OLD base (a gross), so
    #    it scales by `ratio`; the new base unit is 1.0 by definition. A bill
    #    written at the old `unit_type` spelling means a GROSS, not a piece —
    #    that row is why a blanket ledger multiply would be wrong.
    for (bsn_unit,) in conn.execute(
            "SELECT bsn_unit FROM unit_conversions WHERE product_id=?", (pid,)).fetchall():
        conn.execute(
            "UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=?",
            (1.0 if bsn_unit == new_unit else ratio, pid, bsn_unit))
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,1.0) "
        "ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio=1.0", (pid, new_unit))

    # 3. replay through the app's own engine, exactly as
    #    bsn_sync.update_unit_conversion_ratio does: reset synced -> delete every
    #    ledger row a BSN sync wrote -> re-sync -> verify identity-by-identity.
    synced_before = bsn_sync._synced_source_ids(conn, pid)
    for table in ('sales_transactions', 'purchase_transactions'):
        conn.execute("UPDATE %s SET synced_to_stock=0 WHERE product_id=?" % table, (pid,))
    conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND ({})".format(
            " OR ".join("note LIKE ?" for _ in bsn_sync._BSN_LEDGER_NOTE_PATTERNS)),
        (pid, *bsn_sync._BSN_LEDGER_NOTE_PATTERNS))
    bsn_sync._sync_bsn_to_stock(conn, 'sales_transactions', 'sales', product_ids=(pid,))
    bsn_sync._sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase', product_ids=(pid,))
    synced_after = bsn_sync._synced_source_ids(conn, pid)
    if synced_before - synced_after:
        raise RebaseRefused(
            "pid %s: replay lost %d movement(s) — %r"
            % (pid, len(synced_before - synced_after), sorted(synced_before - synced_after)[:5]))

    # 4. opening. Recomputed, never rescaled: the old plug was denominated in the
    #    old base AND contained the bug it was hiding, so neither its number nor
    #    its unit survives. Stamped strictly before the head because WACC sorts
    #    IN first on a created_at tie, so a row sharing the head's timestamp
    #    would be costed AFTER the first purchase.
    conn.execute("DELETE FROM transactions WHERE product_id=? AND note=?", (pid, OPENING_NOTE))
    needed = preserve_stock - _ledger_sum(conn, pid)
    if abs(needed) > 1e-9:
        head = conn.execute(
            "SELECT MIN(created_at) FROM transactions WHERE product_id=?", (pid,)).fetchone()[0]
        stamp = (conn.execute("SELECT datetime(?, '-1 second')", (head,)).fetchone()[0]
                 if head else '2024-01-03 00:00:00')
        conn.execute(
            "INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, note, created_at) "
            "VALUES (?, 'ADJUST', ?, 'unit', ?, ?)", (pid, needed, OPENING_NOTE, stamp))

    # 5. cost ledger: same physical history, re-denominated. The note is REBUILT
    #    from structured values and APPENDED — never text-substituted. Batch 1's
    #    v1 ran replace() on the unit WORDS and left the NUMBERS, producing false
    #    audit evidence from the step meant to keep the record honest.
    for lid, qty, unit_cost, note in conn.execute(
            "SELECT id, qty_change, unit_cost, note FROM product_cost_ledger "
            "WHERE product_id=? ORDER BY id", (pid,)).fetchall():
        conn.execute(
            "UPDATE product_cost_ledger SET qty_change=qty_change*?, stock_after=stock_after*?, "
            "unit_cost=unit_cost/?, wacc_after=wacc_after/?, note=? WHERE id=?",
            (ratio, ratio, ratio, ratio,
             "{} | แปลงหน่วย 2026-09-19: {:g} {} @ ฿{:.4f} → {:g} {} @ ฿{:.6f} ({})".format(
                 note or '(ไม่มีหมายเหตุเดิม)', qty, unit_type, unit_cost,
                 qty * ratio, new_unit, unit_cost / ratio, SOURCE),
             lid))
    return needed
