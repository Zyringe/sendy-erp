"""
DEPRECATED: one-off from 2026-05-17. Kept for audit trail. Do not re-run;
if a similar fix is needed, write a new dated script.

Apply mapping + compute platform stock from product_platform_overview CSV.

CSV: $SENDY_INPUT_DIR/product_platform_overview_*.csv (defaults to ~/Downloads)
CSV columns:
  Platform, platform_sku_id, Parent ID, Variation ID, Listing name, Variation,
  Seller SKU, Stock, Suggested SKU, Suggested name, Confidence, checked,
  → internal_sku (fill in), → qty_per_sale (default 1), sku_ของแถม,
  qty_sku_ของแถม, Note

Behavior
--------
1. Skip rows where checked != 'TRUE'.
2. Skip rows where internal_sku is empty, OR equals 'new_sku' with empty
   Suggested name.
3. internal_sku == 'new_sku' (with Suggested name) → create stub product
   (auto-increment id, no sku, base_sell_price=0, cost_price=0,
   unit_type='ตัว', is_active=1, product_name=Suggested name).
4. UPDATE platform_skus by id:
     internal_product_id ← resolved
     qty_per_sale         ← from CSV (default 1)
     stock                ← computed
5. Bundle stock (sku_ของแถม non-empty):
     main_avail = floor(stock_levels[internal] / qty_per_sale)
     comp_avail = floor(stock_levels[component] / qty_sku_ของแถม)
     stock      = max(0, min(main_avail, comp_avail))
   (Bundle relationship is NOT persisted to listing_bundles — see CLAUDE note.)
6. Platform-stock cap (CSV "Stock" column = current Shopee/Lazada stock):
     final_stock = min(computed_stock, csv_current_platform_stock)
   This prevents pushing INFLATED DB values when platform stock was already
   set lower (e.g. Putty manually capped at 98 on Shopee while DB has 402 due
   to opening-balance ADJUST overestimates).
6. Generate Shopee + Lazada upload xlsx via parse_platform.export_*().
   Saved to data/exports/.
7. --dry-run rolls back the transaction; upload files NOT written.

Run
---
  python scripts/apply_platform_overview_mapping.py [--dry-run] [--csv PATH]
"""
import argparse
import csv
import datetime
import os
import re
import sqlite3
import sys
from math import floor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'inventory_app' / 'instance' / 'inventory.db'
EXPORTS = ROOT / 'data' / 'exports'

sys.path.insert(0, str(ROOT / 'inventory_app'))
from parse_platform import export_shopee, export_lazada  # noqa: E402

_INPUT_DIR = os.environ.get('SENDY_INPUT_DIR', os.path.expanduser('~/Downloads'))
DEFAULT_CSV = os.path.join(_INPUT_DIR,
                           'product_platform_overview_20260504.xlsx - '
                           'To Map (Active + In Stock) (1).csv')

COL_INTERNAL_SKU = '→ internal_sku (fill in)'
COL_QTY_PER_SALE = '→ qty_per_sale (default 1)'
COL_FREEBIE_SKU = 'sku_ของแถม'
COL_FREEBIE_QTY = 'qty_sku_ของแถม'

# Pack-size patterns: when listing name / variation contains "(N <unit>)" the
# qty_per_sale should match N. We auto-correct CSV-supplied qty_per_sale when
# it disagrees with the detected pack-size, to prevent overselling.
_PACK_PATTERNS = [
    r'\((\d+)\s*ดอก\)',
    r'\((\d+)\s*ใบ\s*/\s*แพ็ค\)', r'\((\d+)\s*ใบ\)',
    r'\((\d+)\s*แผ่น\)', r'\((\d+)\s*ตัว\)',
    r'\((\d+)\s*ซอง\s*/\s*แพ็ค\)', r'\((\d+)\s*ซอง\)',
    r'\((\d+)\s*อัน\)', r'\((\d+)\s*ม้วน\)',
    r'\((\d+)\s*ก\.?\s*ก\.?\)',  # (1 กก.)
    r'\((\d+)\s*กล่อง\)', r'\((\d+)\s*ชุด\)',
    r'แพ็ค\s*(\d+)\s*ใบ', r'แพ็ค\s*(\d+)\s*แผ่น',
    r'ชุดละ\s*(\d+)\s*แผ่น',
]


def detect_pack_size(listing_name, variation):
    text = f'{listing_name} {variation or ""}'
    for pat in _PACK_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def parse_int(value):
    s = (value or '').strip()
    return int(s) if s.isdigit() else None


def parse_qty(value, default=1.0):
    s = (value or '').strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def resolve_legacy_sku(conn, sku_int):
    """Translate an OLD integer products.sku (the xlsx key column) to a
    product_id via the forensic legacy_product_sku_map (products.sku dropped
    in mig 097)."""
    return conn.execute(
        'SELECT product_id FROM legacy_product_sku_map WHERE sku = ?', (sku_int,)
    ).fetchone()


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


