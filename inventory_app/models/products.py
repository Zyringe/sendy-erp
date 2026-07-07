"""Product CRUD — extracted verbatim from models.py (behavior-preserving
split, Phase 11) — see models/__init__.py's module docstring for the
overall file-split rationale. No behavior changes.
"""
import config
from database import get_connection
import name_builder
from sku_code_utils import PACKAGING_SHORT, regenerate_for_product

from ._shared import _set_price_change_source


def get_products(search=None, low_stock=False, hard_to_sell=False,
                 location=None, in_stock=False, restock=False, page=1, per_page=50,
                 include_inactive=False):
    conn = get_connection()
    conditions = [] if include_inactive else ["p.is_active = 1"]
    params = []
    if search:
        conditions.append("(p.product_name LIKE ? OR CAST(p.id AS TEXT) LIKE ? OR p.sku_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if hard_to_sell:
        conditions.append("p.hard_to_sell = 1")
    if location:
        conditions.append(
            "EXISTS (SELECT 1 FROM product_locations pl"
            " WHERE pl.product_id = p.id AND pl.floor_no LIKE ?)"
        )
        params.append(f"%{location}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    having_clauses = []
    if low_stock:
        having_clauses.append("COALESCE(s.quantity, 0) <= p.low_stock_threshold")
    if in_stock:
        having_clauses.append("COALESCE(s.quantity, 0) > 0")
    if restock:
        # out of stock but sold in the last 30 days → reorder candidates
        having_clauses.append("COALESCE(s.quantity, 0) <= 0")
        having_clauses.append(
            "EXISTS (SELECT 1 FROM sales_transactions st WHERE st.product_id = p.id"
            " AND st.net <> 0 AND st.date_iso >= date('now', '-30 days'))")
    having = ("HAVING " + " AND ".join(having_clauses)) if having_clauses else ""

    sql = f"""
        SELECT p.id, p.sku_code, p.product_name, p.units_per_carton, p.units_per_box,
               p.unit_type, p.hard_to_sell, p.cost_price, p.base_sell_price,
               p.low_stock_threshold, p.is_active, p.brand_id, p.category_id,
               p.created_at, p.updated_at,
               COALESCE(s.quantity, 0) AS quantity,
               CASE WHEN COALESCE(s.quantity, 0) <= p.low_stock_threshold THEN 1 ELSE 0 END AS is_low,
               COALESCE((SELECT SUM(stock) FROM platform_skus
                          WHERE platform='shopee' AND internal_product_id=p.id), 0) AS shopee_stock,
               COALESCE((SELECT SUM(stock) FROM platform_skus
                          WHERE platform='lazada' AND internal_product_id=p.id), 0) AS lazada_stock
        FROM products p
        LEFT JOIN stock_levels s ON s.product_id = p.id
        WHERE {where}
        GROUP BY p.id
        {having}
        ORDER BY p.id
        LIMIT ? OFFSET ?
    """
    params += [per_page, (page - 1) * per_page]
    rows = conn.execute(sql, params).fetchall()

    count_sql = f"""
        SELECT COUNT(*) FROM products p
        LEFT JOIN stock_levels s ON s.product_id = p.id
        WHERE {where}
        {having.replace('HAVING','AND') if having else ''}
    """
    total = conn.execute(count_sql, params[:-2]).fetchone()[0]
    conn.close()
    return rows, total


def get_product(product_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT p.id, p.sku_code, p.sku_code_locked,
               p.product_name, p.units_per_carton, p.units_per_box,
               p.unit_type, p.hard_to_sell, p.cost_price, p.base_sell_price,
               p.low_stock_threshold, p.is_active, p.brand_id, p.category_id,
               p.sub_category, p.series, p.model, p.size,
               p.color_code, p.packaging_th, p.packaging_short, p.condition, p.pack_variant,
               p.created_via,
               p.created_at, p.updated_at,
               COALESCE(s.quantity, 0) AS quantity,
               CASE WHEN COALESCE(s.quantity, 0) <= p.low_stock_threshold THEN 1 ELSE 0 END AS is_low,
               COALESCE((SELECT SUM(stock) FROM platform_skus
                          WHERE platform='shopee' AND internal_product_id=p.id), 0) AS shopee_stock,
               COALESCE((SELECT SUM(stock) FROM platform_skus
                          WHERE platform='lazada' AND internal_product_id=p.id), 0) AS lazada_stock
        FROM products p
        LEFT JOIN stock_levels s ON s.product_id = p.id
        WHERE p.id = ?
    """, (product_id,)).fetchone()
    conn.close()
    return row


def create_product(data: dict) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO products (product_name, units_per_carton, units_per_box,
            unit_type, hard_to_sell, cost_price, opening_cost, base_sell_price, low_stock_threshold,
            shopee_stock, lazada_stock)
        VALUES (:product_name, :units_per_carton, :units_per_box,
            :unit_type, :hard_to_sell, :cost_price, :cost_price, :base_sell_price, :low_stock_threshold,
            :shopee_stock, :lazada_stock)
    """, data)
    # ensure stock_levels row exists
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (cur.lastrowid,))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def create_structured_product(fields: dict, created_via: str, conn=None) -> int:
    """Canonical structured product-creation path (P3 of the
    product-creation-consolidation plan). Resolves inline "other"
    brand/color into new FK rows, resolves a free-text `category` into
    `category_id` when no id was given, derives `packaging_short` from
    `packaging_th`, inserts the product row (spec columns + core numeric
    fields + `created_via`), ensures a matching `stock_levels` row, then
    sets `product_name` (an explicit override is kept verbatim; otherwise
    it's derived from the spec columns via `name_builder.rebuild_product_name`)
    and always (re)generates `sku_code` (collision-safe, falls back to
    `INT-<id>` when no structured fields are present).

    Both real create entry points route through this: the `/products/new`
    hand form (`created_via='manual'`) and Smart Suggest approval
    (`created_via='smart_mapping'`).

    Transaction ownership: when `conn` is omitted, this function opens its
    own connection and is a self-contained transaction (commits on success,
    rolls back + closes on error) — used by the standalone `/products/new`
    path. When a caller passes its own `conn` (e.g. `approve_pending_suggestion`,
    which does more writes — the BSN-mapping upsert, unit_conversion,
    suggestion status — around this call), this function does NOT commit,
    rollback, or close; it just raises on error so the CALLER's transaction
    owns the all-or-nothing outcome (no orphan product on a later step's
    failure).

    `fields` keys (all optional unless noted):
      product_name          -- explicit override; falsy => name is derived
      brand_id, brand_other_name
      color_code, color_code_other, color_th
      category_id, category (free-text, resolved only when category_id absent)
      sub_category, series, model, size, condition, pack_variant
      packaging_th          -- must be NULL or one of the CHECK-trigger values
      unit_type (default 'ตัว'), hard_to_sell (default 0)
      cost_price (default 0.0) -- also seeds opening_cost
      base_sell_price (default 0.0)
      low_stock_threshold (default config.LOW_STOCK_DEFAULT_THRESHOLD)
      shopee_stock, lazada_stock (default 0)
      units_per_carton, units_per_box (default 1)

    Returns the new product id. Raises on any error, e.g. an invalid
    `packaging_th` value (the products_packaging_th_check_insert trigger) —
    with `conn=None` this leaves no orphan product/stock_levels row; with a
    caller-supplied `conn`, the caller's own rollback is responsible for that.
    """
    d = dict(fields)
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        # brand: if brand_other_name set and no brand_id -> INSERT new brand row
        if not d.get('brand_id') and d.get('brand_other_name'):
            new_brand_name = d['brand_other_name'].strip()
            if new_brand_name:
                code = new_brand_name.upper().replace(' ', '_')[:30]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO brands (code, name, name_th, is_own_brand, sort_order)"
                    " VALUES (?, ?, ?, 0, 100)",
                    (code, new_brand_name, new_brand_name)
                )
                bid = cur.lastrowid or conn.execute(
                    "SELECT id FROM brands WHERE code = ?", (code,)
                ).fetchone()[0]
                d['brand_id'] = bid

        # color: if color_code_other set and no color_code -> INSERT new color row
        if not d.get('color_code') and d.get('color_code_other'):
            new_code = d['color_code_other'].strip().upper()[:10]
            color_th = d.get('color_th') or new_code
            if new_code:
                conn.execute(
                    "INSERT OR IGNORE INTO color_finish_codes (code, name_th, sort_order)"
                    " VALUES (?, ?, 100)",
                    (new_code, color_th)
                )
                d['color_code'] = new_code

        # category: resolve free-text against the categories master when no
        # id was given. Unmatched text -> NULL (never create a new category
        # here — that's a deliberate migration, matches approve's behavior).
        category_id = d.get('category_id')
        if not category_id and d.get('category'):
            cat_txt = str(d['category']).strip()
            crow = conn.execute(
                "SELECT id FROM categories WHERE name_th = ? OR code = ? LIMIT 1",
                (cat_txt, cat_txt),
            ).fetchone()
            category_id = crow[0] if crow else None

        pkg_th = d.get('packaging_th') or None
        pkg_short = PACKAGING_SHORT.get(pkg_th) if pkg_th else None
        cost_price = d.get('cost_price') or 0.0

        cur = conn.execute("""
            INSERT INTO products
              (product_name, units_per_carton, units_per_box,
               unit_type, hard_to_sell, cost_price, opening_cost, base_sell_price,
               low_stock_threshold, shopee_stock, lazada_stock,
               brand_id, category_id, sub_category, color_code,
               packaging_th, packaging_short,
               series, model, size, condition, pack_variant, created_via)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            d.get('product_name') or '',
            d.get('units_per_carton') or 1,
            d.get('units_per_box') or 1,
            d.get('unit_type') or 'ตัว',
            1 if d.get('hard_to_sell') else 0,
            cost_price,
            cost_price,
            d.get('base_sell_price') or 0.0,
            d.get('low_stock_threshold') if d.get('low_stock_threshold') is not None
                else config.LOW_STOCK_DEFAULT_THRESHOLD,
            d.get('shopee_stock') or 0,
            d.get('lazada_stock') or 0,
            d.get('brand_id'),
            category_id,
            d.get('sub_category') or None,
            d.get('color_code') or None,
            pkg_th,
            pkg_short,
            d.get('series') or None,
            d.get('model') or None,
            d.get('size') or None,
            d.get('condition') or None,
            d.get('pack_variant') or None,
            created_via,
        ))
        new_pid = cur.lastrowid

        # ensure stock_levels row
        conn.execute(
            "INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
            (new_pid,)
        )

        # name: explicit override kept verbatim; otherwise derive from spec cols
        if not d.get('product_name'):
            derived = name_builder.rebuild_product_name(conn, new_pid)
            conn.execute(
                "UPDATE products SET product_name = ? WHERE id = ?",
                (derived, new_pid)
            )

        # sku_code: always (re)generate — collision-safe, falls back to INT-<id>
        regenerate_for_product(conn, new_pid)

        if own_conn:
            conn.commit()
        return new_pid
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


# The only columns update_product is allowed to write. Built dynamically from
# whichever of these keys are actually present in `data` — callers (currently
# just product_edit) may omit fields their form doesn't collect, and those
# columns are left untouched rather than clobbered to 0/NULL. See PR fixing
# the product-edit form silently zeroing hard_to_sell/shopee_stock/lazada_stock
# (those three aren't rendered by the edit form, so they were always absent
# from `data` and a fixed full-column UPDATE wiped them on every save).
_UPDATABLE_PRODUCT_COLUMNS = (
    'product_name', 'units_per_carton', 'units_per_box', 'unit_type',
    'hard_to_sell', 'cost_price', 'base_sell_price', 'low_stock_threshold',
    'shopee_stock', 'lazada_stock',
)


def update_product(product_id: int, data: dict, source=None):
    fields = {k: data[k] for k in _UPDATABLE_PRODUCT_COLUMNS if k in data}
    if not fields:
        return
    conn = get_connection()
    # set source BEFORE the UPDATE so the price-history trigger can stamp it;
    # reset to NULL AFTER so a later write on this connection defaults to NULL.
    _set_price_change_source(conn, source)
    set_clause = ", ".join(f"{col}=:{col}" for col in fields)
    conn.execute(f"UPDATE products SET {set_clause} WHERE id=:id",
                 {**fields, 'id': product_id})
    _set_price_change_source(conn, None)
    conn.commit()
    conn.close()


def deactivate_product(product_id: int):
    conn = get_connection()
    conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
