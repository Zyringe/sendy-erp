"""Platform-SKU (marketplace listing↔product) helpers — extracted verbatim
from models.py (behavior-preserving split, Phase 12) — see
models/__init__.py's module docstring for the overall file-split rationale.
No behavior changes.

`suggest_platform_mapping` uses `_clean_for_match` from `._shared` (per the
brief).
"""

from datetime import datetime

from database import get_connection

from ._shared import PLATFORMS, _clean_for_match

# Product-wide cap on marketplace price-history rows fetched + embedded per
# product-detail load (bounds page weight / modal DOM as history accumulates).
_MKT_HISTORY_CAP = 500


def import_platform_skus(platform, records):
    """Upsert platform SKU records keyed on (platform, variation_id).

    SAFE UPSERT CONTRACT (spec §3.1):
    - Never DELETEs any row.
    - Never touches internal_product_id or qty_per_sale in the UPDATE SET.
    - Enrichment columns (weight/dims/gtin/special_price dates/variation_image_url)
      use COALESCE(excluded.col, col) so a partial import never nulls existing data.
    - price/stock/name/variation_name/raw_json overwrite normally.

    Returns (count_upserted, propagated_count).
    """
    conn = get_connection()
    # NO DELETE — that is the whole point of this rewrite.
    count = 0
    for r in records:
        conn.execute("""
            INSERT INTO platform_skus
              (platform, variation_id, product_id_str, product_name, variation_name,
               parent_sku, seller_sku, price, special_price, stock, raw_json,
               weight_kg, length_cm, width_cm, height_cm, gtin,
               special_price_start, special_price_end, variation_image_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform, variation_id) DO UPDATE SET
              product_id_str      = excluded.product_id_str,
              product_name        = excluded.product_name,
              variation_name      = excluded.variation_name,
              parent_sku          = excluded.parent_sku,
              seller_sku          = excluded.seller_sku,
              price               = excluded.price,
              special_price       = excluded.special_price,
              stock               = excluded.stock,
              raw_json            = excluded.raw_json,
              weight_kg           = COALESCE(excluded.weight_kg, weight_kg),
              length_cm           = COALESCE(excluded.length_cm, length_cm),
              width_cm            = COALESCE(excluded.width_cm, width_cm),
              height_cm           = COALESCE(excluded.height_cm, height_cm),
              gtin                = COALESCE(excluded.gtin, gtin),
              special_price_start = COALESCE(excluded.special_price_start, special_price_start),
              special_price_end   = COALESCE(excluded.special_price_end, special_price_end),
              variation_image_url = COALESCE(excluded.variation_image_url, variation_image_url),
              imported_at         = datetime('now','localtime')
              -- internal_product_id and qty_per_sale are DELIBERATELY ABSENT from UPDATE SET
        """, (
            platform,
            r.get('variation_id'),   r.get('product_id_str'),
            r.get('product_name', ''), r.get('variation_name'),
            r.get('parent_sku'),     r.get('seller_sku'),
            r.get('price'),          r.get('special_price'),
            r.get('stock'),          r.get('raw_json'),
            r.get('weight_kg'),      r.get('length_cm'),
            r.get('width_cm'),       r.get('height_cm'),
            r.get('gtin'),
            r.get('special_price_start'), r.get('special_price_end'),
            r.get('variation_image_url'),
        ))
        count += 1
    propagated = _propagate_listings_to_platform_skus(conn, platform)
    conn.commit()
    conn.close()
    return count, propagated