def get_stock(conn, product_id):
    row = conn.execute(
        'SELECT quantity FROM stock_levels WHERE product_id = ?', (product_id,)
    ).fetchone()
    return float(row[0]) if row else 0.0


def compute_platform_stock(conn, internal_pid, qty_per_sale,
                           component_pid=None, component_qty=None):
    main_stock = get_stock(conn, internal_pid)
    main_avail = floor(main_stock / qty_per_sale) if qty_per_sale > 0 else 0
    if component_pid is not None and component_qty and component_qty > 0:
        comp_stock = get_stock(conn, component_pid)
        comp_avail = floor(comp_stock / component_qty)
        return max(0, min(main_avail, comp_avail))
    return max(0, main_avail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=DEFAULT_CSV)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f'CSV not found: {csv_path}')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')

    with open(csv_path, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    print(f'CSV rows: {len(rows)}')
    print(f'DB:       {DB_PATH}')
    print(f'Mode:     {"DRY-RUN" if args.dry_run else "APPLY"}')
    print()

    stats = dict(
        skipped_unchecked=0, skipped_empty=0, skipped_new_sku_no_name=0,
        created_stubs=0, sku_not_found=0, platform_sku_not_found=0,
        updated=0, bundles_applied=0, bundle_component_not_found=0,
        qty_auto_corrected=0, stock_capped_by_csv=0,
    )
    sku_not_found_list = []
    platform_sku_not_found_list = []
    bundle_skip_list = []
    qty_corrections = []  # (pid, platform, sku, csv_qty, detected, listing)

    # collect rows that we will export at the end (post-update)
    affected_pids = []
    stub_products_created = []  # [(id, name)]

    for r in rows:
        if r.get('checked', '').strip().upper() != 'TRUE':
            stats['skipped_unchecked'] += 1
            continue

        platform_sku_id = parse_int(r['platform_sku_id'])
        if platform_sku_id is None:
            stats['skipped_empty'] += 1
            continue

        ps_row = conn.execute(
            '''SELECT id, platform, product_id_str, product_name, variation_id,
                      variation_name, seller_sku, internal_product_id
               FROM platform_skus WHERE id = ?''', (platform_sku_id,)
        ).fetchone()
        if not ps_row:
            stats['platform_sku_not_found'] += 1
            platform_sku_not_found_list.append(platform_sku_id)
            continue

        token = (r[COL_INTERNAL_SKU] or '').strip()
        suggested_name = (r['Suggested name'] or '').strip()

        # Skip empty internal_sku
        if not token:
            stats['skipped_empty'] += 1
            continue

        # Resolve internal_product_id
        internal_pid = None
        if token == 'new_sku':
            if not suggested_name:
                stats['skipped_new_sku_no_name'] += 1
                print(f'  skip: pid={platform_sku_id} new_sku without Suggested name')
                continue
            # If platform_sku already maps to a stub from a previous run with
            # the same name, reuse it instead of creating a duplicate.
            existing = ps_row['internal_product_id']
            if existing is not None:
                existing_row = conn.execute(
                    '''SELECT id, product_name FROM products
                       WHERE id = ? AND product_name = ?''',
                    (existing, suggested_name)
                ).fetchone()
                if existing_row:
                    internal_pid = existing_row['id']
                    stats['stub_reused'] = stats.get('stub_reused', 0) + 1
                    print(f'  reuse stub: id={internal_pid} '
                          f'(pid={platform_sku_id})')
                else:
                    internal_pid = None
            else:
                internal_pid = None

            if internal_pid is None:
                internal_pid = create_stub_product(conn, suggested_name)
                stub_products_created.append((internal_pid, suggested_name))
                stats['created_stubs'] += 1
                print(f'  stub: id={internal_pid} '
                      f'"{suggested_name[:60]}" (pid={platform_sku_id})')
        else:
            sku_int = parse_int(token)
            if sku_int is None:
                print(f'  skip: pid={platform_sku_id} non-numeric internal_sku={token!r}')
                stats['sku_not_found'] += 1
                continue
            # internal_sku is the OLD integer sku — translate to product_id.
            row = resolve_legacy_sku(conn, sku_int)
            if not row:
                stats['sku_not_found'] += 1
                sku_not_found_list.append((platform_sku_id, sku_int))
                print(f'  skip: pid={platform_sku_id} internal_sku={sku_int} not in products')
                continue
            internal_pid = row['product_id']

        qty_per_sale = parse_qty(r[COL_QTY_PER_SALE], default=1.0)

        # Auto-correct qty_per_sale when listing name / variation has an
        # explicit pack-size like "(10 ดอก)" that disagrees with the CSV.
        # Do NOT override when CSV qty already matches a different pack value
        # — only correct when CSV qty == 1 OR CSV qty != detected.
        detected_pack = detect_pack_size(r['Listing name'], r['Variation'])
        if detected_pack is not None and detected_pack != int(qty_per_sale):
            qty_corrections.append((
                platform_sku_id, ps_row['platform'], token,
                int(qty_per_sale), detected_pack, r['Listing name'][:60]
            ))
            qty_per_sale = float(detected_pack)
            stats['qty_auto_corrected'] += 1

        # Bundle resolution
        component_pid = None
        component_qty = None
        freebie_sku = parse_int(r[COL_FREEBIE_SKU])
        if freebie_sku is not None:
            # freebie_sku is the OLD integer sku — translate to product_id.
            crow = resolve_legacy_sku(conn, freebie_sku)
            if crow:
                component_pid = crow['product_id']
                component_qty = parse_qty(r[COL_FREEBIE_QTY], default=1.0)
                stats['bundles_applied'] += 1
            else:
                stats['bundle_component_not_found'] += 1
                bundle_skip_list.append((platform_sku_id, freebie_sku))
                print(f'  bundle skip: pid={platform_sku_id} freebie_sku={freebie_sku} not in products')

        # Compute new platform stock
        computed_stock = compute_platform_stock(
            conn, internal_pid, qty_per_sale, component_pid, component_qty
        )

        # Cap at current platform stock (CSV "Stock" column) — don't push more
        # than what the platform already has. The user manages platform stock
        # as a defensive ceiling against inflated DB values.
        csv_current_stock = parse_int(r.get('Stock'))
        if csv_current_stock is not None and computed_stock > csv_current_stock:
            new_stock = csv_current_stock
            stats['stock_capped_by_csv'] += 1
        else:
            new_stock = computed_stock

        conn.execute(
            '''UPDATE platform_skus
               SET internal_product_id = ?, qty_per_sale = ?, stock = ?
               WHERE id = ?''',
            (internal_pid, qty_per_sale, new_stock, platform_sku_id)
        )
        stats['updated'] += 1
        affected_pids.append(platform_sku_id)

    # ── Generate upload files ────────────────────────────────────────────────
    EXPORTS.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().strftime('%Y%m%d')

    if affected_pids:
        placeholders = ','.join('?' * len(affected_pids))
        out_rows = list(conn.execute(
            f'''SELECT id, platform, product_id_str, product_name, variation_id,
                       variation_name, parent_sku, seller_sku, price,
                       special_price, stock, raw_json
                FROM platform_skus
                WHERE id IN ({placeholders})''',
            affected_pids
        ).fetchall())
    else:
        out_rows = []

    shopee_rows = [dict(r) for r in out_rows if r['platform'] == 'shopee']
    lazada_rows = [dict(r) for r in out_rows if r['platform'] == 'lazada']

    if not args.dry_run:
        if shopee_rows:
            shopee_path = EXPORTS / f'shopee_stock_update_{today}.xlsx'
            buf = export_shopee(shopee_rows)
            shopee_path.write_bytes(buf.getvalue())
            print(f'\nShopee upload file: {shopee_path}  ({len(shopee_rows)} rows)')
        if lazada_rows:
            lazada_path = EXPORTS / f'lazada_stock_update_{today}.xlsx'
            buf = export_lazada(lazada_rows)
            lazada_path.write_bytes(buf.getvalue())
            print(f'Lazada upload file: {lazada_path}  ({len(lazada_rows)} rows)')

    if args.dry_run:
        conn.rollback()
        print('\nDRY RUN — rolled back, no upload files generated')
    else:
        conn.commit()
        print('\nCommitted')

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print('Summary:')
    for k, v in stats.items():
        print(f'  {k:30s} {v}')
    print(f'  {"shopee_rows_for_export":30s} {len(shopee_rows)}')
    print(f'  {"lazada_rows_for_export":30s} {len(lazada_rows)}')

    if stub_products_created:
        print('\nNew stub products:')
        for pid, name in stub_products_created:
            print(f'  id={pid} "{name}"')

    if qty_corrections:
        print(f'\nAuto-corrected qty_per_sale ({len(qty_corrections)} rows):')
        for pid, pf, sku, old_q, new_q, listing in qty_corrections:
            print(f'  pid={pid} {pf:<7} sku={sku} qty {old_q}→{new_q}  "{listing}"')

    if sku_not_found_list:
        path = EXPORTS / f'platform_overview_sku_not_found_{today}.csv'
        if not args.dry_run:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['platform_sku_id', 'csv_internal_sku'])
                w.writerows(sku_not_found_list)
            print(f'\nSKU-not-found report: {path}')
        else:
            print(f'\nSKU-not-found rows: {len(sku_not_found_list)} (dry-run, not written)')

    if platform_sku_not_found_list:
        print(f'\nplatform_sku_id not in DB: {platform_sku_not_found_list[:10]} '
              f'(total {len(platform_sku_not_found_list)})')

    if bundle_skip_list:
        print(f'\nBundle component SKU not found: {bundle_skip_list}')

    conn.close()


if __name__ == '__main__':
    main()
