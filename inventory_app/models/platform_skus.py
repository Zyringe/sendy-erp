"""Platform-SKU (marketplace listing↔product) helpers — extracted verbatim
from models.py (behavior-preserving split, Phase 12) — see
models/__init__.py's module docstring for the overall file-split rationale.
No behavior changes.

`suggest_platform_mapping` uses `_clean_for_match` from `._shared` (per the
brief).
"""

from database import get_connection

from ._shared import _clean_for_match


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

    Keyed on (platform, product_id_str). All columns overwrite on conflict
    (no internal mapping to preserve at the product grain — spec §3.2).

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
              status          = excluded.status,
              cover_image_url = COALESCE(excluded.cover_image_url, cover_image_url),
              image_urls      = excluded.image_urls,
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
            r.get('cover_image_url'), r.get('image_urls', '[]'),
            r.get('dts_info'),        r.get('raw_json'),
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


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


def get_marketplace_price_history(product_id, limit=50):
    """Marketplace (Shopee/Lazada) price-change timeline for one product.

    Rows from platform_price_history — captured by the mig-137 trigger on
    platform_skus (import upsert / in-app edit) plus the mig-138 campaign seed.
    Newest first. NOTE: an import-diff log — changed_at is the import time, not
    necessarily when the price changed on the platform.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT h.platform, h.variation_id, h.field_name,
                  h.old_value, h.new_value, h.changed_at, h.source,
                  ps.variation_name, ps.seller_sku, ps.qty_per_sale
             FROM platform_price_history h
             LEFT JOIN platform_skus ps
                    ON ps.platform = h.platform
                   AND ps.variation_id = h.variation_id
            WHERE h.internal_product_id = ?
            ORDER BY h.changed_at DESC, h.id DESC
            LIMIT ?""",
        (product_id, limit),
    ).fetchall()
    conn.close()
    return rows


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
    conn.execute("""
        UPDATE platform_skus
        SET price=?, special_price=?, stock=?, qty_per_sale=?,
            imported_at=datetime('now','localtime')
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
