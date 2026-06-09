"""Audit products with incomplete structured columns + write a fixable
review CSV listing each product's gaps and suggested fixes.

Helps batch-fix the ~150 SKUs whose sku_code falls back to '-<sku>'
because they share structured cols with another product (data gap, not
true duplicate).

Severity heuristic (worst first):
  CRITICAL  no brand, no model, no size, no color  → INT-<sku> fallback
  HIGH      no brand AND multiple SKUs share name/sub_category
  MEDIUM    has brand but no model/size to differentiate within sub_category
  LOW       same structured cols as another SKU (true near-duplicate)

Output: sendy_erp/data/exports/data_gaps_audit.csv
Columns:
  product_id, sku_code, product_name, sub_category, severity, gap_summary,
  suggested_brand, suggested_model, suggested_size, suggested_color,
  notes, action_taken (Put fills 'Y' after manual fix)
"""
from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "inventory_app" / "instance" / "inventory.db"
OUT = ROOT / "data" / "exports" / "data_gaps_audit.csv"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    products = conn.execute("""
        SELECT p.id, p.sku_code, p.product_name, p.sub_category,
               p.brand_id, p.model, p.size, p.color_code, p.packaging_th AS packaging,
               p.series, p.pack_variant,
               b.name AS brand_name
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
         WHERE p.is_active = 1
         ORDER BY p.id
    """).fetchall()

    # All brands (longest token first)
    brand_rows = conn.execute(
        "SELECT id, name, name_th, short_code FROM brands"
    ).fetchall()
    brand_tokens = []
    for b in brand_rows:
        for tok in (b['name'], b['name_th'], b['short_code']):
            if tok and len(tok) >= 3:
                brand_tokens.append((tok, b['id'], b['name']))
    brand_tokens.sort(key=lambda x: -len(x[0]))

    # Group products by structured "fingerprint" to find collisions
    fingerprint_groups = defaultdict(list)
    for p in products:
        fp = (
            p['brand_id'],
            (p['model'] or '').strip(),
            (p['size'] or '').strip(),
            (p['color_code'] or '').strip(),
            (p['packaging'] or '').strip(),
            (p['series'] or '').strip(),
            (p['pack_variant'] or '').strip(),
        )
        fingerprint_groups[fp].append(p['id'])

    rows = []
    for p in products:
        # Detect what's missing
        missing = []
        if not p['brand_id']:    missing.append('brand')
        if not p['model']:       missing.append('model')
        if not p['size']:        missing.append('size')
        if not p['color_code']:  missing.append('color')

        # Severity
        if not p['brand_id'] and not p['model'] and not p['size'] and not p['color_code']:
            severity = 'CRITICAL'
        elif not p['brand_id']:
            severity = 'HIGH'
        elif not p['model'] and not p['size']:
            severity = 'MEDIUM'
        else:
            # Check if duplicate fingerprint
            fp = (
                p['brand_id'],
                (p['model'] or '').strip(),
                (p['size'] or '').strip(),
                (p['color_code'] or '').strip(),
                (p['packaging'] or '').strip(),
                (p['series'] or '').strip(),
                (p['pack_variant'] or '').strip(),
            )
            severity = 'LOW' if len(fingerprint_groups[fp]) > 1 else 'OK'

        if severity == 'OK':
            continue  # skip — no gap

        # Suggest brand from product_name (word-boundary match)
        suggested_brand = ''
        if not p['brand_id']:
            for tok, bid, bname in brand_tokens:
                if tok.isascii():
                    if re.search(rf"\b{re.escape(tok)}\b", p['product_name'], re.IGNORECASE):
                        suggested_brand = bname
                        break
                else:
                    if tok in p['product_name']:
                        suggested_brand = bname
                        break

        # Suggest size from product_name (size pattern: digits + unit)
        suggested_size = ''
        if not p['size']:
            m = re.search(r'\d+(?:\.\d+)?(?:/\d+)?\s*(?:in|นิ้ว|mm|cm|m|kg|g|oz)\b',
                          p['product_name'], re.IGNORECASE)
            if m:
                suggested_size = m.group(0).strip()

        # Suggest model from product_name (#NNN or #XX-NNN pattern)
        suggested_model = ''
        if not p['model']:
            m = re.search(r'#[A-Za-z0-9\-/.]+', p['product_name'])
            if m:
                suggested_model = m.group(0)

        gap_summary = ', '.join(missing) if missing else 'duplicate-fingerprint'

        rows.append({
            'product_id':         p['id'],
            'sku_code':           p['sku_code'],
            'severity':           severity,
            'gap_summary':        gap_summary,
            'product_name':       p['product_name'],
            'sub_category':       p['sub_category'] or '',
            'current_brand':      p['brand_name'] or '',
            'current_model':      p['model'] or '',
            'current_size':       p['size'] or '',
            'current_color':      p['color_code'] or '',
            'suggested_brand':    suggested_brand,
            'suggested_model':    suggested_model,
            'suggested_size':     suggested_size,
            'suggested_color':    '',  # color guessing is unreliable; leave blank
            'notes':              '',
            'action_taken':       '',  # Put writes 'Y' after fix
        })

    # Sort: CRITICAL → HIGH → MEDIUM → LOW
    sev_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    rows.sort(key=lambda r: (sev_order[r['severity']], r['product_id']))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(OUT, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # Summary
    by_sev = defaultdict(int)
    for r in rows:
        by_sev[r['severity']] += 1
    total = sum(by_sev.values())
    print(f"Products with structured-col gaps: {total} / {len(products)} ({total*100/len(products):.1f}%)")
    for sev in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
        if by_sev[sev]:
            print(f"  {sev:<10} {by_sev[sev]:>4}")
    print()
    print(f"Output: {OUT.relative_to(ROOT)}")
    print()
    print("Top 10 CRITICAL/HIGH (no brand, suggest from name):")
    sample = [r for r in rows if r['severity'] in ('CRITICAL', 'HIGH') and r['suggested_brand']][:10]
    for r in sample:
        print(f"  product_id={r['product_id']:>5} {r['severity']:<10} suggest={r['suggested_brand']:<15} ← {r['product_name'][:50]}")


if __name__ == '__main__':
    main()
