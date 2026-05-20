"""One-shot fix for product_id=592 (ไขควงลองไฟ MATA เล็ก K11-1155).

Bug: BSN purchase rows recorded with unit='อน' for this product were never
synced to ledger because product 592 had no unit_conversion for 'อน' (only
หล=12 and โหล=12). Someone manually inserted 3 ledger entries of `IN 288
แถม RR%` (=24×12, mistakenly applying the หล ratio). This left:
  - 864 inflate (3 wrong manual ADJUSTs)
  - 432 missing (6 paid+free purchase rows never synced)
  - Net ledger overcount: +432 vs reality

Put confirmed physical stock = 0 (2026-05-05).

Fix:
  1. Add unit_conversions(592, 'อน', 1.0)
  2. Delete 3 wrong manual entries
  3. Delete opening ADJUST (was back-solved from wrong ledger, half-unit)
  4. Narrow BSN sync — only product 592, only unit='อน' rows
  5. Insert clean ADJUST to bring stock to 0
  6. Also delete the 3 wrong entries from old backup (if present) so future
     replay_history_apply.py won't restore them.

Run on default local DB:
  python scripts/fix_k11_1155_unit_conversion.py

Run on a specific DB file (e.g. prod snapshot):
  python scripts/fix_k11_1155_unit_conversion.py /path/to/inventory.db
"""
from __future__ import annotations
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / 'inventory_app/instance/inventory.db'
DEFAULT_OLD_BACKUP = ROOT / 'data/backups/inventory-2026-04-27.db'

DB = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else DEFAULT_DB
OLD_BACKUP = DEFAULT_OLD_BACKUP if DB == DEFAULT_DB else None

PRODUCT_ID = 592
TARGET_STOCK = 0
ADJUST_NOTE = 'แก้ไข unit conversion ผิด K11-1155 (อน=1 ไม่ใช่ 12) — ตั้งสต็อกตามนับจริง=0'


def backup(path: Path) -> Path:
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = path.parent / f'{path.stem}.backup-pre-K11fix-{ts}{path.suffix}'
    shutil.copy2(path, target)
    print(f'  → backup: {target.name}')
    return target


