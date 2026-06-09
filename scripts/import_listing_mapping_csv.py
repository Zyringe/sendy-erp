"""
One-shot import for listing_mapping_cleaned_20260427.csv.

CSV columns: listing_id, platform, ชื่อสินค้า (platform), ตัวเลือก (platform),
  seller_sku, ราคาตัวอย่าง (฿), Checked, internal_sku, ชื่อสินค้า (ERP),
  sku_ของแถม, qty_per_sale, คำอธิบาย qty_per_sale

Mapping target: ecommerce_listings.id == CSV listing_id (verified).
internal_sku == the OLD integer products.sku — products.sku was dropped (mig
097), so it is translated to product_id via legacy_product_sku_map.

Behavior:
  - Plain row → translate internal_sku (legacy sku) to product_id, UPDATE
    ecommerce_listings (product_id, qty_per_sale).
  - new_sku row → create stub product (auto-increment id, no sku,
    base_sell_price=0, cost_price=0, unit_type='ตัว'), then map.
  - new_sku "same as listing_id N" → defer; map to whatever listing N resolved to.
  - sku_ของแถม present → INSERT INTO listing_bundles (listing_id,
    component_product_id, qty_per_sale=1).
  - For each mapped ecommerce_listings row, propagate to platform_skus where
    (platform, product_name == item_name, variation_name == variation, seller_sku)
    match — otherwise skip (and report).

Run:  python scripts/import_listing_mapping_csv.py [--dry-run] [--csv PATH]
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from pathlib import Path

_INPUT_DIR = os.environ.get('SENDY_INPUT_DIR', os.path.expanduser('~/Downloads'))
DEFAULT_CSV = os.path.join(_INPUT_DIR,
                           'listing_mapping_cleaned_20260427 - '
                           'listing_mapping_cleaned_20260427 (1).csv')

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'inventory_app' / 'instance' / 'inventory.db'


def normalize_int(value):
    s = (value or '').strip()
    return int(s) if s.isdigit() else None


def parse_qty(value):
    s = (value or '').strip()
    if not s:
        return 1.0
    try:
        return float(s)
    except ValueError:
        return 1.0


def is_new_sku(token):
    return 'new_sku' in (token or '').lower()


SAME_AS_RE = re.compile(r'same\s+as\s+listing_id\s+(\d+)', re.IGNORECASE)

# CSV had 3 rows referencing skus that don't exist (1940/1941/1942 — likely a
# product-deletion gap). User confirmed the correct skus on 2026-05-04.
SKU_FIXUPS = {1940: 1315, 1941: 1320, 1942: 1324}


def parse_same_as(token):
    m = SAME_AS_RE.search(token or '')
    return int(m.group(1)) if m else None


def resolve_legacy_sku(conn, sku_int, active_only=False):
    """Translate an OLD integer products.sku (the CSV key) to a product_id via
    the forensic legacy_product_sku_map (products.sku was dropped in mig 097)."""
    if active_only:
        return conn.execute(
            'SELECT m.product_id FROM legacy_product_sku_map m '
            'JOIN products p ON p.id = m.product_id '
            'WHERE m.sku = ? AND p.is_active = 1',
            (sku_int,)
        ).fetchone()
    return conn.execute(
        'SELECT product_id FROM legacy_product_sku_map WHERE sku = ?',
        (sku_int,)
    ).fetchone()


def norm_token(value):
    """Treat literal 'nan' (pandas NaN-cast-to-string) and NULL as the empty string."""
    s = (value or '').strip()
    return '' if s.lower() == 'nan' else s


def _strip_lazada_prefix(value):
    """
    Lazada exports variation as '<attribute_name>:<value>' (e.g.
    'เบอร์:เบอร์ 100 (5แผ่น)') whereas platform_skus stores just the value.
    Strip the leading 'prefix:' if present.
    """
    if not value:
        return value
    if ':' in value:
        head, _, tail = value.partition(':')
        if head and tail and ':' not in head:
            return tail.strip()
    return value


def _try_propagate(conn, listing, product_id, qty, variation, seller_sku):
    cur = conn.execute(
        '''UPDATE platform_skus
           SET internal_product_id = ?, qty_per_sale = ?
           WHERE platform = ?
             AND product_name = ?
             AND CASE WHEN LOWER(COALESCE(variation_name,'')) IN ('','nan')
                      THEN '' ELSE variation_name END
                 = ?
             AND CASE WHEN LOWER(COALESCE(seller_sku,'')) IN ('','nan')
                      THEN '' ELSE seller_sku END
                 = ?''',
        (product_id, qty, listing['platform'], listing['item_name'],
         variation, seller_sku)
    )
    return cur.rowcount


def propagate_to_platform_skus(conn, listing, product_id, qty):
    """
    Match platform_skus rows whose (platform, product_name, variation_name,
    seller_sku) align with the given listing — treating 'nan' / NULL / '' as
    equivalent. Falls back to stripping Lazada 'prefix:' on variation.
    Returns number of rows updated.
    """
    var = norm_token(listing['variation'])
    ssk = norm_token(listing['seller_sku'])
    n = _try_propagate(conn, listing, product_id, qty, var, ssk)
    if n > 0:
        return n
    var2 = _strip_lazada_prefix(var)
    if var2 != var:
        n = _try_propagate(conn, listing, product_id, qty, var2, ssk)
    return n


def create_stub_product(conn, name):
    # products.sku was dropped (mig 097) — new products get an auto-increment
    # id and no sku.
    cur = conn.execute(
        '''INSERT INTO products (product_name, unit_type, cost_price,
                                 base_sell_price, is_active)
           VALUES (?, 'ตัว', 0, 0, 1)''',
        (name,)
    )
    return cur.lastrowid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=DEFAULT_CSV)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not Path(args.csv).exists():
        sys.exit(f'CSV not found: {args.csv}')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')

    with open(args.csv, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    print(f'CSV rows: {len(rows)}')

    stats = dict(updated=0, created_stubs=0, deferred=0, bundles=0,
                 platform_skus_propagated=0, listing_not_found=0,
                 sku_not_found=0, propagate_no_match=0)
    deferred = []
    new_stub_pid_by_listing = {}
    unmatched_listings = []  # (listing_id, platform, item_name, variation, seller_sku, internal_sku_csv)
    sku_not_found_list = []

    for r in rows:
        lid = normalize_int(r['listing_id'])
        if lid is None:
            continue

        listing = conn.execute(
            'SELECT id, platform, item_name, variation, seller_sku '
            'FROM ecommerce_listings WHERE id = ?', (lid,)
        ).fetchone()
        if not listing:
            stats['listing_not_found'] += 1
            continue

        token = (r.get('internal_sku') or '').strip()
        erp_name = (r.get('ชื่อสินค้า (ERP)') or '').strip()
        qty = parse_qty(r.get('qty_per_sale'))
        bundle_sku = (r.get('sku_ของแถม') or '').strip()

        product_id = None

        if is_new_sku(token):
            same_as = parse_same_as(token)
            if same_as is not None:
                deferred.append((lid, same_as, qty, bundle_sku))
                stats['deferred'] += 1
                continue
            if not erp_name:
                print(f'  skip: listing_id={lid} new_sku without ERP name')
                stats['sku_not_found'] += 1
                continue
            existing = conn.execute(
                'SELECT product_id FROM ecommerce_listings WHERE id = ?', (lid,)
            ).fetchone()
            if existing and existing['product_id']:
                product_id = existing['product_id']
                stats['stub_reused'] = stats.get('stub_reused', 0) + 1
            else:
                pid = create_stub_product(conn, erp_name)
                stats['created_stubs'] += 1
                print(f'  stub created: id={pid} "{erp_name}" '
                      f'(listing_id={lid})')
                product_id = pid
                new_stub_pid_by_listing[lid] = pid
        else:
            sku_int = normalize_int(token)
            if sku_int is None:
                stats['sku_not_found'] += 1
                continue
            if sku_int in SKU_FIXUPS:
                sku_int = SKU_FIXUPS[sku_int]
            # CSV is keyed by the OLD integer sku — translate to product_id.
            row = resolve_legacy_sku(conn, sku_int, active_only=True)
            if not row:
                row = resolve_legacy_sku(conn, sku_int)
            if not row:
                print(f'  not found: listing_id={lid} internal_sku={sku_int}')
                stats['sku_not_found'] += 1
                sku_not_found_list.append((lid, listing['platform'],
                                          listing['item_name'], sku_int))
                continue
            product_id = row['product_id']

        conn.execute(
            'UPDATE ecommerce_listings SET product_id = ?, qty_per_sale = ? WHERE id = ?',
            (product_id, qty, lid)
        )
        stats['updated'] += 1

        if bundle_sku:
            bsku = normalize_int(bundle_sku)
            if bsku is not None:
                row = resolve_legacy_sku(conn, bsku)
                if row:
                    conn.execute(
                        '''INSERT OR REPLACE INTO listing_bundles
                           (listing_id, component_product_id, qty_per_sale)
                           VALUES (?, ?, 1)''',
                        (lid, row['product_id'])
                    )
                    stats['bundles'] += 1
                    print(f'  bundle: listing_id={lid} + component_sku={bsku}')
                else:
                    print(f'  bundle skip: listing_id={lid} bundle_sku={bsku} not found')

        cur = propagate_to_platform_skus(conn, listing, product_id, qty)
        if cur > 0:
            stats['platform_skus_propagated'] += cur
        else:
            stats['propagate_no_match'] += 1
            unmatched_listings.append((lid, listing['platform'],
                                      listing['item_name'], listing['variation'],
                                      listing['seller_sku']))

    # Resolve deferred "same as listing_id N" rows
    for lid, same_as, qty, bundle_sku in deferred:
        target = conn.execute(
            'SELECT product_id FROM ecommerce_listings WHERE id = ?', (same_as,)
        ).fetchone()
        if not target or not target['product_id']:
            print(f'  deferred unresolved: listing_id={lid} -> {same_as} (no mapping)')
            continue
        pid = target['product_id']
        conn.execute(
            'UPDATE ecommerce_listings SET product_id = ?, qty_per_sale = ? WHERE id = ?',
            (pid, qty, lid)
        )
        stats['updated'] += 1
        listing = conn.execute(
            'SELECT platform, item_name, variation, seller_sku '
            'FROM ecommerce_listings WHERE id = ?', (lid,)
        ).fetchone()
        cur = propagate_to_platform_skus(conn, listing, pid, qty)
        if cur > 0:
            stats['platform_skus_propagated'] += cur
        else:
            stats['propagate_no_match'] += 1
        if bundle_sku:
            bsku = normalize_int(bundle_sku)
            if bsku is not None:
                row = resolve_legacy_sku(conn, bsku)
                if row:
                    conn.execute(
                        '''INSERT OR REPLACE INTO listing_bundles
                           (listing_id, component_product_id, qty_per_sale)
                           VALUES (?, ?, 1)''',
                        (lid, row['product_id'])
                    )
                    stats['bundles'] += 1
        print(f'  resolved deferred: listing_id={lid} -> {same_as} -> product_id={pid}')

    if args.dry_run:
        conn.rollback()
        print('DRY RUN — rolled back')
    else:
        conn.commit()
        print('Committed')

    print()
    print('Summary:')
    for k, v in stats.items():
        print(f'  {k:30s} {v}')

    # Write reports if there are issues
    exports = ROOT / 'data' / 'exports'
    exports.mkdir(parents=True, exist_ok=True)

    if unmatched_listings:
        path = exports / 'listing_mapping_unmatched_platform_skus.csv'
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['listing_id', 'platform', 'item_name', 'variation', 'seller_sku'])
            w.writerows(unmatched_listings)
        print(f'Unmatched-on-platform_skus report: {path}')

    if sku_not_found_list:
        path = exports / 'listing_mapping_sku_not_found.csv'
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['listing_id', 'platform', 'item_name', 'csv_internal_sku'])
            w.writerows(sku_not_found_list)
        print(f'CSV internal_sku not in products: {path}')

    conn.close()


if __name__ == '__main__':
    main()
