"""One-off, 2026-07-10.

Bucket-2a fix from Put's /grilling session (follow-up to
resync_stale_order_item_pid_20260710.py). That script fixed listings where
platform_skus was ALREADY correct but order_items lagged behind. This script
fixes listings where platform_skus ITSELF is still wrong right now — a
genuine remap, not a resync — but only for cases confirmed SAFE by evidence:
the currently-mapped (old) product has ZERO invoice history EVER, company-wide
(not just this marketplace customer_code), so there is no historical period
it could legitimately belong to (rules out the temporal-changeover risk found
in the rivet-family case, and in the door-knob #5588 SB/PB case, which is
deliberately EXCLUDED from this file — its old pid (194) has 10 real invoices
including one that is ALREADY correctly matched, spread across the whole
2024-2026 window with no clean cutoff, so no confident blanket fix exists;
left untouched pending per-order investigation).

Confirmed via evidence (see the /grilling session transcript):
  - pid 163 "สายยูประกบอย่างดีหนา Sendai 3mm สีรมดำ (AC) (ตัว)": 0 invoices ever.
    Real sibling pid 1600 "สายยูประกบ Sendai #100-3mm สีรมดำ (AC)": 21 invoices
    2024-03..2025-09. Shopee's nearest invoice candidate for this listing is
    an EXACT date+amount match on 1600.
  - pid 164 "...สีโครเมียม (CR) (ตัว)": 0 invoices ever. Sibling pid 1601
    "...#100-3mm สีโครเมียม (CR)": 6 invoices, same window.

Fixes BOTH platform_skus (future imports) AND backfills existing
marketplace_order_items (historical orders) for the SAME (old pid has zero
history) safety bar as the bucket-1 script. Shopee rows here have no
variation_id (text-fallback resolution, see
models/marketplace.py::resolve_marketplace_product_id) so the order_items
backfill joins on (product_name, variation_name) for those; Lazada rows join
on variation_id.

Dry-run by default. --apply commits. Backs up first (sqlite3 .backup, not cp
— WAL).
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUP_DIR = ROOT / "data" / "backups"

# (platform_skus.id, new_pid) — the listing-level fix (future imports).
PLATFORM_SKUS_FIXES = [
    (1965, 1600),  # shopee, สายยู 3 มิล AC
    (1257, 1600),  # lazada, same listing (no variation split)
    (1964, 1601),  # shopee, สายยู 3 มิล CR
]

# Historical order_items backfill: (platform, join_type, join_value, old_pid, new_pid)
# join_type 'variation_id' or 'name' (product_name, variation_name).
ORDER_ITEM_BACKFILLS = [
    ("lazada", "variation_id", "2931310368_TH-10746038556", 163, 1600),
    ("shopee", "name",
     ("สายยูคล้องกุญแจ สายยูหูช้างอย่างดี  SENDAI หนาพิเศษ แข็งแรง ทนทาน", "สายยู 3 มิล AC"),
     163, 1600),
]


def _ever_invoiced_anywhere(conn, pid):
    row = conn.execute(
        "SELECT COUNT(*) c FROM sales_transactions WHERE product_id=? AND doc_base LIKE 'IV%'",
        (pid,),
    ).fetchone()
    return row["c"] > 0


def _verify_safety(conn):
    """Fail loudly if any 'old' pid in this file's fix list turns out to have
    real invoice history — the whole point of this script is that they don't."""
    old_pids = {163, 164}
    for pid in old_pids:
        if _ever_invoiced_anywhere(conn, pid):
            raise SystemExit(f"SAFETY CHECK FAILED: pid {pid} has real invoice "
                              f"history — do not apply this script blindly.")


