"""Auto-suggest product families by grouping SKUs that share
(brand, model_or_subcategory, color).

Per user feedback 2026-05-08:
  - Color is part of the family key — different colors = different cards
    (was 'matrix' before; now split by color)
  - When model is empty, fall back to sub_category for grouping (DRB-GL
    rotary drill bits don't use # naming so model='', use 'ดจ.โรตารี')

For each cluster, analyze which non-color cols vary:
  1 SKU                              → display_format='single'
  vary only on packaging             → 'pack_variants'
  vary only on size                  → 'size_table'
  vary on size AND packaging         → 'size_table' (renderer handles
                                        sub-row for packaging variants)

Family code: <BRAND_SHORT>-<MODEL_OR_SUBCAT_HASH>[-<COLOR_CODE>] when color
is non-NULL. e.g. 'SD-230-AC', 'GL-DRB-ROT' (sub_category-derived).

Output: sendy_erp/data/exports/family_review.csv
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
OUT = ROOT / "data" / "exports" / "family_review.csv"


def _norm_model(m: str) -> str:
    """Normalize model for grouping — strip '#', whitespace, uppercase ASCII."""
    if not m:
        return ""
    return re.sub(r"\s+", "", m.strip().lstrip("#")).upper()


def _subcat_to_code(sub: str) -> str:
    """Build a short ASCII code from Thai sub_category for use in family_code
    when model is empty. Hash-based to keep stable + unique-ish.

    Format: 'SC-<6-char-hex>' — not human-readable but stable across runs.
    Could be replaced with a curated alias dict per sub_category later.
    """
    import hashlib
    h = hashlib.md5(sub.strip().encode("utf-8")).hexdigest()[:6].upper()
    return f"SC{h}"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # products.sku was dropped (mig 097); product_id is the identifier. The
    # "skus" worklist column now carries product_id end-to-end (consumed by
    # apply_family_mapping.py as the product_id key).
    products = conn.execute("""
        SELECT p.id, p.product_name, p.brand_id, p.model, p.size,
               p.color_code, p.packaging_th AS packaging, p.series, p.sub_category, p.family_id,
               b.short_code AS brand_short_code, b.name AS brand_name
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
         WHERE p.is_active = 1
    """).fetchall()

    # Group by (brand_id, model OR sub_category, series)
    # Updated 2026-05-08 (round 3):
    #   - color_code DROPPED from key: different colors of same model/series
    #     now stay in ONE card (e.g. ลูกบิด #5588 PB/SB/SB-PB = 1 card with
    #     color column in the size_table, not 3 cards). Per user catalog
    #     review request 'apply กับอย่างอื่นด้วย'.
    #   - brand_id NULL allowed (round 2): products without brand cluster by
    #     sub_category + series (e.g. กลอนขวางชุบซิงค์ 4in/6in)
    #   - series KEPT in key: ดจ.สแตนเลส กล่องดำ/น้ำเงิน/แดง stay separate
    #     since they're physically distinct product lines, not finish options.
    #   - When model is empty, fall back to sub_category
    groups = defaultdict(list)
    for p in products:
        model_norm = _norm_model(p["model"])
        cluster_key = model_norm or (p["sub_category"] or "").strip()
        if not cluster_key:
            continue
        series = (p["series"] or "").strip()
        # brand_id None → 0 sentinel so no-brand products still group
        brand_key = p["brand_id"] or 0
        key = (brand_key, cluster_key, series)
        groups[key].append(dict(p))

    rows = []
    for (brand_id, cluster_key, series), members in groups.items():
        # Skip groups of 1 — they're "single" style. Could still create
        # a family row, but for trip-prep we focus on multi-SKU families.
        if len(members) < 2:
            continue

        first = members[0]
        # Build family_code: BRAND-MODEL[-SERIES] or BRAND-SC<hash>[-SERIES]
        # When brand is missing, prefix becomes 'NB' (No-Brand).
        # Color no longer in family key — colors stay as columns within the
        # size_table layout (see template).
        if first['model']:
            cluster_seg = _norm_model(first['model'])
        else:
            cluster_seg = _subcat_to_code(first['sub_category'] or '')
        brand_short = first['brand_short_code'] or 'NB'
        family_code = f"{brand_short}-{cluster_seg}"
        if series:
            series_seg = _subcat_to_code(series)[2:6]  # 4-char hex
            family_code = f"{family_code}-S{series_seg}"

        # Display name — sub_category + series suffix when both present
        sub_cats = [m['sub_category'] for m in members if m['sub_category']]
        if sub_cats:
            display_name = max(set(sub_cats), key=sub_cats.count)
            if series:
                display_name = f"{display_name} {series}"
        else:
            display_name = first['product_name']

        # Detect variation axes — color now varies within a family
        sizes = {m['size'] or '' for m in members}
        packs = {m['packaging'] or '' for m in members}
        colors = {m['color_code'] or '' for m in members}
        size_varies = len(sizes) > 1
        pack_varies = len(packs) > 1
        color_varies = len(colors) > 1

        # Display: size_table covers most cases; pack_variants only when ONLY pack varies
        if size_varies or color_varies:
            display_format = 'size_table'
        elif pack_varies:
            display_format = 'pack_variants'
        else:
            display_format = 'single'

        rows.append({
            "proposed_family_code":  family_code,
            "proposed_display_name": display_name,
            "brand_short_code":      first['brand_short_code'],
            "brand_name":             first['brand_name'],
            "cluster_key":           cluster_key,
            "series":                series,
            "sku_count":             len(members),
            "skus":                  ",".join(str(m['id']) for m in sorted(members, key=lambda x: x['id'])),
            "size_varies":           int(size_varies),
            "color_varies":          int(color_varies),
            "pack_varies":           int(pack_varies),
            "proposed_display_format": display_format,
            "user_override_format":  "",
            "user_override_display_name": "",
            "skip":                   "",
            "catalogue_label":        "",
            "notes":                 "",
        })

    rows.sort(key=lambda r: (-r["sku_count"], r["proposed_family_code"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # Summary
    by_format = defaultdict(int)
    sku_total = 0
    for r in rows:
        by_format[r["proposed_display_format"]] += 1
        sku_total += r["sku_count"]
    print(f"Proposed families: {len(rows)} (covering {sku_total} SKUs)")
    print()
    print("Distribution by display_format:")
    for fmt, n in sorted(by_format.items(), key=lambda kv: -kv[1]):
        print(f"  {fmt:<15}  {n:>4} families")
    print()
    print(f"Output: {OUT.relative_to(ROOT)}")
    print()
    print("Top 8 families by SKU count:")
    for r in rows[:8]:
        print(f"  {r['proposed_display_format']:<14} {r['proposed_family_code']:<20} ({r['sku_count']} SKUs)  {r['proposed_display_name'][:40]}")


if __name__ == "__main__":
    main()
