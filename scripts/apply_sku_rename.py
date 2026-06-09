"""Apply SKU rename from a parsed/edited CSV.

Reads `sku_name_parsed.csv` (or any CSV with at minimum `sku` + the rename
column) and writes the new name to `products.product_name`.

Default mode is DRY-RUN. Use --apply to commit. The audit_products_update
trigger captures every UPDATE in audit_log automatically.

CLI:
    python sendy_erp/scripts/apply_sku_rename.py
    python sendy_erp/scripts/apply_sku_rename.py --apply
    python sendy_erp/scripts/apply_sku_rename.py --csv /tmp/edited.csv --apply
    python sendy_erp/scripts/apply_sku_rename.py --column custom_name
    python sendy_erp/scripts/apply_sku_rename.py --sku-min 1 --sku-max 500
    python sendy_erp/scripts/apply_sku_rename.py --brand Sendai --apply
    python sendy_erp/scripts/apply_sku_rename.py --category-contains กลอน

Filters compose (AND).
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_CSV = ROOT / "data" / "exports" / "sku_name_parsed.csv"
DEFAULT_DIFF = ROOT / "data" / "exports" / "sku_rename_diff.csv"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                   help=f"CSV with sku + rename column (default: {DEFAULT_CSV})")
    p.add_argument("--column", default="proposed_name",
                   help="CSV column to use as the new product_name (default: proposed_name)")
    p.add_argument("--apply", action="store_true",
                   help="commit changes to DB (default: dry-run)")
    p.add_argument("--diff-out", type=Path, default=DEFAULT_DIFF,
                   help=f"output CSV listing planned changes (default: {DEFAULT_DIFF})")
    p.add_argument("--sku-min", type=int, help="filter: SKU >= this value")
    p.add_argument("--sku-max", type=int, help="filter: SKU <= this value")
    p.add_argument("--brand", help="filter: brand name (case-insensitive substring match against current brand)")
    p.add_argument("--category-contains", help="filter: CSV.category contains this substring")
    p.add_argument("--limit", type=int, help="limit number of rows applied (for testing)")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")

    # Load CSV
    with open(args.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        sys.exit(f"CSV is empty: {args.csv}")

    if args.column not in rows[0]:
        sys.exit(f"Column {args.column!r} not found in CSV. "
                 f"Available: {list(rows[0].keys())}")

    # Load current names from DB. The CSV is keyed by the OLD integer sku
    # (products.sku dropped in mig 097); key by_sku off the forensic legacy map
    # so an existing CSV's sku column still resolves to a product row.
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    db_rows = conn.execute(
        "SELECT p.id, m.sku, p.product_name, p.brand_id, b.name AS brand_name "
        "FROM products p "
        "JOIN legacy_product_sku_map m ON m.product_id = p.id "
        "LEFT JOIN brands b ON b.id = p.brand_id"
    ).fetchall()
    by_sku = {str(r["sku"]): dict(r) for r in db_rows}

    # Apply filters
    filtered = []
    skipped_no_match = 0
    skipped_filter = 0
    skipped_empty = 0
    skipped_unchanged = 0

    for r in rows:
        sku = r["sku"]
        if sku not in by_sku:
            skipped_no_match += 1
            continue

        if args.sku_min is not None and int(sku) < args.sku_min:
            skipped_filter += 1
            continue
        if args.sku_max is not None and int(sku) > args.sku_max:
            skipped_filter += 1
            continue
        if args.brand:
            db_brand = (by_sku[sku].get("brand_name") or "").lower()
            if args.brand.lower() not in db_brand:
                skipped_filter += 1
                continue
        if args.category_contains:
            cat = (r.get("category") or "")
            if args.category_contains not in cat:
                skipped_filter += 1
                continue

        new_name = (r.get(args.column) or "").strip()
        if not new_name:
            skipped_empty += 1
            continue

        old_name = by_sku[sku]["product_name"]
        if new_name == old_name:
            skipped_unchanged += 1
            continue

        filtered.append({
            "id": by_sku[sku]["id"],
            "sku": sku,
            "old_name": old_name,
            "new_name": new_name,
            "brand": by_sku[sku].get("brand_name") or "",
        })

    # Apply --limit
    if args.limit:
        filtered = filtered[: args.limit]

    # Write diff CSV (for review)
    args.diff_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.diff_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "brand", "old_name", "new_name"])
        w.writeheader()
        for c in filtered:
            w.writerow({k: c[k] for k in ("sku", "brand", "old_name", "new_name")})

    # Report
    print(f"CSV rows scanned:     {len(rows)}")
    print(f"  no DB match:        {skipped_no_match}")
    print(f"  filtered out:       {skipped_filter}")
    print(f"  empty {args.column}:  {skipped_empty}")
    print(f"  unchanged:          {skipped_unchanged}")
    print(f"  → would update:     {len(filtered)}")
    print(f"Diff CSV:             {args.diff_out}")
    print()

    if not filtered:
        print("Nothing to apply.")
        return

    print("First 8 changes:")
    for c in filtered[:8]:
        print(f"  [{c['sku']:>5}] {c['brand']}")
        print(f"        - {c['old_name'][:75]}")
        print(f"        + {c['new_name'][:75]}")

    if not args.apply:
        print()
        print("DRY-RUN — no DB changes made. Re-run with --apply to commit.")
        print("STRONGLY RECOMMENDED: backup first")
        print("    bash ~/Sendai-Boonsawat/sendy_erp/scripts/backup_db.sh")
        return

    # Apply
    print()
    print(f"Applying {len(filtered)} renames...")
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    for c in filtered:
        cur.execute(
            "UPDATE products SET product_name = ? WHERE id = ?",
            (c["new_name"], c["id"]),
        )
    conn.commit()
    print(f"Done. {len(filtered)} rows updated. audit_log captures every change.")


if __name__ == "__main__":
    main()
