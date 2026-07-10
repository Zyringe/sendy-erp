"""One-off, 2026-07-10.

Bucket-2b follow-up from Put's /grilling session — this one turned out to be
NOT a "generic vs specific variant" case (that's the real bucket-2b problem,
left as a design question for db-architect; see the session transcript). It's
just another simple wrong-sibling mapping, same pattern as
fix_wrong_sibling_mapping_20260710.py: pid 1406 ("บานพับสีทอง GP #413 (ตัว)")
has ZERO invoice history ever, while its pack sibling pid 2005 ("...#413
(แผง)") has 20 real invoices. Both sides have zero warehouse stock (unlike
the rivet/nozzle-head cases), so there is no inventory-corruption risk from
remapping — confirmed order_sn 240103VUTGB29B, cited in PR #280's report as
"pre-existing staleness, not caused by this PR", now correctly resolves to
IV6700029.

3 platform_skus listings (1 Lazada, 2 Shopee) + 19 historical order_items
backfilled (Lazada via variation_id join; Shopee via (item_name,
variation_name) since Shopee exports no SKU id for these two listings).

Already applied directly to local+prod DBs (dry-run verified via delta
simulation first: 0 lost, 0 changed, +12 Shopee / +1 Lazada newly matched).
Kept for audit trail per existing scripts/apply_*.py convention.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
BACKUP_DIR = ROOT / "data" / "backups"

OLD_PID = 1406
NEW_PID = 2005
PLATFORM_SKUS_IDS = (1154, 1543, 1625)

_SHOPEE_ITEM_A = ('บานพับสีทอง บานพับประตู แหวนลูกปืน  4" สีทอง', 'No.413 (แกนใหญ่)')
_SHOPEE_ITEM_B = ('บานพับสีทอง บานพับประตู แหวนลูกปืน 4" แกนใหญ่ หนา 3.2มิล', None)
_LAZADA_VARIATION_ID = '2157778976_TH-7187066238'


def _verify_safety(conn):
    row = conn.execute(
        "SELECT COUNT(*) c FROM sales_transactions WHERE product_id=? AND doc_base LIKE 'IV%'",
        (OLD_PID,)).fetchone()
    if row["c"] > 0:
        raise SystemExit(f"SAFETY CHECK FAILED: pid {OLD_PID} has real invoice history")


def preview(conn):
    _verify_safety(conn)
    for sku_id in PLATFORM_SKUS_IDS:
        row = conn.execute(
            "SELECT platform, product_name, variation_name, internal_product_id "
            "FROM platform_skus WHERE id=?", (sku_id,)).fetchone()
        print(f"  platform_skus id={sku_id} {row['platform']} {row['product_name'][:40]!r} "
              f"{row['variation_name']!r}: {row['internal_product_id']} -> {NEW_PID}")

    n_lazada = conn.execute(
        "SELECT COUNT(*) c FROM marketplace_order_items WHERE platform='lazada' "
        "AND variation_id=? AND internal_product_id=?", (_LAZADA_VARIATION_ID, OLD_PID)).fetchone()
    n_shopee_a = conn.execute(
        "SELECT COUNT(*) c FROM marketplace_order_items WHERE platform='shopee' AND item_name=? "
        "AND variation_name=? AND internal_product_id=?", (*_SHOPEE_ITEM_A, OLD_PID)).fetchone()
    n_shopee_b = conn.execute(
        "SELECT COUNT(*) c FROM marketplace_order_items WHERE platform='shopee' AND item_name=? "
        "AND variation_name IS NULL AND internal_product_id=?", (_SHOPEE_ITEM_B[0], OLD_PID)).fetchone()
    print(f"  order_items to backfill: lazada={n_lazada['c']} shopee_a={n_shopee_a['c']} "
          f"shopee_b={n_shopee_b['c']}")
    return n_lazada["c"] + n_shopee_a["c"] + n_shopee_b["c"]


def apply(conn):
    _verify_safety(conn)
    for sku_id in PLATFORM_SKUS_IDS:
        conn.execute("UPDATE platform_skus SET internal_product_id=? WHERE id=?", (NEW_PID, sku_id))
    conn.execute(
        "UPDATE marketplace_order_items SET internal_product_id=? "
        "WHERE platform='lazada' AND variation_id=? AND internal_product_id=?",
        (NEW_PID, _LAZADA_VARIATION_ID, OLD_PID))
    conn.execute(
        "UPDATE marketplace_order_items SET internal_product_id=? "
        "WHERE platform='shopee' AND item_name=? AND variation_name=? AND internal_product_id=?",
        (NEW_PID, *_SHOPEE_ITEM_A, OLD_PID))
    conn.execute(
        "UPDATE marketplace_order_items SET internal_product_id=? "
        "WHERE platform='shopee' AND item_name=? AND variation_name IS NULL AND internal_product_id=?",
        (NEW_PID, _SHOPEE_ITEM_B[0], OLD_PID))


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

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"pre_hinge413_fix_{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(args.db))
    dst = sqlite3.connect(str(backup_path))
    src.backup(dst)
    src.close()
    dst.close()
    print(f"\nBackup written: {backup_path}")

    conn.execute("BEGIN IMMEDIATE")
    apply(conn)
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) c FROM marketplace_order_items WHERE internal_product_id=?", (OLD_PID,)).fetchone()
    bad = 0
    for sku_id in PLATFORM_SKUS_IDS:
        row = conn.execute("SELECT internal_product_id FROM platform_skus WHERE id=?", (sku_id,)).fetchone()
        if row["internal_product_id"] != NEW_PID:
            bad += 1
    print(f"\nApplied. Remaining order_items at old pid (should be 0): {remaining['c']}, "
          f"platform_skus mismatches (should be 0): {bad}")
    if remaining["c"] or bad:
        raise SystemExit("FAILED verification")
    conn.close()


if __name__ == "__main__":
    main()
