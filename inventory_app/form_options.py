"""Page-level pick-list option builders — one source of truth.

`/mapping`, `/products/new` and `/naming` each build the same handful of
option lists (brands, colors, categories, packaging, unit types, conditions)
for their combo/select fields. This module is the single place that queries
them; each call site keeps its own dict keys so no template variable is
renamed (only the *values* are unified — see plan `mapping-suggest-clone`
PR1, decision Q10).
"""
from sku_code_utils import CONDITION_SHORT, PACKAGING_SHORT, _EXP_DATE


def brands(conn):
    return conn.execute(
        "SELECT id, name, name_th FROM brands ORDER BY is_own_brand DESC, sort_order, name"
    ).fetchall()


def colors(conn):
    return conn.execute(
        "SELECT code, name_th FROM color_finish_codes ORDER BY sort_order, code"
    ).fetchall()


def categories(conn):
    return conn.execute(
        "SELECT id, code, name_th FROM categories ORDER BY sort_order, name_th"
    ).fetchall()


def packaging():
    """Closed list, deliberately NOT widened from the DB.

    packaging_th has the identical closed-list shape to condition, but unlike
    condition an unmapped packaging_th is NOT sku-safe: the save path derives
    packaging_short from PACKAGING_SHORT, so offering an unknown value would
    let a save blank packaging_short and change the sku_code. Widening this
    one needs that decision first; it is not the same fix as `conditions()`
    below. (Carried across verbatim from the exemption note that used to live
    on `naming.py::_editor_options`.)
    """
    return list(PACKAGING_SHORT.keys())


def units(conn):
    """Unit types already in use on `products`, most-common first.

    Free-text suggestion source (any value allowed) — not a closed list."""
    return [r[0] for r in conn.execute(
        "SELECT unit_type FROM products WHERE unit_type IS NOT NULL AND unit_type <> '' "
        "GROUP BY unit_type ORDER BY COUNT(*) DESC"
    ).fetchall()]


def conditions(conn, *, drop_dated=False):
    """Canonical conditions PLUS every value already stored on a product.

    The dropdown used to be `CONDITION_SHORT` alone. That list is closed (10 values)
    and doubles as the sku-segment map, so any condition outside it could not be
    represented in the editor — and the editor does not fail safe:
    master_naming.html's `set()` leaves the <select> blank for an unknown value, the
    save payload always carries `condition: <value> || null`, and `_clean_updates`
    treats a present key as an instruction to write. So opening such a product and
    saving NULLED the column and rebuilt product_name without the word, in one
    transaction.

    44 rows on prod were exposed (swept 2026-08-14), in two severities:
      แบบหุล 19 · แบบมิล 8 · มียอด 7 — unmapped, so the sku survives; the word is lost.
      EXP:* 10 rows            — `_condition_segment` DOES emit a segment for these, so
                                 the regenerated sku loses it too:
                                 `DSC-CUT-AS-S5048-14in-GRN-EXP0727` → `…-14in-GRN`.
                                 That SKU is a live variant in a TikTok listing file, and
                                 sku_code is the join key three other consumers look up by.

    ⛔ Do NOT close the gap by adding those words to CONDITION_SHORT instead. That map
    also generates the sku segment, so it would rename every affected product —
    and `sku_code` is a cross-system join key (ERP ↔ photo folders ↔ batch.json ↔
    listing Seller SKU). `_condition_segment()` returns "" for an unmapped word, which
    is exactly why widening the dropdown alone leaves every sku_code untouched.

    Canonical values keep their defined order (it is a meaningful ordering, not
    alphabetical); DB-only extras are appended, sorted, de-duplicated.

    `drop_dated` (keyword-only, default False = preserving):
      - False (default): `/naming`'s behaviour. An option missing from an EDIT form's
        list gets blanked on save (the 44-row incident above, 10 of which were
        `EXP:*` whose sku segment `_condition_segment` actually emits) — so `/naming`
        must keep every dated value.
      - True: for CREATE forms (`/mapping`, and PR4's `/products/new`). A new product
        should not be offered a 2019 expiry. Nothing is lost: the combo is
        `data-allow-new="1"`, so a genuine new expiry can still be typed and is kept
        verbatim. "Dated" is matched with the existing `sku_code_utils._EXP_DATE`
        regex (do not write a second one) — the undated canonical `หมดอายุ` is not
        dated and always survives.
    """
    stored = [r[0] for r in conn.execute(
        "SELECT DISTINCT condition FROM products "
        "WHERE TRIM(COALESCE(condition, '')) <> '' ORDER BY condition")]
    known = list(CONDITION_SHORT)
    extras = [c for c in stored if c not in CONDITION_SHORT]
    if drop_dated:
        extras = [c for c in extras if not _EXP_DATE.match(c)]
    return known + extras
