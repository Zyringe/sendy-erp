"""
DEPRECATED: one-off from 2026-05-04. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Dry-run: replay BSN history into transactions ledger.

Goal:
  - Re-sync sales/purchase rows (1.1.67 → 28.4.69) as UNPAIRED IN/OUT
    (so stock movement reflects every BSN row, not net-zero pairs)
  - Restore manual ADJUST (47) + แถม + unit_conversion entries from
    backup 2026-04-27 (these were lost in the 28.4.69 reimport)
  - Add Opening Balance ADJUST at 2024-01-03 per product so final
    stock_levels == current target

Run on dry-run DB copy. Does NOT touch production.
Reports diff per product. If diff == 0 for all products, plan is safe to apply.
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ERP_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ERP_ROOT / "inventory_app"
sys.path.insert(0, str(APP_DIR))

from models import _sync_bsn_to_stock  # noqa: E402

DRY_DB = ERP_ROOT / "data/backups/inventory-dryrun-v2-2026-04-30-002432.db"
PROD_DB = APP_DIR / "instance/inventory.db"
OLD_BACKUP = ERP_ROOT / "data/backups/inventory-2026-04-27.db"

OPENING_TS = "2024-01-03 00:00:00"
OPENING_NOTE = "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)"
LOSS_TS = "2026-04-29 23:59:59"
LOSS_NOTE = "ยอดสูญหาย/ส่วนต่าง (stock loss — BSN IN > current + OUT)"


def snapshot_target():
    with sqlite3.connect(PROD_DB) as prod:
        return dict(prod.execute(
            "SELECT product_id, quantity FROM stock_levels"
        ).fetchall())


def main():
    print(f"DRY DB:  {DRY_DB}")
    print(f"PROD DB: {PROD_DB}")
    print(f"BACKUP:  {OLD_BACKUP}")
    print()

    target = snapshot_target()
    print(f"[step 0] target stock snapshot: {len(target)} products")

    conn = sqlite3.connect(DRY_DB)
    conn.row_factory = sqlite3.Row

    # ── Step 1: wipe ledger + reset synced flags ──
    conn.execute("DELETE FROM transactions")
    conn.execute("UPDATE sales_transactions SET synced_to_stock=0")
    conn.execute("UPDATE purchase_transactions SET synced_to_stock=0")
    conn.execute("DELETE FROM stock_levels")
    conn.commit()
    print("[step 1] wiped transactions, reset synced_to_stock, cleared stock_levels")

    # ── Step 2: re-sync BSN sales + purchase (unpaired) ──
    _sync_bsn_to_stock(conn, "sales_transactions", "sales")
    _sync_bsn_to_stock(conn, "purchase_transactions", "purchase")
    conn.commit()
    n_sync = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN %'"
    ).fetchone()[0]
    print(f"[step 2] BSN sync: {n_sync} IN/OUT rows created (unpaired)")

    # ── Step 3: restore manual ADJUST + แถม + unit_conversion from backup 27.4.69 ──
    old = sqlite3.connect(OLD_BACKUP)
    old.row_factory = sqlite3.Row
    extras = old.execute("""
        SELECT product_id, txn_type, quantity_change, unit_mode,
               reference_no, note, created_at
        FROM transactions
        WHERE note NOT LIKE 'BSN %'
          AND note NOT LIKE 'ประวัติขาย%'
        ORDER BY created_at, id
    """).fetchall()

    # Filter: only product_ids that exist in current DB
    valid_pids = {r[0] for r in conn.execute(
        "SELECT id FROM products"
    ).fetchall()}

    n_extras_inserted = 0
    n_extras_skipped = 0
    by_type = defaultdict(int)
    for r in extras:
        if r["product_id"] not in valid_pids:
            n_extras_skipped += 1
            continue
        conn.execute("""
            INSERT INTO transactions
                (product_id, txn_type, quantity_change, unit_mode,
                 reference_no, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (r["product_id"], r["txn_type"], r["quantity_change"],
              r["unit_mode"], r["reference_no"], r["note"], r["created_at"]))
        n_extras_inserted += 1
        by_type[r["txn_type"]] += 1
    conn.commit()
    old.close()
    print(f"[step 3] restored {n_extras_inserted} extras "
          f"(ADJUST={by_type['ADJUST']}, IN={by_type['IN']}, OUT={by_type['OUT']}); "
          f"skipped {n_extras_skipped} (product_id missing)")

    # ── Step 4: compute opening balance per product ──
    sum_rows = conn.execute(
        "SELECT product_id, SUM(quantity_change) FROM transactions GROUP BY product_id"
    ).fetchall()
    sum_qty = {pid: total for pid, total in sum_rows}

    all_pids = set(target.keys()) | set(sum_qty.keys()) | valid_pids
    n_opening = 0
    n_loss = 0
    sum_loss = 0
    for pid in all_pids:
        target_q = target.get(pid, 0)
        cur_sum = sum_qty.get(pid, 0)
        calc = target_q - cur_sum
        if calc > 0:
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (pid, calc, OPENING_NOTE, OPENING_TS))
            n_opening += 1
        elif calc < 0:
            # opening must be ≥ 0 (physical reality) — push deficit to current as stock loss
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (pid, calc, LOSS_NOTE, LOSS_TS))
            n_loss += 1
            sum_loss += calc
    conn.commit()
    print(f"[step 4a] inserted {n_opening} opening-balance ADJUST rows at {OPENING_TS}")
    print(f"[step 4b] inserted {n_loss} stock-loss ADJUST rows at {LOSS_TS} "
          f"(total qty: {sum_loss})")

    # ── Step 5: verify stock_levels == target ──
    final_stock = dict(conn.execute(
        "SELECT product_id, quantity FROM stock_levels"
    ).fetchall())

    mismatches = []
    for pid in all_pids:
        target_q = target.get(pid, 0)
        final_q = final_stock.get(pid, 0)
        if final_q != target_q:
            mismatches.append((pid, target_q, final_q, final_q - target_q))

    print()
    print("=" * 60)
    print("REPORT")
    print("=" * 60)
    n_txn_final = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"transactions (final):     {n_txn_final}")
    print(f"products (target):        {len(target)}")
    print(f"products (final stock):   {len(final_stock)}")
    print(f"opening ADJUST inserted:  {n_opening}")
    print(f"stock mismatches:         {len(mismatches)}")
    if mismatches:
        print()
        print("Sample mismatches (product_id, target, final, diff):")
        for m in mismatches[:20]:
            print(f"  {m}")
    else:
        print()
        print("✅ ALL stock_levels match target — plan is safe to apply.")

    conn.close()


if __name__ == "__main__":
    main()
