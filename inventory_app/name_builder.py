"""Canonical product-name builder for the app.

Thin bridge to the shared ``build()`` in ``scripts/build_name_from_columns.py``
(the same name-format logic the offline CSV rebuild uses — single source of
truth), plus a DB-backed single-product rebuild for the Master Naming workbench
inline editor.

The path insert mirrors what ``app.py`` already does for ``bsn_suggest`` and
keeps this module self-contained so it imports without booting the Flask app
(needed for unit tests).
"""
import os
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import build_name_from_columns as _bnc  # noqa: E402


def build_name(row):
    """Compose the canonical display name from a dict of name parts.

    Keys: category, series, brand, model, size, color_th, color_code,
    packaging, condition, pack_variant (all optional, "" when absent).
    """
    return _bnc.build(row)


def preview_name(conn, fields):
    """Build the canonical name from PROPOSED (not-yet-saved) field values,
    resolving brands.name from brand_id and color_finish_codes.name_th from
    color_code. fields keys: brand_id, sub_category, series, model, size,
    color_code, packaging_th, condition, pack_variant (all optional).
    """
    brand_name = ""
    if fields.get("brand_id"):
        b = conn.execute("SELECT name FROM brands WHERE id=?",
                         (fields["brand_id"],)).fetchone()
        if b:
            brand_name = b["name"] or ""
    color_th = ""
    if fields.get("color_code"):
        c = conn.execute("SELECT name_th FROM color_finish_codes WHERE code=?",
                         (fields["color_code"],)).fetchone()
        if c:
            color_th = c["name_th"] or ""
    row = {
        "category": fields.get("sub_category") or "",
        "series": fields.get("series") or "",
        "brand": brand_name,
        "model": fields.get("model") or "",
        "size": fields.get("size") or "",
        "color_th": color_th,
        "color_code": fields.get("color_code") or "",
        "packaging": fields.get("packaging_th") or "",
        "condition": fields.get("condition") or "",
        "pack_variant": str(fields.get("pack_variant") or ""),
    }
    return build_name(row)


def rebuild_product_name(conn, product_id):
    """Return the canonical name for ``product_id`` from its structured columns
    (joining brands.name + color_finish_codes.name_th), or None if it doesn't
    exist. Maps the products schema onto build()'s expected part names:
    category←sub_category, brand←brands.name, packaging←packaging_th.
    """
    r = conn.execute(
        """
        SELECT p.sub_category, p.series, p.model, p.size, p.color_code,
               p.packaging_th, p.condition, p.pack_variant,
               b.name AS brand_name, cf.name_th AS color_th
          FROM products p
          LEFT JOIN brands b              ON b.id = p.brand_id
          LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
         WHERE p.id = ?
        """,
        (product_id,),
    ).fetchone()
    if not r:
        return None
    row = {
        "category": r["sub_category"] or "",
        "series": r["series"] or "",
        "brand": r["brand_name"] or "",
        "model": r["model"] or "",
        "size": r["size"] or "",
        "color_th": r["color_th"] or "",
        "color_code": r["color_code"] or "",
        "packaging": r["packaging_th"] or "",
        "condition": r["condition"] or "",
        "pack_variant": str(r["pack_variant"]) if r["pack_variant"] else "",
    }
    return build_name(row)
