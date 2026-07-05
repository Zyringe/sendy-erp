"""Pending product-suggestion review/approval helpers — extracted verbatim
from models.py (behavior-preserving split, Phase 12) — see
models/__init__.py's module docstring for the overall file-split rationale.
No behavior changes.

`approve_pending_suggestion` calls `create_structured_product` (`.products`)
and `resolve_pending_mappings` (`.mapping`) bare — both on the brief's
expected suggestions->{mapping, products} edge list. The `resolve_pending_mappings`
binding here is load-bearing: a test patches `models.suggestions.resolve_pending_mappings`
(not `models.mapping...`) to intercept this exact call — see the Phase 12
report's monkeypatch-retarget section.
"""

from database import get_connection

from .products import create_structured_product
from .mapping import resolve_pending_mappings


def count_pending_suggestions() -> int:
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM pending_product_suggestions WHERE status='pending'"
    ).fetchone()[0]
    conn.close()
    return n


def get_pending_suggestions():
    """List of suggestions awaiting manager/admin review, oldest first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT pps.*, u.display_name AS suggested_by_name, b.name AS brand_name
          FROM pending_product_suggestions pps
          LEFT JOIN users u ON u.id = pps.suggested_by_user_id
          LEFT JOIN brands b ON b.id = pps.brand_id
         WHERE pps.status = 'pending'
         ORDER BY pps.created_at ASC
    """).fetchall()
    conn.close()
    return rows


