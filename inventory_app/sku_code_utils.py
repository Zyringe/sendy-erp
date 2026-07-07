"""sku_code generation logic — shared between bulk script and Flask routes.

Format: <CAT>-<BRAND>-<MODEL>-<SIZE>[-<SERIES>]-<COLOR>-<PKG>[-<pack_variant>]
        Fallback: INT-<id> when nothing structured is available
"""
from __future__ import annotations

import hashlib
import re


# Packaging Thai → 2-3 char English code mapping. After mig 087 the products
# table stores packaging_short as its own column (populated at write time), so
# build_sku_code reads it directly without dict lookup. This dict is kept for
# callers that derive packaging_short from a Thai value at insert time
# (normalize_products_round1.py, parse_sku_names.py output).
# Must align with the products_packaging_short_check_* CHECK trigger in DB.
PACKAGING_SHORT = {
    "ตัว":         "UN",   # Unit
    "แผง":         "PN",   # Panel
    "ถุง":         "BG",   # Bag
    "ซอง":         "SC",   # Sachet
    "แพ็ค":        "PK",   # Pack
    "โหล":         "DZ",   # Dozen
    "แพ็คหัว":     "HP",   # Hanging-Pack
    "แพ็คถุง":     "PP",   # Pouch-Pack
    "แบบหลอด":     "TB",   # Tube
    "อัดแผง":      "SP",   # Strip-Pack
    "1กลมี60ใบ":   "C60",  # Carton-60
}


# Condition Thai → 3-letter code (sku_code_naming_rule.md table).
# `EXP:MM/YYYY` is handled separately by _condition_segment (formats to EXP{MMYY}).
CONDITION_SHORT = {
    "ไม่สวย":       "BLM",   # cosmetic blemish
    "ตำหนิ":        "DEF",   # defective
    "กล่องไม่สวย":  "BXD",   # box damaged
    "เก่า":         "OLD",   # old stock
    "รีแพ็ค":       "RPK",   # repacked
    "ไม่มีน็อต":     "NPT",   # missing parts
    "แผงอ่อน":      "WBP",   # weak blister panel
    "ไม่สกรีน":      "NSP",   # no screen print
    "แบบเก่า":      "OMD",   # old model
    "หมดอายุ":      "EXP",   # expired (undated)
}

_EXP_DATE = re.compile(r"^EXP[:\s]*(\d{2})/(\d{4})$")


def _condition_segment(c: str) -> str:
    """Map Thai condition text to its 3-letter code. EXP:MM/YYYY → EXP{MM}{YY}.
    Returns "" when no mapping exists (silently drops free-form conditions
    rather than embedding Thai in sku_code, which would violate the
    'English/ASCII only' rule).
    """
    if not c:
        return ""
    c = c.strip()
    if c in CONDITION_SHORT:
        return CONDITION_SHORT[c]
    m = _EXP_DATE.match(c)
    if m:
        mm, yyyy = m.group(1), m.group(2)
        return f"EXP{mm}{yyyy[-2:]}"
    return ""


def _norm_segment(s: str) -> str:
    """Strip whitespace; collapse internal spaces. Keeps leading '#' (model marker)."""
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"\s+", "", s)
    return s


def _series_segment(s: str) -> str:
    """Convert series value to a sku_code-safe segment.
    ASCII: cleaned + uppercase (DOME, BRUSHNO.98, CSK).
    Thai/mixed: 'S' + 4-hex hash (stable across runs, ASCII-safe).
    Symbol-only (screwdriver-head '+'/'-'): omitted — a bare symbol segment
    renders as '-+-' / '---' (padding, violates rule 2) and subcat/name
    already carry the head type (Put 2026-07-07, pids 1797-1800).
    """
    if not s:
        return ""
    s = s.strip()
    if not re.search(r"[0-9A-Za-zก-๙]", s):
        return ""
    if s.isascii():
        return re.sub(r"\s+", "", s).upper()
    return "S" + hashlib.md5(s.encode("utf-8")).hexdigest()[:4].upper()


def build_sku_code(p: dict) -> str:
    """Build sku_code from a dict-like row per the locked 10-slot rule
    (`sku_code_naming_rule.md`): cat-subcat?-brand-series?-model?-size?-color?-pkg?-condition?-pack_variant?.

    Required keys: id
    Optional keys (segments included when truthy):
      cat_short_code, sub_category_short_code, brand_short_code,
      series, model, size, color_code, packaging_short
      (2-3 char code, e.g. UN/PN/BG), condition,
      pack_variant (suppressed when value is 1 — default variant has no suffix)

    For back-compat the function also accepts `packaging` (Thai value, looked
    up via PACKAGING_SHORT) when `packaging_short` is absent — this lets
    callers that parse a Thai name pass the Thai value directly.
    """
    parts = []
    if p.get("cat_short_code"):
        parts.append(p["cat_short_code"])
    if p.get("sub_category_short_code"):
        parts.append(p["sub_category_short_code"])
    if p.get("brand_short_code"):
        parts.append(p["brand_short_code"])
    if p.get("series"):
        seg = _series_segment(p["series"])
        if seg:
            parts.append(seg)
    if p.get("model"):
        parts.append(_norm_segment(p["model"]))
    if p.get("size"):
        parts.append(_norm_segment(p["size"]))
    if p.get("color_code"):
        parts.append(p["color_code"])
    pkg_short = p.get("packaging_short")
    if not pkg_short and p.get("packaging_th"):
        pkg_short = PACKAGING_SHORT.get(p["packaging_th"])
    if not pkg_short and p.get("packaging"):
        # Legacy callers that still pass Thai value under "packaging" key
        pkg_short = PACKAGING_SHORT.get(p["packaging"])
    if pkg_short:
        parts.append(pkg_short)
    cond_seg = _condition_segment(p.get("condition") or "")
    if cond_seg:
        parts.append(cond_seg)
    pv = p.get("pack_variant")
    if pv and str(pv) != "1":
        parts.append(str(pv))

    if not parts:
        return f"INT-{p['id']}"
    # '/' is a valid fraction char in size/series/model (1/2", 5/16") but is a
    # path separator — keep sku_code path-safe by mapping '/'→'-'. See
    # test_sku_code_slash.py (Put 2026-06-29).
    return "-".join(part.replace("/", "-") for part in parts)


def regenerate_for_product(conn, product_id: int) -> tuple:
    """Recompute sku_code for one product. Returns (old, new).
    Caller is responsible for COMMIT and for honoring sku_code_locked
    (this helper does NOT check the lock — invoke at higher level).
    """
    row = conn.execute("""
        SELECT p.id, p.sku_code, p.model, p.size, p.series,
               p.color_code, p.packaging_th, p.packaging_short, p.condition,
               p.pack_variant, p.sub_category_short_code,
               b.short_code AS brand_short_code,
               c.short_code AS cat_short_code
          FROM products p
          LEFT JOIN brands b     ON b.id = p.brand_id
          LEFT JOIN categories c ON c.id = p.category_id
         WHERE p.id = ?
    """, (product_id,)).fetchone()
    if not row:
        return None, None

    old_code = row["sku_code"] if "sku_code" in row.keys() else row[2]
    new_code = build_sku_code(dict(row))

    # Collision check — append -<id> if collision (unless same product)
    collision = conn.execute(
        "SELECT id FROM products WHERE sku_code = ? AND id != ?",
        (new_code, product_id),
    ).fetchone()
    if collision:
        new_code = f"{new_code}-{row['id']}"

    if new_code != old_code:
        conn.execute(
            "UPDATE products SET sku_code = ? WHERE id = ?",
            (new_code, product_id)
        )
    return old_code, new_code