def import_platform_products(platform, records):
    """Upsert product-grain records into platform_products.

    Keyed on (platform, product_id_str). Most columns overwrite on conflict
    (no internal mapping to preserve at the product grain — spec §3.2), but
    `status` and `image_urls` COALESCE onto the existing value when the
    record omits the key (r.get returns None) — a single-file basic-info-only
    import (ecommerce-revamp Phase 2, e.g. Shopee mass_update_basic_info,
    which never carries image data) must not wipe fields a fuller multi-file
    import previously populated. A record that DOES explicitly parse an empty
    gallery (e.g. Lazada basic-info's own image columns, genuinely empty for
    that product) still overwrites — only an ABSENT key preserves.

    Returns count of records processed.
    """
    conn = get_connection()
    count = 0
    for r in records:
        conn.execute("""
            INSERT INTO platform_products
              (platform, product_id_str, parent_sku, product_name, name_en,
               description, category_id_str, category_name, brand,
               place_of_origin, material, warranty_policy, warranty_period,
               status, cover_image_url, image_urls, dts_info, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform, product_id_str) DO UPDATE SET
              parent_sku      = excluded.parent_sku,
              product_name    = excluded.product_name,
              name_en         = excluded.name_en,
              description     = COALESCE(excluded.description, description),
              category_id_str = COALESCE(excluded.category_id_str, category_id_str),
              category_name   = COALESCE(excluded.category_name, category_name),
              brand           = COALESCE(excluded.brand, brand),
              place_of_origin = COALESCE(excluded.place_of_origin, place_of_origin),
              material        = COALESCE(excluded.material, material),
              warranty_policy = COALESCE(excluded.warranty_policy, warranty_policy),
              warranty_period = COALESCE(excluded.warranty_period, warranty_period),
              status          = COALESCE(excluded.status, status),
              cover_image_url = COALESCE(excluded.cover_image_url, cover_image_url),
              image_urls      = COALESCE(excluded.image_urls, image_urls),
              dts_info        = COALESCE(excluded.dts_info, dts_info),
              raw_json        = excluded.raw_json,
              imported_at     = datetime('now','localtime')
        """, (
            platform,
            r.get('product_id_str'),  r.get('parent_sku'),
            r.get('product_name', ''), r.get('name_en'),
            r.get('description'),     r.get('category_id_str'),
            r.get('category_name'),   r.get('brand'),
            r.get('place_of_origin'), r.get('material'),
            r.get('warranty_policy'), r.get('warranty_period'),
            r.get('status'),
            r.get('cover_image_url'), r.get('image_urls'),
            r.get('dts_info'),        r.get('raw_json'),
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


# ── TikTok snapshot ──────────────────────────────────────────────────────────
#
# TikTok's `all_information` export feeds BOTH grains from one file, so it gets
# its own writer rather than two sequential calls that each open and commit
# their own connection — a failure between them would leave platform_products
# advertising listings whose prices and stock never landed.
#
# `import_platform_skus` is deliberately NOT reused. Its contract overwrites
# `stock` and always bumps `imported_at`; TikTok needs both conditional, and
# the Shopee/Lazada path must not be bent for a TikTok-only case (Put,
# 2026-08-21). The SQL below differs from it in exactly the two CASE
# expressions at the bottom.

_TIKTOK_PRODUCT_UPSERT = """
    INSERT INTO platform_products
      (platform, product_id_str, product_name, description, category_id_str,
       category_name, brand, status, cover_image_url, image_urls, raw_json)
    VALUES ('tiktok',?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(platform, product_id_str) DO UPDATE SET
      product_name    = excluded.product_name,
      description     = COALESCE(excluded.description, description),
      category_id_str = COALESCE(excluded.category_id_str, category_id_str),
      category_name   = COALESCE(excluded.category_name, category_name),
      brand           = COALESCE(excluded.brand, brand),
      status          = COALESCE(excluded.status, status),
      cover_image_url = COALESCE(excluded.cover_image_url, cover_image_url),
      image_urls      = COALESCE(excluded.image_urls, image_urls),
      raw_json        = excluded.raw_json,
      imported_at     = datetime('now','localtime')
"""

_TIKTOK_SKU_UPSERT = """
    INSERT INTO platform_skus
      (platform, variation_id, product_id_str, product_name, variation_name,
       seller_sku, price, special_price, stock, raw_json,
       weight_kg, length_cm, width_cm, height_cm)
    VALUES ('tiktok',?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(platform, variation_id) DO UPDATE SET
      product_id_str = excluded.product_id_str,
      product_name   = excluded.product_name,
      variation_name = excluded.variation_name,
      seller_sku     = excluded.seller_sku,
      price          = excluded.price,
      special_price  = excluded.special_price,
      raw_json       = excluded.raw_json,
      weight_kg      = COALESCE(excluded.weight_kg, weight_kg),
      length_cm      = COALESCE(excluded.length_cm, length_cm),
      width_cm       = COALESCE(excluded.width_cm, width_cm),
      height_cm      = COALESCE(excluded.height_cm, height_cm),
      -- An export with no `quantity` column must not blank the stock already
      -- on record, and must not move the snapshot date either:
      -- ecommerce_overview._snapshot_dates() reads MAX(imported_at) as "when
      -- this platform's stock was last true", so bumping it would make a stale
      -- number look like today's and collapse the sold_since window to zero.
      stock       = CASE WHEN ? THEN excluded.stock ELSE stock END,
      imported_at = CASE WHEN ? THEN datetime('now','localtime') ELSE imported_at END
      -- internal_product_id and qty_per_sale are DELIBERATELY ABSENT, exactly
      -- as in import_platform_skus: they are the operator's work, not the file's.
"""


def import_tiktok_snapshot(parsed):
    """Write one `parse_tiktok` result — both grains — in ONE transaction.

    Returns ``(n_products, n_skus, absent)`` where `absent` lists the active
    TikTok rows already on record that this file did NOT contain. They are
    reported, never auto-flagged: an `all_information` export cannot be told
    apart from a category-filtered one, and auto-ignoring a partial export
    would silently hide listings that are still selling (Put, 2026-08-21).
    """
    products = list(parsed.get('products') or [])
    skus = list(parsed.get('skus') or [])
    stock_present = 1 if parsed.get('stock_present') else 0

    conn = get_connection()
    conn.isolation_level = None          # manual transaction control
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('BEGIN IMMEDIATE')

        seen = {s['variation_id'] for s in skus}
        held = conn.execute(
            "SELECT variation_id, product_id_str, product_name, variation_name, "
            "       stock, imported_at, is_ignored "
            "  FROM platform_skus WHERE platform = 'tiktok'").fetchall()
        known = {r['variation_id'] for r in held}
        absent = [dict(r) for r in held
                  if r['variation_id'] not in seen and not r['is_ignored']]

        # A file with no `quantity` column may UPDATE what we already hold —
        # the CASE expressions below keep that row's stock and snapshot date.
        # It may NOT introduce a variation: on INSERT those CASEs do not apply,
        # so the row would land with stock NULL and `imported_at` defaulted to
        # now. ecommerce_overview then reads MAX(stock,0)=0 as "sold out on the
        # platform" while MAX(imported_at) says the snapshot is today's, and
        # every one of those rows turns RED the moment it is mapped. Measured
        # on the real 36-column export, 2026-08-22: 47 rows, all NULL, snapshot
        # stamped today, first mapped row RED against true_available 97.
        if not stock_present:
            fresh = [s for s in skus if s['variation_id'] not in known]
            if fresh:
                names = ' · '.join(
                    f"{s.get('variation_name') or s['variation_id']}" for s in fresh[:3])
                more = f' และอีก {len(fresh) - 3}' if len(fresh) > 3 else ''
                raise ValueError(
                    f'ไฟล์นี้ไม่มีคอลัมน์สต็อก (ปริมาณ) แต่มี {len(fresh)} ตัวเลือกที่ยังไม่เคย'
                    f'นำเข้ามาก่อน ({names}{more}) — ตัวเลือกใหม่ต้องมาพร้อมสต็อก '
                    'ไม่งั้นระบบจะเข้าใจว่าของหมดบนแพลตฟอร์ม '
                    'ให้ export ใหม่โดยติ๊กคอลัมน์ "ปริมาณ" แล้วนำเข้าอีกครั้ง')

        for p in products:
            conn.execute(_TIKTOK_PRODUCT_UPSERT, (
                p.get('product_id_str'), p.get('product_name', ''),
                p.get('description'), p.get('category_id_str'),
                p.get('category_name'), p.get('brand'), p.get('status'),
                p.get('cover_image_url'), p.get('image_urls'), p.get('raw_json'),
            ))
        for s in skus:
            conn.execute(_TIKTOK_SKU_UPSERT, (
                s.get('variation_id'), s.get('product_id_str'),
                s.get('product_name'), s.get('variation_name'),
                s.get('seller_sku'), s.get('price'), s.get('special_price'),
                s.get('stock'), s.get('raw_json'), s.get('weight_kg'),
                s.get('length_cm'), s.get('width_cm'), s.get('height_cm'),
                stock_present, stock_present,
            ))

        # No row-count assertion here on purpose: every record either INSERTs
        # or UPDATEs (ON CONFLICT, no WHERE), so a short write is unreachable
        # and a check for it can never go red — a green checkmark, not a test.
        # The invariant that IS reachable is atomicity across the two grains,
        # and that one is pinned by test_import_is_atomic_across_both_grains.
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return len(products), len(skus), absent


def _propagate_listings_to_platform_skus(conn, platform):
    """
    After a fresh platform_skus snapshot, restore internal_product_id +
    qty_per_sale on platform_skus by matching ecommerce_listings on
    (platform, item_name, variation, seller_sku). Treat 'nan'/NULL/'' as
    equivalent and fall back to stripping the Lazada 'attr:' prefix.
    Returns count of platform_skus rows updated.
    """
    rows = conn.execute(
        '''SELECT id, item_name, variation, seller_sku, product_id, qty_per_sale
           FROM ecommerce_listings
           WHERE platform = ? AND product_id IS NOT NULL''',
        (platform,)
    ).fetchall()

    def _norm(v):
        s = (v or '').strip()
        return '' if s.lower() == 'nan' else s

    def _strip_lazada(v):
        if v and ':' in v:
            head, _, tail = v.partition(':')
            if head and tail and ':' not in head:
                return tail.strip()
        return v

    update_sql = '''
        UPDATE platform_skus
           SET internal_product_id = ?, qty_per_sale = ?
         WHERE platform = ?
           AND internal_product_id IS NULL
           AND product_name = ?
           AND CASE WHEN LOWER(COALESCE(variation_name,'')) IN ('','nan')
                    THEN '' ELSE variation_name END = ?
           AND CASE WHEN LOWER(COALESCE(seller_sku,'')) IN ('','nan')
                    THEN '' ELSE seller_sku END = ?
    '''
    total = 0
    for r in rows:
        var = _norm(r['variation'])
        ssk = _norm(r['seller_sku'])
        cur = conn.execute(update_sql, (
            r['product_id'], r['qty_per_sale'], platform,
            r['item_name'], var, ssk
        ))
        if cur.rowcount == 0:
            var2 = _strip_lazada(var)
            if var2 != var:
                cur = conn.execute(update_sql, (
                    r['product_id'], r['qty_per_sale'], platform,
                    r['item_name'], var2, ssk
                ))
        total += cur.rowcount
    return total


def get_platform_skus(platform, search=None, page=1, per_page=50):
    conn = get_connection()
    params = [platform]
    where = "WHERE platform = ? AND is_ignored = 0"
    if search:
        where += " AND (product_name LIKE ? OR variation_name LIKE ? OR seller_sku LIKE ?)"
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    total = conn.execute(
        f"SELECT COUNT(*) FROM platform_skus {where}", params
    ).fetchone()[0]
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"""SELECT * FROM platform_skus {where}
            ORDER BY product_name, variation_name
            LIMIT ? OFFSET ?""",
        params + [per_page, offset]
    ).fetchall()
    conn.close()
    return rows, total


def get_platform_skus_all(platform):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM platform_skus WHERE platform = ? AND is_ignored = 0 "
        "ORDER BY product_name, variation_name",
        (platform,)
    ).fetchall()
    conn.close()
    return rows


