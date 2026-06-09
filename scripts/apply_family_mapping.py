"""DEPRECATED: one-off from 2026-05-08. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply product_families from family_review.csv:
  - INSERT a product_families row per CSV line (unless skip='Y')
  - UPDATE products.family_id for SKUs in the cluster

Resolution rules per CSV row:
  skip == 'Y'                              → ignore row entirely
  user_override_format set                 → use that as display_format
  user_override_display_name set           → use that as display_name
  else                                     → use proposed_*

Default mode is dry-run. Use --apply to commit.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
DEFAULT_CSV = ROOT / "data" / "exports" / "family_review.csv"

VALID_FORMATS = {'single', 'pack_variants', 'size_table', 'color_swatch', 'matrix'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    with open(args.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Brand short_code → id lookup
    brand_short_to_id = {
        r[0]: r[1] for r in conn.execute(
            "SELECT short_code, id FROM brands WHERE short_code IS NOT NULL"
        )
    }

    # CSV "skus" column holds product_id values (build_family_review.py emits
    # product_id end-to-end now that products.sku was dropped in mig 097).
    n_skip = n_apply = n_already = n_invalid_format = 0
    pid_updates = []  # list of (product_id, family_id) tuples to apply

    for r in rows:
        if (r.get("skip") or "").strip().upper() == "Y":
            n_skip += 1
            continue

        family_code = (r["proposed_family_code"] or "").strip()
        if not family_code:
            n_skip += 1
            continue

        display_name = (r.get("user_override_display_name") or "").strip() \
                       or r["proposed_display_name"]
        display_format = (r.get("user_override_format") or "").strip() \
                         or r["proposed_display_format"]
        if display_format not in VALID_FORMATS:
            n_invalid_format += 1
            continue

        brand_id = brand_short_to_id.get(r["brand_short_code"])
        catalogue_label = (r.get("catalogue_label") or "").strip() or None

        # Check if family already exists
        existing = conn.execute(
            "SELECT id FROM product_families WHERE family_code = ?", (family_code,)
        ).fetchone()
        if existing:
            family_id = existing[0]
            n_already += 1
            if args.apply:
                cur.execute("""
                    UPDATE product_families
                       SET display_name = ?, display_format = ?,
                           catalogue_label = ?, brand_id = ?
                     WHERE id = ?
                """, (display_name, display_format, catalogue_label, brand_id, family_id))
        else:
            n_apply += 1
            if args.apply:
                cur.execute("""
                    INSERT INTO product_families
                      (family_code, display_name, brand_id, display_format, catalogue_label)
                    VALUES (?, ?, ?, ?, ?)
                """, (family_code, display_name, brand_id, display_format, catalogue_label))
                family_id = cur.lastrowid
            else:
                family_id = None

        # Queue product updates (the "skus" column carries product_id values)
        pids = [s.strip() for s in (r.get("skus") or "").split(",") if s.strip()]
        for pid in pids:
            pid_updates.append((int(pid), family_id))

    # Apply product_id → family_id updates
    n_sku_updated = 0
    if args.apply:
        for pid, family_id in pid_updates:
            if family_id is None:
                continue
            cur.execute(
                "UPDATE products SET family_id = ? WHERE id = ?",
                (family_id, pid)
            )
            n_sku_updated += cur.rowcount
        conn.commit()

    print(f"CSV rows scanned:           {len(rows)}")
    print(f"  skipped (skip='Y' / empty): {n_skip}")
    print(f"  invalid display_format:     {n_invalid_format}")
    print(f"  family already exists:      {n_already}")
    print(f"  → would INSERT new family:  {n_apply}")
    print(f"  → SKU→family_id updates:    {len(pid_updates)}")
    if args.apply:
        print()
        print(f"Applied: {n_sku_updated} SKUs linked to families")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    # Coverage report
    cov = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM product_families) AS families,
          (SELECT COUNT(*) FROM products WHERE family_id IS NOT NULL) AS skus_with_family,
          (SELECT COUNT(*) FROM products) AS total_skus
    """).fetchone()
    print(f"\nFinal: {cov[0]} families, {cov[1]}/{cov[2]} ({cov[1]*100/cov[2]:.1f}%) SKUs have family_id")
    conn.close()


if __name__ == "__main__":
    main()