def get_pending_suggestion(suggestion_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM pending_product_suggestions WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    conn.close()
    return row


def save_pending_suggestion(data: dict, user_id: int) -> int:
    """Insert a new staged SKU suggestion. Returns new suggestion id.
    UPSERT on bsn_code so re-submitting overwrites the prior staged version.
    `data` may include free-text overrides (brand_other_name, color_code_other,
    packaging_other) and unit-conversion hints (bsn_unit, unit_conversion_ratio)."""
    # Default any missing extras to None so SQL params bind cleanly
    for k in ('brand_other_name', 'color_code_other', 'packaging_other',
              'bsn_unit', 'unit_conversion_ratio'):
        data.setdefault(k, None)
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO pending_product_suggestions
          (bsn_code, bsn_name, suggested_name, category, series, brand_id,
           model, size, color_th, color_code, packaging, condition, pack_variant,
           suggested_cost, suggested_unit_type, units_per_carton, units_per_box,
           brand_other_name, color_code_other, packaging_other,
           bsn_unit, unit_conversion_ratio,
           suggested_by_user_id, status)
        VALUES
          (:bsn_code, :bsn_name, :suggested_name, :category, :series, :brand_id,
           :model, :size, :color_th, :color_code, :packaging, :condition, :pack_variant,
           :suggested_cost, :suggested_unit_type, :units_per_carton, :units_per_box,
           :brand_other_name, :color_code_other, :packaging_other,
           :bsn_unit, :unit_conversion_ratio,
           :suggested_by_user_id, 'pending')
        ON CONFLICT(bsn_code) DO UPDATE SET
            bsn_name = excluded.bsn_name,
            suggested_name = excluded.suggested_name,
            category = excluded.category,
            series = excluded.series,
            brand_id = excluded.brand_id,
            model = excluded.model,
            size = excluded.size,
            color_th = excluded.color_th,
            color_code = excluded.color_code,
            packaging = excluded.packaging,
            condition = excluded.condition,
            pack_variant = excluded.pack_variant,
            suggested_cost = excluded.suggested_cost,
            suggested_unit_type = excluded.suggested_unit_type,
            units_per_carton = excluded.units_per_carton,
            units_per_box = excluded.units_per_box,
            brand_other_name = excluded.brand_other_name,
            color_code_other = excluded.color_code_other,
            packaging_other = excluded.packaging_other,
            bsn_unit = excluded.bsn_unit,
            unit_conversion_ratio = excluded.unit_conversion_ratio,
            suggested_by_user_id = excluded.suggested_by_user_id,
            status = 'pending'
    """, {**data, 'suggested_by_user_id': user_id})
    conn.commit()
    sid = cur.lastrowid or conn.execute(
        "SELECT id FROM pending_product_suggestions WHERE bsn_code = ?",
        (data['bsn_code'],)
    ).fetchone()[0]
    conn.close()
    return sid


def approve_pending_suggestion(suggestion_id: int, edits: dict, reviewer_id: int) -> int:
    """Apply manager/admin edits → create product → map BSN code → mark approved.
    Returns the new product id. Single transaction (on `conn`) — the product
    row itself (spec cols + derived/override name + sku_code) is created by
    `create_structured_product` (P3 of the product-creation-consolidation
    plan; stamps `created_via='smart_mapping'`), called WITH this function's
    `conn` so it participates in the same transaction rather than committing
    on its own. That plus the surrounding BSN-mapping upsert, unit_conversion
    insert, and suggestion status update all commit or roll back together —
    a failure anywhere leaves no orphan product/mapping row. `edits` dict
    overrides any field on the staged suggestion."""
    conn = get_connection()
    try:
        sug = conn.execute(
            "SELECT * FROM pending_product_suggestions WHERE id = ? AND status='pending'",
            (suggestion_id,)
        ).fetchone()
        if not sug:
            raise ValueError(f'suggestion {suggestion_id} not found or already approved')

        # Merge: edits overrides suggestion
        d = dict(sug)
        d.update({k: v for k, v in edits.items() if v is not None})

        # packaging: free-text override is stored if dropdown empty
        # (may fail CHECK trigger on products INSERT — admin must extend trigger first)
        packaging_th = d.get('packaging') or None
        if not packaging_th and d.get('packaging_other'):
            packaging_th = d['packaging_other'].strip() or None

        # Row-insert + name + sku_code all go through the canonical create
        # path. It re-resolves brand_other_name/color_code_other into new FK
        # rows and free-text `category` into `category_id` itself (same
        # logic this function used to inline). Passing OUR conn keeps it
        # inside this function's own transaction — no separate commit, so
        # the mapping/status writes below can still roll everything back
        # together on failure (no orphan product).
        new_pid = create_structured_product({
            'product_name': d.get('suggested_name') or d.get('bsn_name'),
            'brand_id': d.get('brand_id'),
            'brand_other_name': d.get('brand_other_name'),
            'color_code': d.get('color_code'),
            'color_code_other': d.get('color_code_other'),
            'color_th': d.get('color_th'),
            'category_id': d.get('category_id'),
            'category': d.get('category'),
            'series': d.get('series'),
            'model': d.get('model'),
            'size': d.get('size'),
            'condition': d.get('condition'),
            'pack_variant': d.get('pack_variant'),
            'packaging_th': packaging_th,
            'unit_type': d.get('suggested_unit_type') or 'ตัว',
            'cost_price': d.get('suggested_cost') or 0.0,
            'units_per_carton': d.get('units_per_carton') or 1,
            'units_per_box': d.get('units_per_box') or 1,
        }, 'smart_mapping', conn=conn)

        # Upsert mapping (bsn_code → new product) — the non-split catch-all row
        # (bsn_unit='', mig 124 restore). UPDATE-then-INSERT mirrors
        # upsert_mapping() (boundary-safe; reuses the existing pending row, so
        # no separate placeholder cleanup is needed). Filtering/inserting on
        # bsn_unit='' means this never clobbers a unit-specific split row that
        # may already exist for this code (PR #178 regression class: mig 112
        # once made this INSERT omit bsn_unit entirely and 500 on a NOT NULL
        # column with no default — restored here explicitly, not relying on
        # the column DEFAULT, to keep intent obvious).
        updated = conn.execute(
            "UPDATE product_code_mapping SET bsn_name=?, product_id=?, is_ignored=0 "
            "WHERE bsn_code=? AND bsn_unit=''",
            (sug['bsn_name'], new_pid, sug['bsn_code'])
        ).rowcount
        if not updated:
            conn.execute(
                "INSERT OR IGNORE INTO product_code_mapping "
                "(bsn_code, bsn_name, product_id, is_ignored, bsn_unit) "
                "VALUES (?, ?, ?, 0, '')",
                (sug['bsn_code'], sug['bsn_name'], new_pid)
            )

        # Mark suggestion approved
        conn.execute("""
            UPDATE pending_product_suggestions
               SET status = 'approved',
                   reviewed_by_user_id = ?,
                   approved_product_id = ?,
                   reviewed_at = datetime('now','localtime')
             WHERE id = ?
        """, (reviewer_id, new_pid, suggestion_id))

        # Auto-create unit_conversion if BSN ships in different unit than product
        bsn_unit = d.get('bsn_unit')
        ratio = d.get('unit_conversion_ratio')
        product_unit = d.get('suggested_unit_type') or 'ตัว'
        if bsn_unit and ratio and float(ratio) > 0 and bsn_unit != product_unit:
            conn.execute("""
                INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
                VALUES (?, ?, ?)
                ON CONFLICT(product_id, bsn_unit) DO UPDATE SET
                    ratio = excluded.ratio
            """, (new_pid, bsn_unit, float(ratio)))

        # Backfill product_id on existing unlinked transaction rows
        resolve_pending_mappings(conn)

        conn.commit()
        return new_pid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