def get_marketplace_listings_with_history(product_id, now=None, conn=None):
    """Current marketplace listings (Shopee/Lazada) for a product, each with its
    price-change history — powers the 'ราคา marketplace' card + click→history modal.

    Returns {'shopee': {...}, 'lazada': {...}} where each is
    {'listings': [listing, ...], 'last_import': <str|None>}. Each listing dict:
      variation_id, label, qps, price, special_price,
      effective (display price), list_price (struck list price, or None),
      has_history (bool), last_changed (YYYY-MM-DD str|None),
      history: [{field, field_label, old, new, date}, ...] newest-first (capped).

    `special_price` counts as the effective price only when it is genuinely lower
    AND its promotion window (`special_price_start`/`_end`, NULL bound = open) is
    active at `now` — an expired/scheduled promo shows the list price instead.
    `now` is a 'YYYY-MM-DD HH:MM:SS' string (defaults to local now); injectable for tests.

    ⚠ Prices are the LAST-IMPORTED values (platform_skus snapshot), NOT live — the
    card surfaces `last_import` as an "as of import" cue. `last_changed` is the last
    recorded CHANGE date (an import-diff log), which is a different thing from freshness.
    """
    if now is None:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    FIELD_LABEL = {'price': 'ราคาตั้ง', 'special_price': 'ราคาพิเศษ'}
    owned = conn is None
    if owned:
        conn = get_connection()
    skus = conn.execute(
        """SELECT platform, variation_id, variation_name, seller_sku, product_name,
                  price, special_price, special_price_start, special_price_end,
                  qty_per_sale, imported_at
             FROM platform_skus
            WHERE internal_product_id = ? AND is_ignored = 0
            ORDER BY platform, price""",
        (product_id,),
    ).fetchall()
    hist = conn.execute(
        """SELECT platform, variation_id, field_name, old_value, new_value, changed_at
             FROM platform_price_history
            WHERE internal_product_id = ?
            ORDER BY changed_at DESC, id DESC
            LIMIT ?""",
        (product_id, _MKT_HISTORY_CAP),
    ).fetchall()
    if owned:
        conn.close()

    by_key = {}
    for h in hist:
        by_key.setdefault((h['platform'], h['variation_id']), []).append(h)

    out = {p: {'listings': [], 'last_import': None} for p in PLATFORMS}
    for s in skus:
        plat = s['platform']
        if plat not in out:
            continue
        imp = s['imported_at']
        if imp and (out[plat]['last_import'] is None or imp > out[plat]['last_import']):
            out[plat]['last_import'] = imp
        # Label fallback: option name → seller SKU → Shopee/Lazada listing title
        # → variation code. The title fallback is meaningful when a product is
        # posted as several separate listings that have no per-option name.
        label = ((s['variation_name'] or '').strip()
                 or (s['seller_sku'] or '').strip()
                 or (s['product_name'] or '').strip())
        if not label:
            vid = s['variation_id'] or ''
            label = ('รหัส ' + vid[:12]) if vid else '(ไม่ระบุตัวเลือก)'
        price, sp = s['price'], s['special_price']
        st, en = s['special_price_start'], s['special_price_end']
        # special counts only if genuinely lower AND its promo window is active now
        special_active = (
            sp is not None and price is not None and sp < price
            and (not st or st <= now)
            and (not en or en >= now)
        )
        if special_active:
            effective, list_price = sp, price
        else:
            effective, list_price = price, None
        rows = by_key.get((plat, s['variation_id']), [])
        history = [{
            'field': r['field_name'],
            'field_label': FIELD_LABEL.get(r['field_name'], r['field_name']),
            'old': r['old_value'], 'new': r['new_value'],
            'date': (r['changed_at'] or '')[:10],   # date only (handles date & datetime rows)
        } for r in rows]
        out[plat]['listings'].append({
            'variation_id': s['variation_id'],
            'label': label,
            'qps': s['qty_per_sale'],
            'price': price, 'special_price': sp,
            'effective': effective, 'list_price': list_price,
            'has_history': bool(history),
            'last_changed': history[0]['date'] if history else None,
            'history': history,
        })
    return out


