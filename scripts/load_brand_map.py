"""Load product → brand mappings from a CSV (Put's brand.csv export) and
backfill express_sales.brand_kind.

CSV format (3 columns after a 1-line title):
    กลุ่มสินค้า, ชื่อสินค้า, Brand

Own-brand classification — three brands are own per Put 2026-05-02:
    SENDAI, GOLDEN LION, A-SPEC

Anything else maps to third_party. Empty-brand rows are stored with
is_own=0 so the lookup still succeeds for those product names.

After loading the map, this script also backfills brand_kind on every
existing express_sales row:
    1. exact name lookup in product_brand_map → 'own' / 'third_party'
    2. fallback regex (Sendai|S/D|SD|Golden Lion|GL-|GOLDENLION|A-SPEC|สิงห์)
       → 'own', else 'third_party'

CLI:
    python scripts/load_brand_map.py /Users/putty/Downloads/brand.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

DB = Path('/Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db')

OWN_BRANDS = {'SENDAI', 'GOLDEN LION', 'A-SPEC'}

_FALLBACK_OWN_RE = re.compile(
    r'(?:Sendai|SENDAI|\bSD\b|\bS/D\b|S\.D\.|'
    r'สิงห์|Golden\s*Lion|GOLDEN LION|GOLDENLION|GL-|'
    r'A-?SPEC|ASPEC)',
    re.IGNORECASE,
)


def _classify_fallback(name):
    return 'own' if name and _FALLBACK_OWN_RE.search(name) else 'third_party'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv_path', type=Path)
    args = ap.parse_args()

    if not args.csv_path.exists():
        sys.exit(f'CSV not found: {args.csv_path}')

    rows = []
    with open(args.csv_path, encoding='utf-8') as f:
        reader = csv.reader(f)
        # First row is a title line; second is the header
        next(reader, None)
        next(reader, None)
        for row in reader:
            if len(row) < 3:
                continue
            name = row[1].strip()
            brand = row[2].strip()
            if not name:
                continue
            is_own = 1 if brand.upper() in OWN_BRANDS else 0
            rows.append((name, brand, is_own))

    print(f'Loaded {len(rows)} mappings from {args.csv_path.name}')
    own_n = sum(1 for r in rows if r[2])
    print(f'  own brands : {own_n}')
    print(f'  third_party: {len(rows) - own_n}')

    conn = sqlite3.connect(DB)
    conn.execute('PRAGMA foreign_keys = OFF')

    # Insert mappings (UPSERT)
    conn.execute('DELETE FROM product_brand_map')
    conn.executemany("""
        INSERT OR IGNORE INTO product_brand_map (product_name, brand_name, is_own)
        VALUES (?, ?, ?)
    """, rows)
    conn.commit()
    inserted = conn.execute('SELECT COUNT(*) FROM product_brand_map').fetchone()[0]
    print(f'Inserted into product_brand_map: {inserted} rows')

    # Reset all brand_kind so we re-run with the correct priority order
    # (code-based first beats whatever was there from a previous load).
    conn.execute("UPDATE express_sales SET brand_kind = NULL")
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM express_sales").fetchone()[0]

    # Pass 1 (most authoritative): product_code → product_code_mapping →
    # products.brand_id → brands.is_own_brand. 99.9% match because Express
    # and BSN share the product code system.
    conn.execute("""
        UPDATE express_sales
           SET brand_kind = (
               SELECT CASE WHEN b.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
                 FROM product_code_mapping pcm
                 JOIN products p ON p.id = pcm.product_id
                 JOIN brands b   ON b.id = p.brand_id
                WHERE pcm.bsn_code = express_sales.product_code
           )
         WHERE brand_kind IS NULL
           AND product_code IS NOT NULL
    """)
    conn.commit()
    matched = conn.execute(
        "SELECT COUNT(*) FROM express_sales WHERE brand_kind IS NOT NULL"
    ).fetchone()[0]
    print(f'Backfilled by product_code (Sendy brand): {matched} / {total}')

    # Pass 2: exact product-name match in brand.csv map (catches Express
    # codes not in product_code_mapping or Sendy products without brand_id).
    conn.execute("""
        UPDATE express_sales
           SET brand_kind = (
               SELECT CASE WHEN pbm.is_own = 1 THEN 'own' ELSE 'third_party' END
                 FROM product_brand_map pbm
                WHERE pbm.product_name = express_sales.product_name_raw
           )
         WHERE brand_kind IS NULL
           AND product_name_raw IN (SELECT product_name FROM product_brand_map)
    """)
    conn.commit()
    matched2 = conn.execute(
        "SELECT COUNT(*) FROM express_sales WHERE brand_kind IS NOT NULL"
    ).fetchone()[0]
    print(f'Backfilled by exact name match: {matched2 - matched} more (running total {matched2})')

    # Pass 3: regex fallback for remaining rows
    unmatched = conn.execute(
        "SELECT id, product_name_raw FROM express_sales WHERE brand_kind IS NULL"
    ).fetchall()
    if unmatched:
        regex_rows = [(_classify_fallback(name or ''), id_) for id_, name in unmatched]
        conn.executemany(
            "UPDATE express_sales SET brand_kind = ? WHERE id = ?",
            regex_rows,
        )
        conn.commit()
        print(f'Backfilled by regex: {len(regex_rows)} sales lines')

    # Final check
    summary = conn.execute("""
        SELECT brand_kind, COUNT(*), ROUND(SUM(net), 2)
          FROM express_sales
         GROUP BY brand_kind
    """).fetchall()
    print('\nFinal brand_kind distribution:')
    for kind, n, total_net in summary:
        print(f'  {kind or "(null)":<14s}  n={n:<6d}  net={total_net:>14,.2f}')
    conn.close()


if __name__ == '__main__':
    main()
