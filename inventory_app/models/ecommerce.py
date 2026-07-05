"""Ecommerce listing helpers — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

`suggest_listing_mapping` uses `_clean_for_match` from `._shared` (per the
brief).
"""

from database import get_connection

from ._shared import _clean_for_match


def import_ecommerce_listings(records):
    """
    Merge-insert unique listings (INSERT OR IGNORE).
    Returns (added, skipped).
    """
    conn = get_connection()
    added = skipped = 0
    for r in records:
        cur = conn.execute("""
            INSERT OR IGNORE INTO ecommerce_listings
            (platform, item_name, variation, seller_sku, listing_key, sample_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (r['platform'], r['item_name'], r['variation'],
              r['seller_sku'], r['listing_key'], r['sample_price']))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    conn.commit()
    conn.close()
    return added, skipped


def get_ecommerce_listing_summary():
    """Return {platform: {total, mapped, unmatched}} for summary cards."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT platform,
               COUNT(*) AS total,
               COUNT(product_id) AS mapped
        FROM ecommerce_listings
        WHERE is_ignored = 0
        GROUP BY platform
    """).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result[r['platform']] = {
            'total':     r['total'],
            'mapped':    r['mapped'],
            'unmatched': r['total'] - r['mapped'],
        }
    return result


def get_ecommerce_listings(platform=None, search=None, mapped=None, page=1, per_page=50):
    """Return paginated ecommerce_listings joined with product info."""
    conditions = ["el.is_ignored = 0"]
    params = []
    if platform:
        conditions.append("el.platform = ?")
        params.append(platform)
    if search:
        conditions.append("(el.item_name LIKE ? OR el.variation LIKE ? OR el.seller_sku LIKE ?)")
        params += [f'%{search}%'] * 3
    if mapped is True:
        conditions.append("el.product_id IS NOT NULL")
    elif mapped is False:
        conditions.append("el.product_id IS NULL")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    conn = get_connection()
    total = conn.execute(
        f"SELECT COUNT(*) FROM ecommerce_listings el {where}", params
    ).fetchone()[0]
    rows = conn.execute(f"""
        SELECT el.*, p.product_name
        FROM ecommerce_listings el
        LEFT JOIN products p ON p.id = el.product_id
        {where}
        ORDER BY el.platform, el.product_id IS NULL DESC, el.item_name
        LIMIT ? OFFSET ?
    """, params + [per_page, (page - 1) * per_page]).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_listing_mapping_data(unmatched_only=False):
    """Return all listings for mapping export."""
    conn = get_connection()
    cond = "AND el.product_id IS NULL" if unmatched_only else ""
    rows = conn.execute(f"""
        SELECT el.*, p.product_name
        FROM ecommerce_listings el
        LEFT JOIN products p ON p.id = el.product_id
        WHERE el.is_ignored = 0 {cond}
        ORDER BY el.platform, el.product_id IS NULL DESC, el.item_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def apply_listing_mapping(records):
    """
    Apply product_id → product_id for ecommerce_listings.
    records: list of {listing_id, product_id}
    Returns (updated, not_found).
    """
    conn = get_connection()
    updated = not_found = 0
    for r in records:
        lid = r.get('listing_id')
        int_pid = r.get('product_id')
        if not lid or not int_pid:
            continue
        product = conn.execute(
            "SELECT id FROM products WHERE id = ? AND is_active = 1", (int_pid,)
        ).fetchone()
        if not product:
            not_found += 1
            continue
        qty = r.get('qty_per_sale') or 1.0
        conn.execute(
            "UPDATE ecommerce_listings SET product_id = ?, qty_per_sale = ? WHERE id = ?",
            (product['id'], qty, lid)
        )
        updated += 1
    conn.commit()
    conn.close()
    return updated, not_found


def suggest_listing_mapping():
    """
    Fuzzy-match ecommerce_listings to ERP products.
    Returns dict: {listing_id -> {suggested_pid, suggested_name, confidence}}
    """
    import numpy as np
    from rapidfuzz import fuzz
    from rapidfuzz.process import cdist

    conn = get_connection()
    product_list = list(conn.execute(
        "SELECT id, product_name FROM products WHERE is_active = 1"
    ).fetchall())
    listing_list = list(conn.execute(
        "SELECT id, item_name, variation, seller_sku, product_id FROM ecommerce_listings WHERE is_ignored = 0"
    ).fetchall())
    conn.close()

    if not product_list or not listing_list:
        return {}

    corpus  = [_clean_for_match(p['product_name']) for p in product_list]
    queries = [
        _clean_for_match(f"{l['item_name']} {l['variation'] or ''} {l['seller_sku'] or ''}")
        for l in listing_list
    ]

    matrix     = cdist(queries, corpus, scorer=fuzz.token_set_ratio, workers=-1)
    best_idx   = matrix.argmax(axis=1)
    best_score = matrix.max(axis=1)

    results = {}
    for i, listing in enumerate(listing_list):
        lid = listing['id']
        if listing['product_id']:
            matched = next((p for p in product_list if p['id'] == listing['product_id']), None)
            if matched:
                results[lid] = {'suggested_pid': matched['id'], 'suggested_name': matched['product_name'], 'confidence': 100}
            continue
        score = int(best_score[i])
        if score < 25:
            continue
        product = product_list[best_idx[i]]
        results[lid] = {'suggested_pid': product['id'], 'suggested_name': product['product_name'], 'confidence': score}
    return results
