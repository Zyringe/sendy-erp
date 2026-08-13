"""Delete the 3 immortal `BSN re-added pack-basis` ledger rows (2026-08-13).

WHY
    The 2026-06-16 kg→แพ็ค reconcile (archives/erp-prod-ops/local-prod-reconcile/
    artifacts/build-combined-upload.sh) re-added three prod-only sales as
    hand-written ledger rows. models/imports.py deletes `note IN ('BSN ขาย',
    'BSN ขาย-คืน')` — exact-match — so those rows survived every later import
    while the genuine row beside them was deleted and re-created each time.
    One 2-แพ็ค bill has been deducting 4 ever since.

    Verified against an independent oracle (source tables, not the ledger):
    684 short by 2, 1373 by 2, 1374 by 1. The four unaffected ถุงหิ้ว products
    tie at exactly 0, which is the control proving the oracle sound.

WHAT
    Deletes exactly the three rows. Touches nothing else: the mig-080
    `after_transaction_delete` trigger restores stock_levels on its own, so
    this script must NOT write stock_levels itself (that would double-count).

SAFETY
    Every invariant is asserted INSIDE the write transaction and any failure
    rolls back — the post-commit re-read below is confirmation, not protection.
    There is no rehearse/live flag: point it at a `.backup` snapshot to
    rehearse, at the real file to apply. The path IS the mode.

USAGE
    python fix_orphan_tunghiw_ledger.py <db_path>
"""
import sqlite3
import sys

# The rows, pinned by id AND by content: a bare id list would happily delete
# whatever now sits at those ids on a DB this was not written for.
TARGETS = {
    288987: {'product_id': 684,  'reference_no': 'IV6900930-1', 'change': -2},
    288988: {'product_id': 1373, 'reference_no': 'IV6900925-1', 'change': -2},
    288989: {'product_id': 1374, 'reference_no': 'IV6900926-1', 'change': -1},
}
NOTE_PREFIX = 'BSN re-added pack-basis'

# What the numbers were when this was written (2026-08-13). Printed for the
# operator, NOT asserted: the team keys sales daily, so a hardcoded target
# would make this script correct only at the instant it was written. The
# assertion below is on the DELTA — each product must rise by exactly the
# quantity that was deleted — which stays true whatever else has been sold.
STOCK_WHEN_WRITTEN = {684: (46, 48), 1373: (55, 57), 1374: (46, 47)}


