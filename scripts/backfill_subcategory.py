"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Backfill products.sub_category from sku_name_rebuilt CSV's `category` column.

The CSV holds granular Thai type text (e.g. "กลอนมะยม", "บานพับสแตนเลส")
that's more specific than the broad categories table. Stored as plain TEXT
on products for filtering/search; broad category_id is set via a separate
curated mapping (see map_subcategory_to_category.py).
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_CSV = ROOT / "data" / "exports" / "sku_name_rebuilt_2026-05-07.final.csv"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--apply", action="store_true", help="commit (default: dry-run)")
    args = p.parse_args()

    with open(args.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    conn = sqlite3.connect(str(DB_PATH))
    # CSV is keyed by the OLD integer sku (products.sku dropped in mig 097);
    # translate sku → product_id via the forensic legacy map.
    pid_by_sku = {r[0]: r[1] for r in conn.execute(
        "SELECT sku, product_id FROM legacy_product_sku_map")}
    sub_by_pid = {r[0]: r[1] for r in conn.execute(
        "SELECT id, sub_category FROM products")}
    by_sku = {sku: sub_by_pid.get(pid)
              for sku, pid in pid_by_sku.items() if pid in sub_by_pid}

    n_will = n_same = n_no_match = 0
    samples = []
    for r in rows:
        sku = int(r["sku"])
        new_sub = (r["category"] or "").strip()
        if sku not in by_sku:
            n_no_match += 1
            continue
        old_sub = (by_sku[sku] or "").strip()
        if old_sub == new_sub:
            n_same += 1
        else:
            n_will += 1
            if len(samples) < 5:
                samples.append((sku, old_sub, new_sub))

    print(f"CSV rows scanned:        {len(rows)}")
    print(f"  no DB match:           {n_no_match}")
    print(f"  unchanged:             {n_same}")
    print(f"  → will set/update:     {n_will}")
    print()
    print("First 5 changes:")
    for sku, old, new in samples:
        print(f"  sku={sku:>5}  {old!r:<25} → {new!r}")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    cur = conn.cursor()
    for r in rows:
        sku = int(r["sku"])
        if sku not in by_sku:
            continue
        new_sub = (r["category"] or "").strip() or None
        cur.execute("UPDATE products SET sub_category = ? WHERE id = ?",
                    (new_sub, pid_by_sku[sku]))
    conn.commit()
    print(f"\nApplied {n_will} updates")
    conn.close()


if __name__ == "__main__":
    main()
