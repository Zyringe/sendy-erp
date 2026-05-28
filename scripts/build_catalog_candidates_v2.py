"""Build catalog candidate list — replaces v1 (which used slide_top_match).
Now uses families + structured cols + stock + own-brand priority.

Output rules:
  - One row per FAMILY (multi-SKU groupings) + one row per singleton SKU
  - Filters: is_active=1 AND (stock>0 OR is_own_brand=1)
  - Excludes 'other' category and SKUs with NULL category
  - Sort: own_brand DESC, brand_name, sub_category, family/sku

Output CSV columns are aligned with display_format expectations from the
catalog renderer (next step). Each row is a candidate "card" for the catalog.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
OUT = ROOT / "data" / "exports" / "catalog_candidates_v2.csv"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 1) Family-level cards: one row per family that has at least one
    #    in-stock OR own-brand SKU.
    families = conn.execute("""
        SELECT pf.id AS family_id,
               pf.family_code,
               pf.display_name,
               pf.display_format,
               pf.catalogue_label,
               b.name AS brand_name,
               b.short_code AS brand_short,
               b.is_own_brand AS is_own,
               COUNT(p.id) AS sku_count,
               SUM(COALESCE(s.quantity, 0)) AS total_stock,
               MAX(p.base_sell_price) AS max_price,
               MIN(p.base_sell_price) AS min_price,
               GROUP_CONCAT(p.sku, ',') AS sku_list,
               GROUP_CONCAT(DISTINCT c.name_th) AS categories,
               GROUP_CONCAT(DISTINCT p.sub_category) AS sub_categories
          FROM product_families pf
          LEFT JOIN brands b ON b.id = pf.brand_id
          LEFT JOIN products p ON p.family_id = pf.id AND p.is_active = 1
          LEFT JOIN stock_levels s ON s.product_id = p.id
          LEFT JOIN categories c ON c.id = p.category_id
         WHERE p.id IS NOT NULL
         GROUP BY pf.id
        HAVING SUM(COALESCE(s.quantity, 0)) > 0
            OR MAX(b.is_own_brand) = 1
    """).fetchall()

    # 2) Singleton cards: products with NO family + matching filter
    singletons = conn.execute("""
        SELECT p.id AS product_id, p.sku, p.sku_code, p.product_name,
               p.sub_category, p.series, p.size, p.color_code, p.packaging_th AS packaging,
               p.base_sell_price, COALESCE(s.quantity, 0) AS stock,
               b.name AS brand_name, b.short_code AS brand_short,
               b.is_own_brand AS is_own,
               c.name_th AS category, c.short_code AS cat_short
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN categories c ON c.id = p.category_id
          LEFT JOIN stock_levels s ON s.product_id = p.id
         WHERE p.is_active = 1
           AND p.family_id IS NULL
           AND (COALESCE(s.quantity, 0) > 0 OR COALESCE(b.is_own_brand, 0) = 1)
           AND (c.code IS NULL OR c.code != 'other')
         ORDER BY b.is_own_brand DESC, b.sort_order, p.sub_category, p.sku
    """).fetchall()

    rows = []
    for f in families:
        rows.append({
            "card_type":          "family",
            "card_id":            f"family-{f['family_id']}",
            "include":            "",  # Put marks Y/N
            "family_code":        f["family_code"],
            "display_name":       f["display_name"],
            "display_format":     f["display_format"],
            "catalogue_label":    f["catalogue_label"] or "",
            "brand_name":         f["brand_name"] or "",
            "is_own_brand":       f["is_own"] or 0,
            "category":           f["categories"] or "",
            "sub_category":       f["sub_categories"] or "",
            "sku_count":          f["sku_count"],
            "total_stock":        f["total_stock"],
            "min_price":          f["min_price"] or 0,
            "max_price":          f["max_price"] or 0,
            "sku_list":           f["sku_list"] or "",
        })
    for s in singletons:
        rows.append({
            "card_type":          "singleton",
            "card_id":            f"sku-{s['sku']}",
            "include":            "",
            "family_code":        s["sku_code"] or "",
            "display_name":       s["product_name"],
            "display_format":     "single",
            "catalogue_label":    "",
            "brand_name":         s["brand_name"] or "",
            "is_own_brand":       s["is_own"] or 0,
            "category":           s["category"] or "",
            "sub_category":       s["sub_category"] or "",
            "sku_count":          1,
            "total_stock":        s["stock"],
            "min_price":          s["base_sell_price"] or 0,
            "max_price":          s["base_sell_price"] or 0,
            "sku_list":           str(s["sku"]),
        })

    # Sort: own-brand first, then by brand_name, then sub_category, then card_type
    rows.sort(key=lambda r: (
        -(r["is_own_brand"] or 0),
        r["brand_name"] or "zzz",
        r["sub_category"] or "",
        r["card_type"],
    ))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # Summary stats
    n_family = sum(1 for r in rows if r["card_type"] == "family")
    n_single = sum(1 for r in rows if r["card_type"] == "singleton")
    n_own = sum(1 for r in rows if r["is_own_brand"])
    sku_in_families = sum(r["sku_count"] for r in rows if r["card_type"] == "family")
    print(f"Total cards proposed: {len(rows)}")
    print(f"  families:        {n_family}  (covering {sku_in_families} SKUs)")
    print(f"  singletons:      {n_single}")
    print(f"  own-brand cards: {n_own}")
    print(f"\nOutput: {OUT.relative_to(ROOT)}")
    print()
    print("Top 8 cards (own-brand first):")
    for r in rows[:8]:
        own_flag = "⭐" if r["is_own_brand"] else "  "
        print(f"  {own_flag} {r['card_type']:<10} {r['display_name'][:40]:<42} {r['brand_name']:<12} stock={r['total_stock']:>4}")


if __name__ == "__main__":
    main()
