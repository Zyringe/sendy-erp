"""Marketplace product-centric overview (ecommerce-revamp Phase 3).

Aggregates platform_skus (variation grain) up to the Sendy product grain and
cross-checks stock against Shopee/Lazada/TikTok "open sell" — the model layer
behind the /ecommerce list + detail pages (Phase 4). No routes/templates here.

Core-math invariants (see projects/ecommerce-revamp/plan.md — do not re-derive):

    listing_units(ps)          = max(ps.stock, 0) * ps.qty_per_sale        -- NULL stock -> 0
    platform_file_units(p, pf) = sum(listing_units) over ps rows
                                  (internal_product_id=p, platform=pf, is_ignored=0)
    snapshot_date(pf)          = date(MAX(ps.imported_at)) per platform
    sold_since(p, pf)          = sum(st.qty * COALESCE(uc.ratio, 1))
                                  FROM sales_transactions st WHERE st.product_id=p
                                  AND st.date_iso > snapshot_date(pf)   -- strict: same-day
                                  AND st.doc_no NOT LIKE 'SR%'          -- pre-export orders
                                  AND st.customer IN <platform's หน้าร้าน set>  -- already counted
    platform_est(p, pf)        = max(platform_file_units - sold_since, 0)
    true_available(p)          = stock_levels.quantity + buildable     -- models.get_buildable()
    combined_open(p)           = sum(platform_est(p, pf)) over platforms

    RED(p)   = exists pf with >=1 active listing AND platform_est(p,pf) <= 0,
               AND true_available(p) > 0
    AMBER(p) = not RED, not DEAD, and true_available(p) < combined_open(p)
    DEAD(p)  = true_available(p) <= 0 AND combined_open(p) <= 0   -- dim, no alert

Only products with at least one active (is_ignored=0) mapped listing on any
platform appear in the overview — this is a marketplace view, not a full
catalog view. `ecommerce_listings` is NOT joined: platform_skus already
contains the propagated freebie/bundle stub rows (see plan's verified facts),
so it is the complete listing set for this purpose.
"""
from database import get_connection

from .conversions import get_buildable

PLATFORMS = ('shopee', 'lazada', 'tiktok')

# หน้าร้าน customer codes that book a platform's marketplace sales in
# sales_transactions (see workspace-operating-manual.md). TikTok sales are
# not booked in the ERP yet (100% outside ERP as of 2026-07) -> empty set.
PLATFORM_CUSTOMERS = {
    'shopee': ('หน้าร้านS', 'หน้าร้านB'),
    'lazada': ('หน้าร้านL',),
    'tiktok': (),
}

_STATUS_RANK = {'red': 0, 'amber': 1, 'ok': 2, 'dead': 3}


def _snapshot_dates(conn):
    """{platform: 'YYYY-MM-DD' or None} from MAX(imported_at) per platform."""
    rows = conn.execute(
        "SELECT platform, date(MAX(imported_at)) AS snap FROM platform_skus GROUP BY platform"
    ).fetchall()
    by_platform = {r['platform']: r['snap'] for r in rows}
    return {p: by_platform.get(p) for p in PLATFORMS}


def _sold_since_by_pid(conn, platform, snapshot_date, pids=None):
    """{product_id: sold_units} for one platform's หน้าร้าน customers, sold
    strictly AFTER snapshot_date, SR returns excluded, unit-converted via
    unit_conversions (default ratio 1 when no row). Empty dict when the
    platform has no booking customers yet or no snapshot to compare against."""
    customers = PLATFORM_CUSTOMERS[platform]
    if not customers or not snapshot_date:
        return {}
    params = [snapshot_date, *customers]
    pid_filter = ""
    if pids is not None:
        if not pids:
            return {}
        pid_filter = f" AND st.product_id IN ({','.join('?' * len(pids))})"
        params = [snapshot_date, *customers, *pids]
    ph = ",".join("?" * len(customers))
    rows = conn.execute(f"""
        SELECT st.product_id AS pid, SUM(st.qty * COALESCE(uc.ratio, 1)) AS sold
          FROM sales_transactions st
          LEFT JOIN unit_conversions uc
                 ON uc.product_id = st.product_id AND uc.bsn_unit = st.unit
         WHERE st.date_iso > ?
           AND st.doc_no NOT LIKE 'SR%'
           AND st.customer IN ({ph})
           {pid_filter}
         GROUP BY st.product_id
    """, params).fetchall()
    return {r['pid']: (r['sold'] or 0) for r in rows}


def get_marketplace_freshness():
    """{platform: {'snapshot_date', 'days_old', 'sales_through'}} — the
    freshness pills on the overview page. `sales_through` = MAX(date_iso) of
    the platform's หน้าร้าน customer rows (independent of platform_skus)."""
    conn = get_connection()
    try:
        snapshots = _snapshot_dates(conn)
        result = {}
        for platform in PLATFORMS:
            snap = snapshots[platform]
            if snap:
                days_old = conn.execute(
                    "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)", (snap,)
                ).fetchone()[0]
            else:
                days_old = None
            customers = PLATFORM_CUSTOMERS[platform]
            sales_through = None
            if customers:
                ph = ",".join("?" * len(customers))
                sales_through = conn.execute(
                    f"SELECT MAX(date_iso) FROM sales_transactions WHERE customer IN ({ph})",
                    customers,
                ).fetchone()[0]
            result[platform] = {
                'snapshot_date': snap, 'days_old': days_old, 'sales_through': sales_through,
            }
        return result
    finally:
        conn.close()