def fix_live_db(db_path: Path) -> bool:
    print(f'\n=== Fixing {db_path.name} ===')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    pre = conn.execute('SELECT quantity FROM stock_levels WHERE product_id=?',
                       (PRODUCT_ID,)).fetchone()
    print(f'Before: stock_levels[{PRODUCT_ID}] = {pre[0] if pre else None}')

    conn.execute('SAVEPOINT k11fix')
    try:
        # 1. Add unit_conversion
        conn.execute(
            'INSERT OR IGNORE INTO unit_conversions(product_id, bsn_unit, ratio) VALUES (?, ?, ?)',
            (PRODUCT_ID, 'อน', 1.0))
        print(f'[1] Added unit_conversion ({PRODUCT_ID}, อน, 1.0)')

        # 2. Delete wrong manual entries
        cur = conn.execute(
            "DELETE FROM transactions WHERE product_id=? AND quantity_change=288 "
            "AND note LIKE 'แถม RR%'",
            (PRODUCT_ID,))
        print(f'[2] Deleted {cur.rowcount} wrong manual ADJUSTs (qty=288)')

        # 3. Delete opening ADJUST
        cur = conn.execute(
            "DELETE FROM transactions WHERE product_id=? AND txn_type='ADJUST' "
            "AND note LIKE 'ยอดต้นปี%'",
            (PRODUCT_ID,))
        print(f'[3] Deleted {cur.rowcount} opening ADJUST entries')

        # 4. Narrow BSN sync — only product 592, only unit='อน' rows
        rows = conn.execute("""
            SELECT id, doc_no, qty, date_iso
              FROM purchase_transactions
             WHERE product_id=? AND unit='อน' AND synced_to_stock=0
        """, (PRODUCT_ID,)).fetchall()
        sync_count = 0
        for r in rows:
            qty = r['qty'] or 0
            if qty <= 0:
                continue
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, 'IN', ?, 'unit', ?, 'BSN ซื้อ', ?)
            """, (PRODUCT_ID, qty, r['doc_no'], r['date_iso'] + ' 00:00:00'))
            conn.execute(
                'UPDATE purchase_transactions SET synced_to_stock=1 WHERE id=?',
                (r['id'],))
            sync_count += 1
        print(f'[4] Narrow BSN sync: {sync_count} rows for product {PRODUCT_ID} unit=อน')

        # 5. ADJUST to target
        cur_sum = conn.execute(
            'SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?',
            (PRODUCT_ID,)).fetchone()[0]
        adj = TARGET_STOCK - cur_sum
        print(f'[5a] Current ledger sum: {cur_sum}, target: {TARGET_STOCK}, ADJUST: {adj:+}')
        if adj != 0:
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)
            """, (PRODUCT_ID, adj, ADJUST_NOTE,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            print(f'[5b] Inserted ADJUST {adj:+}')

        # 6. Recompute stock_levels from ledger (DELETE doesn't fire any trigger)
        ledger_sum = conn.execute(
            'SELECT COALESCE(SUM(quantity_change), 0) FROM transactions WHERE product_id=?',
            (PRODUCT_ID,)).fetchone()[0]
        conn.execute('UPDATE stock_levels SET quantity=? WHERE product_id=?',
                     (ledger_sum, PRODUCT_ID))
        print(f'[6] Recomputed stock_levels[{PRODUCT_ID}] from ledger: {ledger_sum}')

        # Verify
        post = conn.execute('SELECT quantity FROM stock_levels WHERE product_id=?',
                            (PRODUCT_ID,)).fetchone()
        post_qty = post[0] if post else None
        print(f'After: stock_levels[{PRODUCT_ID}] = {post_qty}')
        if post_qty != TARGET_STOCK:
            print(f'  ❌ Mismatch! Expected {TARGET_STOCK}, got {post_qty} — ROLLING BACK')
            conn.execute('ROLLBACK TO SAVEPOINT k11fix')
            conn.execute('RELEASE SAVEPOINT k11fix')
            return False

        conn.execute('RELEASE SAVEPOINT k11fix')
        conn.commit()
        print('  ✅ committed')
        return True
    except Exception as e:
        print(f'  ❌ Error: {e} — ROLLING BACK')
        conn.execute('ROLLBACK TO SAVEPOINT k11fix')
        conn.execute('RELEASE SAVEPOINT k11fix')
        conn.commit()
        raise
    finally:
        conn.close()


def fix_old_backup(path: Path) -> bool:
    print(f'\n=== Cleaning {path.name} (so future replay won\'t restore wrong entries) ===')
    if not path.exists():
        print(f'  ⚠ Not found — skip')
        return False
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND quantity_change=288 "
        "AND note LIKE 'แถม RR%'",
        (PRODUCT_ID,))
    conn.commit()
    conn.close()
    print(f'  ✅ Deleted {cur.rowcount} wrong manual ADJUSTs from old backup')
    return True


def main():
    print(f'=== K11-1155 (product_id={PRODUCT_ID}) unit conversion fix ===')
    print(f'Target stock: {TARGET_STOCK} (Put confirmed 2026-05-05)\n')

    print(f'Target DB: {DB}\n')
    print('--- Backups ---')
    backup(DB)
    if OLD_BACKUP and OLD_BACKUP.exists():
        backup(OLD_BACKUP)

    if fix_live_db(DB):
        if OLD_BACKUP:
            fix_old_backup(OLD_BACKUP)
        else:
            print('\n(skip old-backup cleanup — non-default DB target)')
        print('\n=== Done ===')
        return 0
    else:
        print('\n=== Live DB fix failed — old backup NOT modified ===')
        return 1


if __name__ == '__main__':
    sys.exit(main())
