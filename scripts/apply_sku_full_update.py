"""DEPRECATED: one-off from 2026-05-07. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply full structured update from rebuilt CSV → products table.

Sets product_name (rename) + 5 new structured columns (series, model, size,
condition, pack_variant). Also fills color_code + packaging where CSV has a
value but DB doesn't (non-destructive: empty CSV cells preserve DB state).

Does NOT touch brand_id / category_id — those have a separate backfill flow
(brand_backfill_suggest.py).

Default mode is DRY-RUN. Use --apply to commit.
audit_products_update trigger captures every change automatically.

CLI:
    python sendy_erp/scripts/apply_sku_full_update.py
    python sendy_erp/scripts/apply_sku_full_update.py --apply
    python sendy_erp/scripts/apply_sku_full_update.py --csv <path> --apply
    python sendy_erp/scripts/apply_sku_full_update.py --limit 10
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_CSV = ROOT / "data" / "exports" / "sku_name_rebuilt_2026-05-07.csv"
DIFF_OUT = ROOT / "data" / "exports" / "sku_full_update_diff.csv"

STRUCTURED_COLS = ["series", "model", "size", "condition", "pack_variant"]
SOFT_FILL_COLS = ["color_code", "packaging"]  # update only if CSV non-empty
ALLOWED_PACKAGING = {  # matches CHECK trigger (post-mig-035)
    "แผง", "ตัว", "ถุง", "แพ็คหัว", "แพ็คถุง",
    "ซอง", "อัดแผง", "แพ็ค", "แบบหลอด", "โหล", "1กลมี60ใบ",
}
NEEDS_REVIEW_OUT = ROOT / "data" / "exports" / "sku_full_update_needs_review.csv"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                   help=f"input CSV (default: {DEFAULT_CSV.relative_to(ROOT)})")
    p.add_argument("--apply", action="store_true", help="commit (default: dry-run)")
    p.add_argument("--limit", type=int, help="limit rows applied (testing)")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")
    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")

    with open(args.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("CSV empty")

    # Load current DB state for affected fields
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    db_rows = conn.execute("""
        SELECT id, sku, product_name, color_code,
               packaging_th AS packaging,
               series, model, size, condition, pack_variant
          FROM products
    """).fetchall()
    by_sku = {str(r["sku"]): dict(r) for r in db_rows}

    # Valid color codes (FK target)
    valid_colors = {row[0] for row in conn.execute("SELECT code FROM color_finish_codes")}

    # Track skipped values for review CSV
    review_rows = []  # (sku, field, value, reason, product_name)

    changes = []
    skipped_no_match = 0
    skipped_unchanged = 0

    for r in rows:
        sku = r["sku"]
        if sku not in by_sku:
            skipped_no_match += 1
            continue

        db = by_sku[sku]
        new_name = (r.get("proposed_name") or "").strip()
        if not new_name:
            continue

        # New structured cols — always take CSV value (may be empty string → NULL)
        new_vals = {c: (r.get(c) or "").strip() or None for c in STRUCTURED_COLS}

        # Soft-fill cols with validation — keep DB if CSV is empty,
        # log + null-out if CSV value invalid (FK miss, CHECK fail).
        for c in SOFT_FILL_COLS:
            csv_v = (r.get(c) or "").strip()
            if not csv_v:
                new_vals[c] = db[c]
                continue
            valid = True
            reason = ""
            if c == "color_code" and csv_v not in valid_colors:
                valid = False
                reason = f"color_code {csv_v!r} not in color_finish_codes"
            elif c == "packaging" and csv_v not in ALLOWED_PACKAGING:
                valid = False
                reason = f"packaging_th {csv_v!r} not in allowed values (mig 087 CHECK trigger)"
            if valid:
                new_vals[c] = csv_v
            else:
                new_vals[c] = db[c]  # keep existing DB value
                review_rows.append((sku, c, csv_v, reason, db["product_name"]))

        # product_name
        new_vals["product_name"] = new_name

        # Detect change vs DB
        diff_fields = {}
        for c in ["product_name"] + STRUCTURED_COLS + SOFT_FILL_COLS:
            old_v = db[c]
            new_v = new_vals[c]
            # treat None and "" as same for comparison
            if (old_v or "") != (new_v or ""):
                diff_fields[c] = (old_v, new_v)

        if not diff_fields:
            skipped_unchanged += 1
            continue

        changes.append({
            "id": db["id"],
            "sku": sku,
            "diff_fields": diff_fields,
            "vals": new_vals,
            "old_name": db["product_name"],
            "new_name": new_name,
        })

    if args.limit:
        changes = changes[: args.limit]

    # Diff CSV
    DIFF_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(DIFF_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "field", "old", "new"])
        for c in changes:
            for field, (old, new) in c["diff_fields"].items():
                w.writerow([c["sku"], field, old or "", new or ""])

    print(f"CSV rows scanned:    {len(rows)}")
    print(f"  no DB match:       {skipped_no_match}")
    print(f"  unchanged:         {skipped_unchanged}")
    print(f"  → would update:    {len(changes)} rows")
    print()

    # Field-level breakdown
    field_counts = {}
    for c in changes:
        for field in c["diff_fields"]:
            field_counts[field] = field_counts.get(field, 0) + 1
    print("Field-level changes:")
    for field, n in sorted(field_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {field:<14} {n}")

    print()
    print("First 3 changes:")
    for c in changes[:3]:
        print(f"  sku={c['sku']:>5}")
        for field, (old, new) in c["diff_fields"].items():
            print(f"    {field:<14} {old!r} → {new!r}")

    print()
    print(f"Diff CSV: {DIFF_OUT.relative_to(ROOT)}")

    # Write needs-review CSV (skipped values that violated FK / CHECK)
    if review_rows:
        with open(NEEDS_REVIEW_OUT, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["sku", "field", "csv_value", "reason", "product_name"])
            for row in review_rows:
                w.writerow(row)
        print(f"Needs review: {NEEDS_REVIEW_OUT.relative_to(ROOT)} ({len(review_rows)} skipped values)")
        print(f"  ⚠ {len(review_rows)} CSV values skipped — DB kept original. Review and decide:")
        from collections import Counter
        ctr = Counter((r[1], r[2]) for r in review_rows)
        for (field, val), n in ctr.most_common(10):
            print(f"    {field:<12} {val!r:<22} ×{n}")

    if not args.apply:
        print()
        print("DRY-RUN. Re-run with --apply to commit.")
        return

    # Apply in single transaction
    print()
    print(f"Applying {len(changes)} updates...")
    conn.execute("PRAGMA foreign_keys = ON")
    # mig 087: packaging → packaging_th + packaging_short pair
    sys.path.insert(0, str(ROOT / "inventory_app"))
    from sku_code_utils import PACKAGING_SHORT
    cur = conn.cursor()
    for c in changes:
        v = c["vals"]
        pkg_th = v["packaging"]
        pkg_short = PACKAGING_SHORT.get(pkg_th) if pkg_th else None
        cur.execute("""
            UPDATE products SET
                product_name    = ?,
                series          = ?,
                model           = ?,
                size            = ?,
                condition       = ?,
                pack_variant    = ?,
                color_code      = ?,
                packaging_th    = ?,
                packaging_short = ?
              WHERE id = ?
        """, (
            v["product_name"], v["series"], v["model"], v["size"],
            v["condition"], v["pack_variant"], v["color_code"], pkg_th, pkg_short,
            c["id"],
        ))
    conn.commit()
    print(f"Done. {len(changes)} rows updated. audit_log captured.")


if __name__ == "__main__":
    main()