def get_platform_summary():
    conn = get_connection()
    rows = conn.execute("""
        SELECT platform,
               COUNT(*) AS sku_count,
               SUM(stock) AS total_stock,
               MAX(imported_at) AS last_import
        FROM platform_skus
        WHERE is_ignored = 0
        GROUP BY platform
    """).fetchall()
    conn.close()
    return {r['platform']: dict(r) for r in rows}


def update_platform_sku(sku_id, price, special_price, stock, qty_per_sale):
    conn = get_connection()
    # imported_at deliberately NOT touched: it is the platform-file stamp that
    # ecommerce_overview._snapshot_dates() reads as MAX(imported_at) — a manual
    # edit bumping it would shift the whole platform's snapshot to today
    # (freshness pill lies, sold_since window collapses to zero).
    conn.execute("""
        UPDATE platform_skus
        SET price=?, special_price=?, stock=?, qty_per_sale=?
        WHERE id=?
    """, (price, special_price, stock, qty_per_sale, sku_id))
    conn.commit()
    conn.close()


def get_platform_mapping_data():
    """
    Return all platform_skus joined with internal product info (if mapped).
    Used for mapping export/import.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            ps.id, ps.platform, ps.product_id_str, ps.product_name,
            ps.variation_id, ps.variation_name, ps.seller_sku,
            ps.price, ps.special_price, ps.stock, ps.qty_per_sale,
            ps.internal_product_id,
            p.id AS internal_pid, p.product_name AS internal_product_name,
            p.unit_type
        FROM platform_skus ps
        LEFT JOIN products p ON p.id = ps.internal_product_id
        WHERE ps.is_ignored = 0
        ORDER BY ps.platform, ps.product_name, ps.variation_name
    """).fetchall()
    conn.close()
    return rows


