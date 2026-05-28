"""Generate products.sku_code from structured columns.

Format: <CAT>-<BRAND>-<MODEL>-<SIZE>-<COLOR>
        + optional pack-variant suffix (-N) for disambiguation

Segments are omitted when missing. Fallback when nothing structured:
  INT-<sku>  (uses the legacy integer SKU)

Skips products where sku_code_locked = 1 (user manually edited — preserve).
On collision (rare), appends -<sku> as last-resort disambiguation.

Default mode is dry-run. Use --apply to commit.
Use --regen to also overwrite existing sku_code on unlocked rows.
By default only writes sku_code where it's currently NULL.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"

# Use the canonical generator from inventory_app — keeps the 10-slot rule
# (subcat, condition, pack_variant=1 suppression) in lockstep with the
# Flask app + normalize/apply scripts. Local duplicate dropped 2026-05-28.
sys.path.insert(0, str(ROOT / "inventory_app"))
from sku_code_utils import build_sku_code  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--regen", action="store_true",
                   help="Regenerate sku_code on unlocked rows (default: only fill NULL)")
    args = p.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT p.id, p.sku, p.sku_code, p.sku_code_locked, p.model, p.size,
               p.series, p.color_code, p.packaging_th, p.packaging_short,
               p.condition, p.pack_variant, p.sub_category_short_code,
               b.short_code AS brand_short_code,
               c.short_code AS cat_short_code
          FROM products p
          LEFT JOIN brands b      ON b.id = p.brand_id
          LEFT JOIN categories c  ON c.id = p.category_id
    """).fetchall()

    used_codes = set(
        row[0] for row in conn.execute(
            "SELECT sku_code FROM products WHERE sku_code IS NOT NULL"
        )
    )

    proposed = []
    n_skip_locked = 0
    n_skip_existing = 0
    n_collide = 0

    for r in rows:
        if r["sku_code_locked"]:
            n_skip_locked += 1
            continue
        if r["sku_code"] and not args.regen:
            n_skip_existing += 1
            continue

        new_code = build_sku_code(dict(r))

        # Collision check — append -sku as disambiguator
        original = new_code
        if new_code in used_codes and new_code != r["sku_code"]:
            new_code = f"{original}-{r['sku']}"
            n_collide += 1

        # Don't update if same as existing
        if new_code == r["sku_code"]:
            continue

        proposed.append({
            "id": r["id"],
            "sku": r["sku"],
            "old": r["sku_code"],
            "new": new_code,
        })
        used_codes.discard(r["sku_code"])
        used_codes.add(new_code)

    print(f"Total products:         {len(rows)}")
    print(f"  skip locked:          {n_skip_locked}")
    print(f"  skip existing (use --regen to overwrite): {n_skip_existing}")
    print(f"  → would update:       {len(proposed)}")
    print(f"  collisions resolved:  {n_collide}")
    print()
    print("First 8 proposals:")
    for x in proposed[:8]:
        print(f"  sku={x['sku']:>5}  {x['old'] or '(NULL)':<40} → {x['new']}")
    print()
    print("Sample of fallback (no structured data):")
    for x in proposed:
        if x['new'].startswith('INT-'):
            print(f"  sku={x['sku']:>5}  → {x['new']}")
            break

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    cur = conn.cursor()
    for x in proposed:
        cur.execute("UPDATE products SET sku_code = ? WHERE id = ?", (x["new"], x["id"]))
    conn.commit()
    print(f"\nApplied {len(proposed)} updates")

    # Coverage report
    by_cov = conn.execute("""
        SELECT
          SUM(CASE WHEN sku_code IS NOT NULL THEN 1 ELSE 0 END) AS with_code,
          SUM(CASE WHEN sku_code LIKE 'INT-%' THEN 1 ELSE 0 END) AS fallback,
          COUNT(*) AS total
          FROM products
    """).fetchone()
    print(f"Coverage: {by_cov['with_code']}/{by_cov['total']} have sku_code "
          f"({by_cov['fallback']} are INT-<sku> fallback)")
    conn.close()


if __name__ == "__main__":
    main()