def get_marketplace_overview(search=None, flt=None, page=1, per_page=50):
    """Product-centric marketplace overview. Returns (rows, total, counts).

    rows[i] = {product_id, product_name, sku_code, unit_type, stock, buildable,
               true_available, platforms: {platform: None | {est, listing_count,
               file_units, sold_since}}, combined_open, red_platforms, status
               ('red'|'amber'|'dead'|'ok'), amber_excess}

    `total` = count after search + flt. `counts` = {'all','red','amber','dead',
    <platform>...} tallied over the search-filtered set, BEFORE flt narrows it
    further (so filter chips show stable totals regardless of which chip is
    active). Sort: red -> amber -> ok -> dead, then product_name.
    """
    conn = get_connection()
    try:
        snapshots = _snapshot_dates(conn)
        file_rows = conn.execute("""
            SELECT internal_product_id AS pid, platform,
                   SUM(MAX(COALESCE(stock, 0), 0) * qty_per_sale) AS file_units,
                   COUNT(*) AS listing_count
              FROM platform_skus
             WHERE internal_product_id IS NOT NULL AND is_ignored = 0
             GROUP BY internal_product_id, platform
        """).fetchall()
        if not file_rows:
            return [], 0, {'all': 0, 'red': 0, 'amber': 0, 'dead': 0, **{p: 0 for p in PLATFORMS}}

        pids = sorted({r['pid'] for r in file_rows})
        sold_by_platform = {
            platform: _sold_since_by_pid(conn, platform, snapshots[platform], pids)
            for platform in PLATFORMS
        }

        ph_pids = ",".join("?" * len(pids))
        stock_by_pid = {
            r['product_id']: r['quantity']
            for r in conn.execute(
                f"SELECT product_id, quantity FROM stock_levels WHERE product_id IN ({ph_pids})", pids
            ).fetchall()
        }
        product_by_pid = {
            r['id']: r
            for r in conn.execute(
                f"SELECT id, product_name, sku_code, unit_type FROM products WHERE id IN ({ph_pids})", pids
            ).fetchall()
        }
    finally:
        conn.close()

    buildable = get_buildable(pids)

    file_by_pid = {}
    for r in file_rows:
        file_by_pid.setdefault(r['pid'], {})[r['platform']] = {
            'file_units': r['file_units'] or 0, 'listing_count': r['listing_count'],
        }

    rows = []
    for pid in pids:
        prod = product_by_pid.get(pid)
        if prod is None:
            continue  # mapped listing points at a since-deleted product row
        stock = stock_by_pid.get(pid) or 0
        b = buildable.get(pid)
        build_qty = b['buildable'] if b else 0
        true_available = stock + build_qty

        platforms = {}
        for platform in PLATFORMS:
            fu = file_by_pid.get(pid, {}).get(platform)
            if fu is None:
                platforms[platform] = None
                continue
            sold = sold_by_platform[platform].get(pid, 0)
            platforms[platform] = {
                'est': max(fu['file_units'] - sold, 0),
                'listing_count': fu['listing_count'],
                'file_units': fu['file_units'],
                'sold_since': sold,
            }

        combined_open = sum(v['est'] for v in platforms.values() if v)
        red_platforms = [pf for pf, v in platforms.items() if v and v['est'] <= 0]
        is_red = bool(red_platforms) and true_available > 0
        # DEAD is checked before AMBER: with a negative true_available (stock
        # drift), AMBER's "<" would also fire — DEAD (dim, no alert) wins.
        is_dead = true_available <= 0 and combined_open <= 0
        is_amber = (not is_red) and (not is_dead) and true_available < combined_open
        status = 'red' if is_red else 'amber' if is_amber else 'dead' if is_dead else 'ok'

        rows.append({
            'product_id': pid,
            'product_name': prod['product_name'],
            'sku_code': prod['sku_code'],
            'unit_type': prod['unit_type'],
            'stock': stock,
            'buildable': build_qty,
            'true_available': true_available,
            'platforms': platforms,
            'combined_open': combined_open,
            'red_platforms': red_platforms,
            'status': status,
            'amber_excess': max(combined_open - true_available, 0) if is_amber else 0,
        })

    if search:
        s = search.strip().lower()
        rows = [
            r for r in rows
            if s in r['product_name'].lower() or s in (r['sku_code'] or '').lower()
        ]

    counts = {'all': len(rows), 'red': 0, 'amber': 0, 'dead': 0, **{p: 0 for p in PLATFORMS}}
    for r in rows:
        if r['status'] in ('red', 'amber', 'dead'):
            counts[r['status']] += 1
        for p in PLATFORMS:
            if r['platforms'][p] is not None:
                counts[p] += 1

    if flt in ('red', 'amber', 'dead'):
        rows = [r for r in rows if r['status'] == flt]
    elif flt in PLATFORMS:
        rows = [r for r in rows if r['platforms'][flt] is not None]

    rows.sort(key=lambda r: (_STATUS_RANK[r['status']], r['product_name']))

    total = len(rows)
    start = (page - 1) * per_page
    return rows[start:start + per_page], total, counts