def apply_platform_mapping(rows):
    """
    rows: list of dicts with keys: platform_sku_id, product_id, qty_per_sale
    Returns (updated, not_found) counts.
    """
    conn = get_connection()
    updated, not_found = 0, 0
    for r in rows:
        sku_id      = r.get('platform_sku_id')
        int_pid     = r.get('product_id')
        qty_per_sale = r.get('qty_per_sale')

        if not sku_id:
            continue

        if int_pid:
            product = conn.execute(
                "SELECT id FROM products WHERE id = ? AND is_active = 1",
                (int_pid,)
            ).fetchone()
            if not product:
                not_found += 1
                continue
            product_id = product['id']
        else:
            product_id = None

        conn.execute("""
            UPDATE platform_skus
            SET internal_product_id = ?,
                qty_per_sale = COALESCE(?, qty_per_sale)
            WHERE id = ?
        """, (product_id, qty_per_sale, sku_id))
        updated += 1

    conn.commit()
    conn.close()
    return updated, not_found


def suggest_platform_mapping():
    """
    For every platform_sku, suggest the best-matching internal product.
    Returns dict: { platform_sku_id -> {suggested_pid, suggested_name, confidence} }
    """
    import re
    import numpy as np
    from rapidfuzz import fuzz
    from rapidfuzz.process import cdist

    conn = get_connection()
    product_list = list(conn.execute(
        "SELECT id, product_name FROM products WHERE is_active = 1"
    ).fetchall())
    psku_list = list(conn.execute(
        "SELECT id, product_name, variation_name, seller_sku, internal_product_id "
        "FROM platform_skus WHERE is_ignored = 0"
    ).fetchall())
    conn.close()

    corpus  = [_clean_for_match(p['product_name']) for p in product_list]
    queries = [
        _clean_for_match(
            f"{s['product_name']} {s['variation_name'] or ''} {s['seller_sku'] or ''}"
        )
        for s in psku_list
    ]

    # Batch fuzzy match (workers=-1 = all CPU cores)
    matrix = cdist(queries, corpus, scorer=fuzz.token_set_ratio, workers=-1)
    best_idx   = matrix.argmax(axis=1)
    best_score = matrix.max(axis=1)

    results = {}
    for i, sku in enumerate(psku_list):
        sku_id = sku['id']

        # Already mapped → confidence 100, keep existing
        if sku['internal_product_id']:
            matched = next(
                (p for p in product_list if p['id'] == sku['internal_product_id']), None
            )
            if matched:
                results[sku_id] = {
                    'suggested_pid':  matched['id'],
                    'suggested_name': matched['product_name'],
                    'confidence':     100,
                }
                continue

        score = int(best_score[i])
        if score < 25:
            continue
        product = product_list[best_idx[i]]
        results[sku_id] = {
            'suggested_pid':  product['id'],
            'suggested_name': product['product_name'],
            'confidence':     score,
        }

    return results
