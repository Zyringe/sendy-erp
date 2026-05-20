"""
DEPRECATED: one-off from 2026-05-04. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply: replay BSN history into transactions ledger on PRODUCTION.

Same logic as replay_history_dry_run.py + safety:
  - SAVEPOINT around the whole operation
  - Snapshot products.shopee_stock/lazada_stock + platform_skus.stock,
    restore after replay (sync would otherwise double-deduct)
  - Verify stock_levels == pre-replay target before COMMIT
  - Auto-ROLLBACK if any mismatch
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ERP_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ERP_ROOT / "inventory_app"
sys.path.insert(0, str(APP_DIR))

from models import _sync_bsn_to_stock  # noqa: E402

PROD_DB = APP_DIR / "instance/inventory.db"
OLD_BACKUP = ERP_ROOT / "data/backups/inventory-2026-04-27.db"

OPENING_TS = "2024-01-03 00:00:00"
OPENING_NOTE = "ยอดต้นปี (back-solved จาก current stock − ประวัติ BSN)"
LOSS_TS = "2026-04-29 23:59:59"
LOSS_NOTE = "ยอดสูญหาย/ส่วนต่าง (stock loss — BSN IN > current + OUT)"


def main():
    print(f"PROD DB: {PROD_DB}")
    print(f"BACKUP:  {OLD_BACKUP}")
    print()

    conn = sqlite3.connect(PROD_DB)
    conn.row_factory = sqlite3.Row

    # ── Pre: snapshot target stock + marketplace columns ──
    target = dict(conn.execute(
        "SELECT product_id, quantity FROM stock_levels"
    ).fetchall())
    snap_products = conn.execute(
        "SELECT id, shopee_stock, lazada_stock FROM products"
    ).fetchall()
    snap_platform = conn.execute(
        "SELECT id, stock FROM platform_skus"
    ).fetchall()
    print(f"[snapshot] target: {len(target)} products, "
          f"products: {len(snap_products)}, platform_skus: {len(snap_platform)}")

    conn.execute("SAVEPOINT replay_history")

    try:
        # ── Step 1: wipe ledger ──
        conn.execute("DELETE FROM transactions")
        conn.execute("UPDATE sales_transactions SET synced_to_stock=0")
        conn.execute("UPDATE purchase_transactions SET synced_to_stock=0")
        conn.execute("DELETE FROM stock_levels")
        print("[step 1] wiped transactions, reset synced flags, cleared stock_levels")

        # ── Step 2: re-sync BSN (unpaired) ──
        _sync_bsn_to_stock(conn, "sales_transactions", "sales")
        _sync_bsn_to_stock(conn, "purchase_transactions", "purchase")
        n_sync = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE note LIKE 'BSN %'"
        ).fetchone()[0]
        print(f"[step 2] BSN sync: {n_sync} IN/OUT rows created")

        # ── Step 3: restore extras from backup 27.4.69 ──
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
        valid_pids = {r[0] for r in conn.execute("SELECT id FROM products").fetchall()}
        n_inserted = 0
        by_type = defaultdict(int)
        for r in extras:
            if r["product_id"] not in valid_pids:
                continue
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (r["product_id"], r["txn_type"], r["quantity_change"],
                  r["unit_mode"], r["reference_no"], r["note"], r["created_at"]))
            n_inserted += 1
            by_type[r["txn_type"]] += 1
        old.close()
        print(f"[step 3] restored {n_inserted} extras "
              f"(ADJUST={by_type['ADJUST']}, IN={by_type['IN']}, OUT={by_type['OUT']})")

        # ── Step 4: opening + stock-loss ADJUSTs ──
        sum_qty = dict(conn.execute(
            "SELECT product_id, SUM(quantity_change) FROM transactions GROUP BY product_id"
        ).fetchall())
        all_pids = set(target) | set(sum_qty) | valid_pids
        n_open = n_loss = 0
        for pid in all_pids:
            calc = target.get(pid, 0) - sum_qty.get(pid, 0)
            if calc > 0:
                conn.execute("""
                    INSERT INTO transactions (product_id, txn_type, quantity_change,
                        unit_mode, reference_no, note, created_at)
                    VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
                """, (pid, calc, OPENING_NOTE, OPENING_TS))
                n_open += 1
            elif calc < 0:
                conn.execute("""
                    INSERT INTO transactions (product_id, txn_type, quantity_change,
                        unit_mode, reference_no, note, created_at)
                    VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
                """, (pid, calc, LOSS_NOTE, LOSS_TS))
                n_loss += 1
        print(f"[step 4] opening={n_open} at {OPENING_TS}, loss={n_loss} at {LOSS_TS}")

        # ── Step 5: restore marketplace columns ──
        for r in snap_products:
            conn.execute("""
                UPDATE products SET shopee_stock=?, lazada_stock=? WHERE id=?
            """, (r["shopee_stock"], r["lazada_stock"], r["id"]))
        for r in snap_platform:
            conn.execute("UPDATE platform_skus SET stock=? WHERE id=?",
                         (r["stock"], r["id"]))
        print(f"[step 5] restored marketplace columns "
              f"(products={len(snap_products)}, platform_skus={len(snap_platform)})")

        # ── Step 6: verify ──
        final = dict(conn.execute(
            "SELECT product_id, quantity FROM stock_levels"
        ).fetchall())
        mismatches = []
        for pid in all_pids:
            if final.get(pid, 0) != target.get(pid, 0):
                mismatches.append((pid, target.get(pid, 0), final.get(pid, 0)))

        if mismatches:
            print(f"\n❌ {len(mismatches)} stock mismatches — ROLLING BACK")
            for m in mismatches[:10]:
                print(f"  pid={m[0]} target={m[1]} final={m[2]}")
            conn.execute("ROLLBACK TO SAVEPOINT replay_history")
            conn.execute("RELEASE SAVEPOINT replay_history")
            conn.commit()
            return 1

        print(f"\n✅ {len(target)} products match — COMMITTING")
        conn.execute("RELEASE SAVEPOINT replay_history")
        conn.commit()

        n_final = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        print(f"\nFinal transactions count: {n_final}")
        return 0

    except Exception as e:
        print(f"\n❌ ERROR: {e} — ROLLING BACK")
        conn.execute("ROLLBACK TO SAVEPOINT replay_history")
        conn.execute("RELEASE SAVEPOINT replay_history")
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