def get_unmapped_counts():
    """{'platform_skus': n, 'ecommerce_listings': n} — active rows with no
    internal product mapping yet, for the overview page's unmapped banner."""
    conn = get_connection()
    try:
        ps_n = conn.execute(
            "SELECT COUNT(*) FROM platform_skus WHERE is_ignored = 0 AND internal_product_id IS NULL"
        ).fetchone()[0]
        el_n = conn.execute(
            "SELECT COUNT(*) FROM ecommerce_listings WHERE is_ignored = 0 AND product_id IS NULL"
        ).fetchone()[0]
        return {'platform_skus': ps_n, 'ecommerce_listings': el_n}
    finally:
        conn.close()


def get_unmapped_rows():
    """Drill-down rows behind the unmapped banner (both sources, tagged)."""
    conn = get_connection()
    try:
        ps_rows = conn.execute("""
            SELECT id, platform, product_name, variation_name, seller_sku
              FROM platform_skus WHERE is_ignored = 0 AND internal_product_id IS NULL
             ORDER BY platform, product_name
        """).fetchall()
        el_rows = conn.execute("""
            SELECT id, platform, item_name, variation, seller_sku
              FROM ecommerce_listings WHERE is_ignored = 0 AND product_id IS NULL
             ORDER BY platform, item_name
        """).fetchall()
    finally:
        conn.close()

    out = [{
        'source': 'platform_skus', 'id': r['id'], 'platform': r['platform'],
        'name': r['product_name'], 'variation': r['variation_name'], 'seller_sku': r['seller_sku'],
    } for r in ps_rows]
    out += [{
        'source': 'ecommerce_listings', 'id': r['id'], 'platform': r['platform'],
        'name': r['item_name'], 'variation': r['variation'], 'seller_sku': r['seller_sku'],
    } for r in el_rows]
    return out


def get_product_marketplace_detail(product_id):
    """Per-product detail for /ecommerce/product/<id> (Phase 4). Source =
    platform_skus ONLY (contains the propagated el stubs). Returns None when
    the product itself doesn't exist; a product with zero listings still
    returns a dict with empty 'items' lists (Phase 4 decides the 404 there).

    Shape: {product: {...products row, stock, buildable, true_available},
            platforms: {pf: {freshness, sold_since, items: [
                {item: platform_products row | None, skus: [ps rows]}]}}}
    Grouped by product_id_str; '' (or NULL, from propagated stubs) forms one
    trailing item-less group per platform with item=None.
    """
    conn = get_connection()
    try:
        prod = conn.execute(
            "SELECT id, product_name, sku_code, unit_type, base_sell_price "
            "FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if prod is None:
            return None

        stock_row = conn.execute(
            "SELECT quantity FROM stock_levels WHERE product_id = ?", (product_id,)
        ).fetchone()
        stock = stock_row['quantity'] if stock_row else 0

        sku_rows = conn.execute("""
            SELECT * FROM platform_skus
             WHERE internal_product_id = ? AND is_ignored = 0
             ORDER BY platform, product_id_str, variation_name
        """, (product_id,)).fetchall()

        snapshots = _snapshot_dates(conn)
        sold_since = {
            platform: _sold_since_by_pid(conn, platform, snapshots[platform], [product_id]).get(product_id, 0)
            for platform in PLATFORMS
        }

        groups = {}
        for r in sku_rows:
            key = (r['platform'], r['product_id_str'] or '')
            groups.setdefault(key, []).append(r)

        item_cache = {}
        freshness_all = get_marketplace_freshness()
        platforms = {
            p: {'freshness': freshness_all[p], 'sold_since': sold_since[p], 'items': []}
            for p in PLATFORMS
        }
        # empty product_id_str (item-less bucket) sorts after real items, per platform
        for platform, pid_str in sorted(groups.keys(), key=lambda k: (k[0], k[1] == '', k[1])):
            item = None
            if pid_str:
                cache_key = (platform, pid_str)
                if cache_key not in item_cache:
                    item_cache[cache_key] = conn.execute(
                        "SELECT * FROM platform_products WHERE platform = ? AND product_id_str = ?",
                        (platform, pid_str),
                    ).fetchone()
                item = item_cache[cache_key]
            platforms[platform]['items'].append({'item': item, 'skus': groups[(platform, pid_str)]})
    finally:
        conn.close()

    buildable = get_buildable([product_id]).get(product_id)
    build_qty = buildable['buildable'] if buildable else 0

    return {
        'product': {
            'id': prod['id'],
            'product_name': prod['product_name'],
            'sku_code': prod['sku_code'],
            'unit_type': prod['unit_type'],
            'base_sell_price': prod['base_sell_price'],
            'stock': stock,
            'buildable': build_qty,
            'true_available': stock + build_qty,
        },
        'platforms': platforms,
    }
