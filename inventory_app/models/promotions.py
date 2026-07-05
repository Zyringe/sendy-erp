"""Promotions + price tiers — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.
"""
from database import get_connection
from datetime import date


def get_promotions(product_id: int, active_only=False):
    conn = get_connection()
    try:
        cond = "WHERE product_id = ?"
        params = [product_id]
        if active_only:
            cond += " AND is_active = 1"
        return conn.execute(
            f"SELECT * FROM promotions {cond} ORDER BY created_at DESC", params
        ).fetchall()
    finally:
        conn.close()


def get_active_promotion(product_id: int):
    today = date.today().isoformat()
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT * FROM promotions
            WHERE product_id = ? AND is_active = 1
              AND (date_start IS NULL OR date_start <= ?)
              AND (date_end IS NULL OR date_end >= ?)
            ORDER BY created_at DESC
            LIMIT 1
        """, (product_id, today, today)).fetchone()
    finally:
        conn.close()


def effective_price(product) -> float:
    """Return the effective per-unit selling price for this product.

    Behavior per promo_type:
      - None / no active promo → base_sell_price
      - 'percent' → base_sell_price × (1 − discount_value/100), rounded 2dp
      - 'fixed'   → discount_value (treated as the FINAL selling price,
                    not a discount amount — see templates/promotions/form.html)
      - 'bundle' / 'gift' / 'mixed' → base_sell_price UNCHANGED
                    These types don't alter per-unit price. They define deal
                    terms (extra free units, free-gift items, bulk conditions)
                    that the cart/quote layer applies separately when summing
                    the order total. Per-unit display price is the catalog
                    price. Cart-level bundle resolution is out of scope here.
    """
    promo = get_active_promotion(product['id'])
    if promo is None:
        return product['base_sell_price']
    if promo['promo_type'] == 'percent':
        return round(product['base_sell_price'] * (1 - promo['discount_value'] / 100), 2)
    if promo['promo_type'] == 'fixed':
        return promo['discount_value']
    # bundle / mixed / gift — per-unit price unchanged
    return product['base_sell_price']


def create_promotion(data: dict) -> int:
    """Insert a promotions row. Accepts any subset of the extended fields
    introduced in mig 086 (bundle_*, gift_*). Missing keys default to None
    so the DB's CHECK constraint enforces shape per promo_type.

    Required keys: product_id, promo_name, promo_type.
    Optional: discount_value, date_start, date_end, bundle_buy, bundle_free,
              bundle_unit, bundle_condition, bundle_tiers_json,
              gift_desc, gift_qty.
    """
    full = {
        "product_id":        data["product_id"],
        "promo_name":        data["promo_name"],
        "promo_type":        data["promo_type"],
        "discount_value":    data.get("discount_value"),
        "date_start":        data.get("date_start"),
        "date_end":          data.get("date_end"),
        "bundle_buy":        data.get("bundle_buy"),
        "bundle_free":       data.get("bundle_free"),
        "bundle_unit":       data.get("bundle_unit"),
        "bundle_condition":  data.get("bundle_condition"),
        "bundle_tiers_json": data.get("bundle_tiers_json"),
        "gift_desc":         data.get("gift_desc"),
        "gift_qty":          data.get("gift_qty"),
    }
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO promotions (
                product_id, promo_name, promo_type, discount_value,
                date_start, date_end,
                bundle_buy, bundle_free, bundle_unit, bundle_condition,
                bundle_tiers_json, gift_desc, gift_qty
            ) VALUES (
                :product_id, :promo_name, :promo_type, :discount_value,
                :date_start, :date_end,
                :bundle_buy, :bundle_free, :bundle_unit, :bundle_condition,
                :bundle_tiers_json, :gift_desc, :gift_qty
            )
        """, full)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def deactivate_promotion(promo_id: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE promotions SET is_active = 0 WHERE id = ?", (promo_id,))
        conn.commit()
    finally:
        conn.close()


def get_product_price_tiers(product_id: int):
    """Return all tier rows for this product, ordered by sort_order then price."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, qty_label, price, note, sort_order "
            "FROM product_price_tiers "
            "WHERE product_id = ? "
            "ORDER BY sort_order, price",
            (product_id,)
        ).fetchall()
    finally:
        conn.close()