def main(db_path):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Independent signal, taken BEFORE the delete: the total stock of every
        # OTHER product must be untouched. A trigger misfire would show here.
        others_before = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock_levels"
            " WHERE product_id NOT IN (684,1373,1374)").fetchone()[0]

        stock_before = {
            pid: conn.execute("SELECT quantity FROM stock_levels WHERE product_id=?",
                              (pid,)).fetchone()[0]
            for pid in {t['product_id'] for t in TARGETS.values()}}

        for tid, want in TARGETS.items():
            row = conn.execute(
                "SELECT product_id, reference_no, quantity_change, note"
                "  FROM transactions WHERE id=?", (tid,)).fetchone()
            assert row is not None, f"row {tid} is gone — wrong DB, or already fixed"
            assert row['product_id'] == want['product_id'], f"{tid}: product mismatch"
            assert row['reference_no'] == want['reference_no'], f"{tid}: doc mismatch"
            assert row['quantity_change'] == want['change'], f"{tid}: qty mismatch"
            assert row['note'].startswith(NOTE_PREFIX), f"{tid}: note is {row['note']!r}"
            # The row is only safe to delete because the real one still exists.
            canon = conn.execute(
                "SELECT COUNT(*) FROM transactions"
                " WHERE product_id=? AND reference_no=? AND note='BSN ขาย'",
                (want['product_id'], want['reference_no'])).fetchone()[0]
            assert canon == 1, (f"{tid}: expected exactly 1 'BSN ขาย' row for "
                                f"{want['reference_no']}, found {canon} — "
                                f"deleting would LOSE the sale")

            # The SOURCE side (Codex review): the canonical row is only the
            # right survivor if exactly one source line stands behind it, and
            # that line is already marked synced. Two source rows would mean
            # the document legitimately deducts twice and neither ledger row is
            # an orphan; synced_to_stock=0 would mean the next import is about
            # to post a THIRD. Either way, stop rather than delete.
            src = conn.execute(
                "SELECT qty, unit, synced_to_stock FROM sales_transactions"
                " WHERE doc_no=? AND product_id=?",
                (want['reference_no'], want['product_id'])).fetchall()
            assert len(src) == 1, (f"{tid}: expected exactly 1 sales_transactions "
                                   f"row for {want['reference_no']} on product "
                                   f"{want['product_id']}, found {len(src)}")
            assert src[0]['qty'] == -want['change'], (
                f"{tid}: source qty {src[0]['qty']} does not match the ledger "
                f"deduction {want['change']}")
            assert src[0]['synced_to_stock'] == 1, (
                f"{tid}: source row is not marked synced — the next import "
                f"would post another ledger row")

        cur = conn.execute(
            "DELETE FROM transactions WHERE id IN (?,?,?)", tuple(TARGETS))
        assert cur.rowcount == 3, f"deleted {cur.rowcount} rows, expected 3"

        for pid, before in stock_before.items():
            restored = sum(-t['change'] for t in TARGETS.values()
                           if t['product_id'] == pid)
            stock = conn.execute(
                "SELECT quantity FROM stock_levels WHERE product_id=?",
                (pid,)).fetchone()[0]
            ledger = conn.execute(
                "SELECT COALESCE(SUM(quantity_change),0) FROM transactions"
                " WHERE product_id=?", (pid,)).fetchone()[0]
            assert stock == before + restored, (
                f"pid {pid}: {before} -> {stock}, expected +{restored}")
            assert ledger == stock, f"pid {pid}: ledger {ledger} != stock {stock}"
            noted = STOCK_WHEN_WRITTEN.get(pid)
            flag = '' if not noted or stock == noted[1] else \
                '  (differs from 2026-08-13 — normal if the team sold more since)'
            print(f"  pid {pid}: {before} -> {stock}{flag}")
            left = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE product_id=?"
                " AND note LIKE 'BSN%' AND note NOT IN"
                " ('BSN ขาย','BSN ขาย-คืน','BSN ซื้อ','BSN ซื้อ-คืน')",
                (pid,)).fetchone()[0]
            assert left == 0, f"pid {pid}: {left} orphan BSN rows still present"

        others_after = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock_levels"
            " WHERE product_id NOT IN (684,1373,1374)").fetchone()[0]
        assert others_after == others_before, (
            f"collateral damage: other stock {others_before} -> {others_after}")

        conn.commit()
        print("committed: 3 rows deleted")
    except Exception:
        conn.rollback()
        print("ROLLED BACK — nothing changed")
        raise
    finally:
        conn.close()

    # Independent re-read on a FRESH connection.
    check = sqlite3.connect(db_path)
    try:
        for pid, before in stock_before.items():
            restored = sum(-t['change'] for t in TARGETS.values()
                           if t['product_id'] == pid)
            got = check.execute(
                "SELECT quantity FROM stock_levels WHERE product_id=?",
                (pid,)).fetchone()[0]
            ok = got == before + restored
            print(f"  verify pid {pid}: stock = {got}"
                  f" (was {before}, +{restored}) {'OK' if ok else 'MISMATCH'}")
            assert ok
        gone = check.execute(
            "SELECT COUNT(*) FROM transactions WHERE note LIKE ?",
            (NOTE_PREFIX + '%',)).fetchone()[0]
        print(f"  verify: {gone} '{NOTE_PREFIX}' rows remain in the whole ledger"
              f" {'OK' if gone == 0 else 'MISMATCH'}")
        assert gone == 0
    finally:
        check.close()
    print("VERIFIED")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