def preview(conn):
    _verify_safety(conn)
    print("platform_skus fixes:")
    for sku_id, new_pid in PLATFORM_SKUS_FIXES:
        row = conn.execute(
            "SELECT platform, product_name, variation_name, internal_product_id "
            "FROM platform_skus WHERE id=?", (sku_id,)).fetchone()
        print(f"  id={sku_id} {row['platform']} {row['product_name'][:40]!r} "
              f"{row['variation_name']!r}: {row['internal_product_id']} -> {new_pid}")

    print("\norder_items backfills:")
    total = 0
    for platform, join_type, join_value, old_pid, new_pid in ORDER_ITEM_BACKFILLS:
        if join_type == "variation_id":
            rows = conn.execute(
                "SELECT id, order_sn FROM marketplace_order_items "
                "WHERE platform=? AND variation_id=? AND internal_product_id=?",
                (platform, join_value, old_pid)).fetchall()
        else:
            pname, vname = join_value
            rows = conn.execute(
                "SELECT id, order_sn FROM marketplace_order_items "
                "WHERE platform=? AND item_name=? AND IFNULL(variation_name,'')=IFNULL(?,'') "
                "AND internal_product_id=?",
                (platform, pname, vname, old_pid)).fetchall()
        print(f"  {platform} {join_type}={join_value!r}: {len(rows)} rows, "
              f"{old_pid} -> {new_pid}")
        for r in rows:
            print(f"    order_sn={r['order_sn']}")
        total += len(rows)
    return total


def apply(conn):
    _verify_safety(conn)
    for sku_id, new_pid in PLATFORM_SKUS_FIXES:
        conn.execute("UPDATE platform_skus SET internal_product_id=? WHERE id=?",
                     (new_pid, sku_id))
    for platform, join_type, join_value, old_pid, new_pid in ORDER_ITEM_BACKFILLS:
        if join_type == "variation_id":
            conn.execute(
                "UPDATE marketplace_order_items SET internal_product_id=? "
                "WHERE platform=? AND variation_id=? AND internal_product_id=?",
                (new_pid, platform, join_value, old_pid))
        else:
            pname, vname = join_value
            conn.execute(
                "UPDATE marketplace_order_items SET internal_product_id=? "
                "WHERE platform=? AND item_name=? AND IFNULL(variation_name,'')=IFNULL(?,'') "
                "AND internal_product_id=?",
                (new_pid, platform, pname, vname, old_pid))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    total = preview(conn)

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to commit.")
        conn.close()
        return

    if total == 0:
        print("\n(platform_skus fixes will still apply even with 0 order_items rows)")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"pre_sibling_fix_{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(args.db))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    src.close()
    dst.close()
    print(f"\nBackup written: {backup_path}")

    conn.execute("BEGIN IMMEDIATE")
    apply(conn)
    conn.commit()

    # Independent re-verify.
    bad = 0
    for sku_id, new_pid in PLATFORM_SKUS_FIXES:
        row = conn.execute("SELECT internal_product_id FROM platform_skus WHERE id=?",
                            (sku_id,)).fetchone()
        if row["internal_product_id"] != new_pid:
            bad += 1
    for platform, join_type, join_value, old_pid, new_pid in ORDER_ITEM_BACKFILLS:
        if join_type == "variation_id":
            remaining = conn.execute(
                "SELECT COUNT(*) c FROM marketplace_order_items "
                "WHERE platform=? AND variation_id=? AND internal_product_id=?",
                (platform, join_value, old_pid)).fetchone()
        else:
            pname, vname = join_value
            remaining = conn.execute(
                "SELECT COUNT(*) c FROM marketplace_order_items "
                "WHERE platform=? AND item_name=? AND IFNULL(variation_name,'')=IFNULL(?,'') "
                "AND internal_product_id=?",
                (platform, pname, vname, old_pid)).fetchone()
        bad += remaining["c"]
    print(f"\nApplied. Remaining wrong-value rows (should be 0): {bad}")
    if bad:
        raise SystemExit(f"FAILED verification: {bad} rows still wrong")
    conn.close()


if __name__ == "__main__":
    main()
