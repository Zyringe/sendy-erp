"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Compensate opening-balance ADJUST records on 2024-01-03 to restore
stock_levels to the state captured in a backup, AFTER a sync flush
created new BSN transactions that revealed unit/data gaps.

User decision 2026-05-08: keep the new BSN transactions (they expose unit
mismatches user wants to catch), but offset opening balance so current
stock equals backup stock.

For each product where current_qty != backup_qty:
  delta = backup_qty - current_qty
  INSERT INTO transactions (product_id, txn_type='ADJUST',
      quantity_change=delta, created_at='2024-01-03 00:00:00',
      note='opening adjust auto-corrected 2026-05-08 (post BSN sync flush)')

The after_transaction_insert trigger auto-recomputes stock_levels.
Default mode is dry-run. Use --apply to commit.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUP = ROOT / "data" / "backups" / "inventory-pre-merge-prod-2026-05-07_114910.db"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", type=Path, default=BACKUP,
                   help="Backup DB to use as 'desired stock' source")
    args = p.parse_args()

    if not args.backup.exists():
        raise SystemExit(f"backup not found: {args.backup}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS bk", (str(args.backup),))

    # Find all products whose stock differs from backup
    diffs = conn.execute("""
        SELECT cur.product_id,
               COALESCE(bk.quantity, 0) AS backup_qty,
               cur.quantity              AS current_qty,
               (COALESCE(bk.quantity, 0) - cur.quantity) AS delta
          FROM main.stock_levels cur
          LEFT JOIN bk.stock_levels bk ON bk.product_id = cur.product_id
         WHERE cur.quantity != COALESCE(bk.quantity, 0)
    """).fetchall()

    print(f"Products with stock diff vs backup: {len(diffs)}")
    if not diffs:
        print("Nothing to fix.")
        return

    pos_count = sum(1 for d in diffs if d['delta'] > 0)
    neg_count = sum(1 for d in diffs if d['delta'] < 0)
    print(f"  delta > 0 (backup higher, restore by adding):     {pos_count}")
    print(f"  delta < 0 (backup lower, restore by subtracting): {neg_count}")
    print()
    print("First 10 changes:")
    for d in diffs[:10]:
        print(f"  pid={d['product_id']:>5}  current={d['current_qty']:>6}  backup={d['backup_qty']:>6}  delta={d['delta']:+d}")
    print()
    # Negative-stock count check before/after
    neg_now = conn.execute("SELECT COUNT(*) FROM stock_levels WHERE quantity < 0").fetchone()[0]
    neg_after = sum(1 for d in diffs if d['backup_qty'] < 0) + \
                conn.execute("""
                    SELECT COUNT(*) FROM stock_levels s
                    LEFT JOIN bk.stock_levels b ON b.product_id = s.product_id
                    WHERE s.quantity = COALESCE(b.quantity, 0) AND s.quantity < 0
                """).fetchone()[0]
    print(f"Negative-stock now: {neg_now}")
    print(f"Negative-stock after restore: {neg_after} (matches pre-sync state)")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    cur = conn.cursor()
    note = 'opening adjust auto-corrected 2026-05-08 (post BSN sync flush)'
    for d in diffs:
        cur.execute("""
            INSERT INTO transactions
                (product_id, txn_type, quantity_change, unit_mode, note, created_at)
            VALUES (?, 'ADJUST', ?, 'unit', ?, '2024-01-03 00:00:00')
        """, (d['product_id'], d['delta'], note))
    conn.commit()
    print(f"\nApplied {len(diffs)} compensating ADJUSTs")

    # Verify
    final = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM stock_levels WHERE quantity < 0) AS neg_now,
          (SELECT COUNT(*)
             FROM stock_levels s
             LEFT JOIN bk.stock_levels b ON b.product_id = s.product_id
            WHERE s.quantity != COALESCE(b.quantity, 0)) AS still_diff
    """).fetchone()
    print(f"\nFinal: neg_stock={final['neg_now']}  diff_vs_backup={final['still_diff']}")
    conn.close()


if __name__ == "__main__":
    main()
