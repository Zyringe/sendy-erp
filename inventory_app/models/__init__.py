"""Sendy ERP — business logic + DB queries (raw SQL, no ORM).

The largest file in the app (~4,400 LOC). Holds:

- Product / brand / category readers + pricing summary helpers
  (`get_product_pricing_summary` — the customer-facing price view)
- Transaction ledger helpers (`record_*`, stock-level math)
- BSN sync + mapping (`product_code_mapping`, `unit_conversions`)
- Cost/WACC math: `get_wacc`, `_recompute_wacc_for_product`,
  `get_cost_history`
- HR queries (employees, leave entitlements, payroll runs)
- Commission helpers (shared with `commission.py` engine)
- Receivable/AR aging (used by `cashflow.py` + `/payment-status`)
- Search / filter / sort builders for the listing pages

Conventions:
  - All queries open their own connection via `database.get_connection()`.
  - Functions returning sequences yield sqlite3.Row objects (template-friendly).
  - Money math: read `sendy_erp/CLAUDE.md` for the VAT-aware `billed`
    formula — never write `SUM(net)` directly for ar/cashflow.
  - Do NOT expose `cost_price` to customer-facing routes — use
    `base_sell_price` or `get_product_pricing_summary()`.

Future split: this file is a candidate for domain-based extraction
(models_products.py / models_transactions.py / models_commission.py /
models_hr.py) when next major touch arrives — opportunistic, not big-bang.
"""
import json
import re
import sqlite3

import config
from database import get_connection
import bsn_units
import name_builder
from sku_code_utils import PACKAGING_SHORT, regenerate_for_product
from cashflow import BSN_AR_PREDICATE
from collections import defaultdict
from datetime import date

from ._shared import (
    _set_price_change_source,
    AUDIT_LOG_RETENTION_DAYS, _AUDIT_PRUNE_PREDICATE, prune_audit_log,
    _NOISE_WORDS, _QTY_PREFIX, _clean_for_match, _re_mod,
)
from .products import (
    get_products, get_product, create_product, create_structured_product,
    update_product, deactivate_product,
)
from .brands import (
    get_brands, get_brand, set_product_brand, _topup_pre_feb_for_product,
    create_brand,
)
from .stock import (
    get_stock_alerts, count_stock_alerts, get_product_locations,
    save_product_locations, count_restock_needed, count_active_products,
    count_in_stock,
)
from .transactions import (
    add_transaction, get_current_stock, get_transactions,
    get_recent_transactions, delete_transactions_by_ids,
)
from .promotions import (
    get_promotions, get_active_promotion, effective_price, create_promotion,
    deactivate_promotion, get_product_price_tiers,
)
from .customers import (
    get_customer_summary, get_regions, get_customers,
    get_all_regions_with_counts, update_region, get_active_salespersons,
    get_all_regions, get_orphan_salesperson_codes, get_customer_master,
    _BULK_MAX, update_customer_assignment, bulk_reassign_customers,
    get_customers_master, import_customers_from_bsn, get_customers_for_map,
    save_customer_geocode, get_customer_zones, get_customer_types,
    get_geocode_progress,
)
from .suppliers import get_suppliers, get_supplier_summary
from .wacc import (
    _WACC_INITIAL_DATE, recalculate_product_wacc, get_current_wacc,
    get_cost_history, recalculate_waccs_for_products,
)
from .bsn_sync import (
    to_base_units, _get_base_qty, _sync_bsn_to_stock,
    get_pending_unit_conversions, learn_acronyms_normalize,
    save_unit_conversions, dismiss_pending_unit_conversion,
    update_unit_conversion_ratio, get_all_unit_conversions,
    upsert_unit_conversion,
)
from .mapping import (
    upsert_mapping, get_pending_mappings, resolve_pending_mappings,
    _resolve_mapping, _BSN_LEDGER_NOTE_PATTERNS, _bsn_code_ledger_orphans,
    repoint_bsn_code,
)
from .imports import (
    _detect_removed_lines, preview_import, import_weekly, get_recent_imports,
)
from .sales import (
    get_sales, get_purchases_by_doc, get_sales_summary, get_sales_by_doc,
    get_trade_dashboard, get_product_trade_summary, get_purchases,
)
from .commission import (
    _normalise_override_payload, _validate_override_targets,
    list_commission_overrides, get_commission_override,
    create_commission_override, update_commission_override,
    toggle_commission_override, delete_commission_override,
)
from .payments import (
    parse_payment_csv, import_payments, get_payment_status,
    get_payment_summary, get_customer_debt_summary, get_ar_reconciliation,
    find_payment_candidates, get_customer_unpaid_bills,
)
from .pricing_ap import (
    get_product_pricing_summary, get_product_pricing, get_ap_outstanding,
)


# ── Unit conversion ──────────────────────────────────────────────────────────



# ── BSN → Stock sync helpers ─────────────────────────────────────────────────

















# ── Product Code Mapping (BSN ↔ internal SKU) ─────────────────────────────────









# ── Weekly Import ─────────────────────────────────────────────────────────────

















# ── Sales Queries ─────────────────────────────────────────────────────────────









# ── Trade Dashboard ───────────────────────────────────────────────────────────



# ── Product Trade Summary ─────────────────────────────────────────────────────



# ── Commission Overrides (CRUD) ──────────────────────────────────────────────
# commission_overrides table holds per-product or per-brand commission rules
# that beat the tier rate. Schema invariants (DB CHECK):
#   - exactly one of (product_id, brand_id) is set
#   - exactly one of (fixed_per_unit, custom_rate_pct) is set
# Resolution priority (commission.py): product > brand; salesperson-specific
# > generic.
#
# Audit triggers (migration 023) capture every INSERT/UPDATE/DELETE.
# The engine reads commission_overrides fresh on every computation
# (commission._load_overrides has no cache), so a write here is picked up
# automatically — no cache-invalidation call is required for correctness.

















# ── Purchase Queries ──────────────────────────────────────────────────────────



# ── Payment Status ─────────────────────────────────────────────────────────────





















# ── Express AP Outstanding ────────────────────────────────────────────────────



# ── E-commerce Platform SKUs ──────────────────────────────────────────────────

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
    where = "WHERE platform = ?"
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
        "SELECT * FROM platform_skus WHERE platform = ? ORDER BY product_name, variation_name",
        (platform,)
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
        "FROM platform_skus"
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


# ── Product Conversion Formulas (สูตรแปลงสินค้า) ────────────────────────────

def get_conversion_formulas():
    conn = get_connection()
    rows = conn.execute("""
        SELECT cf.id, cf.name, cf.output_product_id, cf.output_qty,
               cf.note, cf.is_active, cf.created_at,
               p.product_name AS output_product_name,
               p.unit_type    AS output_unit_type,
               COUNT(cfi.id)  AS input_count
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
          LEFT JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
         GROUP BY cf.id
         ORDER BY cf.is_active DESC, cf.name
    """).fetchall()
    conn.close()
    return rows


def get_conversion_formula(formula_id):
    conn = get_connection()
    formula = conn.execute("""
        SELECT cf.*, p.product_name AS output_product_name,
               p.unit_type AS output_unit_type,
               COALESCE(sl.quantity, 0) AS output_stock
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cf.output_product_id
         WHERE cf.id = ?
    """, (formula_id,)).fetchone()
    if not formula:
        conn.close()
        return None, []
    inputs = conn.execute("""
        SELECT cfi.id, cfi.product_id, cfi.quantity,
               p.product_name, p.unit_type,
               COALESCE(sl.quantity, 0) AS current_stock
          FROM conversion_formula_inputs cfi
          JOIN products p ON p.id = cfi.product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cfi.product_id
         WHERE cfi.formula_id = ?
         ORDER BY cfi.id
    """, (formula_id,)).fetchall()
    conn.close()
    return formula, inputs


def get_buildable(product_ids=None, conn=None):
    """Pack/unpack 'true availability'. For each product that is the OUTPUT of
    one or more ACTIVE conversion formulas, compute how many EXTRA output units
    could be produced from CURRENT input stock — one level deep, no recursion:

        buildable(P) = Σ over active formulas f with output=P of
                       (min over inputs i of floor(stock(i) / i.quantity)) * f.output_qty

    Returns {product_id: {'buildable': int, 'output_stock': num,
             'true_available': num (= output_stock + buildable),
             'sources': [{'formula_id', 'name', 'output_qty', 'qty'}]}}
    for every product that is such an output (buildable may be 0). When
    product_ids is given, the result is restricted to that set. A tiny epsilon
    absorbs IEEE noise in trigger-maintained stock (verification-discipline).
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        params = []
        filt = ""
        if product_ids is not None:
            ids = list(product_ids)
            if not ids:
                return {}
            filt = " AND cf.output_product_id IN (%s)" % ",".join("?" * len(ids))
            params = ids
        rows = conn.execute(f"""
            SELECT cf.id AS formula_id, cf.name, cf.output_product_id AS out_pid,
                   cf.output_qty,
                   COALESCE(slo.quantity, 0) AS output_stock,
                   cfi.quantity AS input_qty,
                   COALESCE(sli.quantity, 0) AS input_stock
              FROM conversion_formulas cf
              JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
              LEFT JOIN stock_levels slo ON slo.product_id = cf.output_product_id
              LEFT JOIN stock_levels sli ON sli.product_id = cfi.product_id
             WHERE cf.is_active = 1{filt}
        """, params).fetchall()
    finally:
        if own:
            conn.close()

    per_formula = {}
    for r in rows:
        f = per_formula.setdefault(r["formula_id"], {
            "name": r["name"], "out_pid": r["out_pid"],
            "output_qty": r["output_qty"], "output_stock": r["output_stock"],
            "factors": [],
        })
        iq = r["input_qty"]
        # floor(input_stock / input_qty); +1e-9 absorbs IEEE noise (e.g. 5.9999999999999 → 6)
        factor = int((r["input_stock"] + 1e-9) // iq) if iq and iq > 0 else 0
        f["factors"].append(factor)

    result = {}
    for fid, f in per_formula.items():
        qty = (min(f["factors"]) if f["factors"] else 0) * f["output_qty"]
        entry = result.setdefault(f["out_pid"], {
            "buildable": 0, "output_stock": f["output_stock"],
            "true_available": 0, "sources": [],
        })
        entry["buildable"] += qty
        entry["sources"].append({
            "formula_id": fid, "name": f["name"],
            "output_qty": f["output_qty"], "qty": qty,
        })
    for e in result.values():
        e["true_available"] = e["output_stock"] + e["buildable"]
    return result


def upsert_pack_unpack_pair(pack_id, loose_id, ratio, direction='both', note='', conn=None):
    """Create or update the conversion formula(s) for a pack↔loose pair, in one
    call (the /conversions pair-mode form). Idempotent — re-running updates the
    matching formula instead of duplicating.

        PACK   : output=pack_id,  output_qty=1,     inputs=[(loose_id, ratio)]
        UNPACK : output=loose_id, output_qty=ratio, inputs=[(pack_id, 1)]

    direction: 'both' | 'pack' | 'unpack'. Dedup key = (output_product_id,
    frozenset(input_product_ids)) over ACTIVE formulas. Returns
    {'created': int, 'updated': int, 'formula_ids': [...]}.
    """
    ratio = int(ratio)
    own = conn is None
    if own:
        conn = get_connection()
    try:
        def _pinfo(pid):
            r = conn.execute("SELECT product_name, unit_type FROM products WHERE id=?", (pid,)).fetchone()
            return (r["product_name"], r["unit_type"]) if r else (str(pid), "")
        pack_name, _pack_unit = _pinfo(pack_id)
        _loose_name, loose_unit = _pinfo(loose_id)

        specs = []
        if direction in ('both', 'pack'):
            specs.append(dict(name=f"[แพ็ค] {pack_name} ⟵ {ratio} {loose_unit}",
                              output_pid=pack_id, output_qty=1, inputs=[(loose_id, ratio)]))
        if direction in ('both', 'unpack'):
            specs.append(dict(name=f"[แกะ] {pack_name} ⟶ {ratio} {loose_unit}",
                              output_pid=loose_id, output_qty=ratio, inputs=[(pack_id, 1)]))

        created = updated = 0
        formula_ids = []
        for spec in specs:
            want_inputs = frozenset(p for p, _ in spec['inputs'])
            existing = None
            for f in conn.execute("SELECT id FROM conversion_formulas WHERE output_product_id=? AND is_active=1",
                                  (spec['output_pid'],)).fetchall():
                ins = frozenset(r[0] for r in conn.execute(
                    "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?", (f["id"],)))
                if ins == want_inputs:
                    existing = f["id"]
                    break
            if existing is not None:
                conn.execute("UPDATE conversion_formulas SET name=?, output_qty=?, note=? WHERE id=?",
                             (spec['name'], spec['output_qty'], note or None, existing))
                conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (existing,))
                fid = existing
                updated += 1
            else:
                cur = conn.execute(
                    "INSERT INTO conversion_formulas(name, output_product_id, output_qty, note) VALUES (?,?,?,?)",
                    (spec['name'], spec['output_pid'], spec['output_qty'], note or None))
                fid = cur.lastrowid
                created += 1
            for ipid, iqty in spec['inputs']:
                conn.execute("INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
                             (fid, ipid, iqty))
            formula_ids.append(fid)
        if own:
            conn.commit()
        return {'created': created, 'updated': updated, 'formula_ids': formula_ids}
    finally:
        if own:
            conn.close()


def delete_conversion_formula(formula_id, also_delete_id=None):
    """Delete a formula (+ its inputs via the explicit DELETE). When
    `also_delete_id` is given (the reciprocal pack/unpack partner), delete both
    in ONE transaction so a pair is never left half-deleted."""
    conn = get_connection()
    ids = [formula_id]
    if also_delete_id is not None and also_delete_id != formula_id:
        ids.append(also_delete_id)
    for fid in ids:
        conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (fid,))
        conn.execute("DELETE FROM conversion_formulas WHERE id=?", (fid,))
    conn.commit()
    conn.close()


def find_pair_partner(formula_id, conn=None):
    """Return the reciprocal pack/unpack partner row of `formula_id`, or None.

    A pair-half has exactly ONE output and ONE input. The partner P satisfies the
    FULL reciprocal: P.output_product_id == this formula's single input product,
    AND this formula's output_product_id is P's single input product; P active,
    single-input, P != self. Multi-input (general) formulas have no partner.
    Matching the full reciprocal (not output alone) disambiguates a loose product
    shared by several packs. Used so deleting one half of a [แพ็ค]/[แกะ] pair can
    offer to take the other half with it instead of silently orphaning it.
    """
    own = conn is None
    if own:
        conn = get_connection()
    try:
        f = conn.execute(
            "SELECT name, output_product_id FROM conversion_formulas WHERE id=?",
            (formula_id,)).fetchone()
        if f is None:
            return None
        # Only [แพ็ค]/[แกะ] pack-unpack formulas form a pair. A generic reciprocal
        # conversion from the advanced editor is NOT a deletable pair — gate on the
        # prefix so this stays consistent with the list's one-way detector.
        if not (f["name"].startswith('[แพ็ค]') or f["name"].startswith('[แกะ]')):
            return None
        ins = [r["product_id"] for r in conn.execute(
            "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?",
            (formula_id,)).fetchall()]
        if len(ins) != 1:                       # not a clean 1-input pair half
            return None
        my_input, my_output = ins[0], f["output_product_id"]
        for cand in conn.execute("""
            SELECT cf.id, cf.name, cf.output_product_id, cf.output_qty,
                   p.product_name AS output_product_name,
                   p.unit_type    AS output_unit_type
              FROM conversion_formulas cf
              JOIN products p ON p.id = cf.output_product_id
             WHERE cf.is_active=1 AND cf.output_product_id=? AND cf.id<>?
               AND (cf.name LIKE '[แพ็ค]%' OR cf.name LIKE '[แกะ]%')
        """, (my_input, formula_id)).fetchall():
            cins = [r["product_id"] for r in conn.execute(
                "SELECT product_id FROM conversion_formula_inputs WHERE formula_id=?",
                (cand["id"],)).fetchall()]
            # dedup key (output, single-input set) is unique among active formulas,
            # so the first full-reciprocal match is the only one.
            if len(cins) == 1 and cins[0] == my_output:
                return cand                     # full reciprocal match
        return None
    finally:
        if own:
            conn.close()


def derive_pair_from_formula(formula_id, conn=None):
    """Recover the (pack, loose, ratio, direction) that built a [แพ็ค]/[แกะ]
    pair-half, so the pair form can reopen it prefilled for editing.

        PACK   half: output=pack qty1, input=(loose, ratio)  → ratio = input qty
        UNPACK half: output=loose qty ratio, input=(pack, 1)  → ratio = output_qty

    Returns {'pack_id','loose_id','ratio','direction','pack_name','loose_name'},
    or None for anything that is NOT a clean pair-half (missing, no [แพ็ค]/[แกะ]
    prefix, or != 1 input — i.e. a generic/advanced formula has no pair form).
    `direction` is 'both' when the reciprocal partner is present, else the single
    side this formula represents ('pack' or 'unpack')."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        f = conn.execute(
            "SELECT id, name, output_product_id, output_qty, note FROM conversion_formulas WHERE id=?",
            (formula_id,)).fetchone()
        if f is None:
            return None
        name = f["name"] or ""
        is_pack, is_unpack = name.startswith('[แพ็ค]'), name.startswith('[แกะ]')
        if not (is_pack or is_unpack):
            return None                          # generic/advanced formula — no pair form
        ins = [(r["product_id"], r["quantity"]) for r in conn.execute(
            "SELECT product_id, quantity FROM conversion_formula_inputs WHERE formula_id=?",
            (formula_id,)).fetchall()]
        if len(ins) != 1:
            return None                          # not a clean 1-input pair half
        in_pid, in_qty = ins[0]
        if is_pack:
            pack_id, loose_id, ratio = f["output_product_id"], in_pid, in_qty
        else:                                    # [แกะ]
            loose_id, pack_id, ratio = f["output_product_id"], in_pid, f["output_qty"]
        direction = 'both' if find_pair_partner(formula_id, conn=conn) is not None \
                    else ('pack' if is_pack else 'unpack')

        def _name(pid):
            r = conn.execute("SELECT product_name FROM products WHERE id=?", (pid,)).fetchone()
            return r["product_name"] if r else str(pid)

        return {'pack_id': pack_id, 'loose_id': loose_id, 'ratio': int(ratio),
                'direction': direction, 'pack_name': _name(pack_id), 'loose_name': _name(loose_id),
                'note': f["note"] or ''}
    finally:
        if own:
            conn.close()


def get_recent_conversion_runs(limit=5):
    conn = get_connection()
    rows = conn.execute("""
        SELECT ccl.id, ccl.reference_no, ccl.event_date, ccl.created_at,
               ccl.output_qty, ccl.unit_cost, ccl.total_input_cost,
               p.product_name AS output_product_name,
               p.unit_type    AS output_unit_type
          FROM conversion_cost_log ccl
          JOIN products p ON p.id = ccl.output_product_id
         ORDER BY ccl.id DESC
         LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def run_conversion(formula_id, multiplier, reference_no='', extra_note='', writeoff_qty=0):
    """Run a conversion. `writeoff_qty` = output units scrapped during the run
    (ของเสีย, e.g. 10 แผง → 20 ตัว but 1 broke). Inputs are still fully consumed;
    only GOOD units (expected − writeoff) enter stock; input cost spreads over
    the good units (scrap raises good-unit cost). Broken units never enter stock.
    """
    from datetime import datetime as _dt
    conn = get_connection()
    formula = conn.execute("""
        SELECT cf.*, p.product_name AS output_product_name
          FROM conversion_formulas cf
          JOIN products p ON p.id = cf.output_product_id
         WHERE cf.id = ?
    """, (formula_id,)).fetchone()
    if not formula:
        conn.close()
        return False, 'ไม่พบสูตรการแปลง', {}

    # write-off (ของเสีย) — output units scrapped this run
    try:
        writeoff_qty = max(0, int(writeoff_qty or 0))
    except (ValueError, TypeError):
        writeoff_qty = 0
    expected_qty = formula['output_qty'] * multiplier
    if writeoff_qty > expected_qty:
        conn.close()
        return False, f'ตัดของเสียได้ไม่เกินจำนวนที่ผลิต ({expected_qty:,})', {}
    good_qty = expected_qty - writeoff_qty

    inputs = conn.execute("""
        SELECT cfi.*, p.product_name, p.unit_type,
               COALESCE(sl.quantity, 0) AS current_stock
          FROM conversion_formula_inputs cfi
          JOIN products p ON p.id = cfi.product_id
          LEFT JOIN stock_levels sl ON sl.product_id = cfi.product_id
         WHERE cfi.formula_id = ?
    """, (formula_id,)).fetchall()

    shortage = []
    for inp in inputs:
        needed = inp['quantity'] * multiplier
        if inp['current_stock'] < needed:
            shortage.append(
                f'{inp["product_name"]}: ต้องการ {needed:,} แต่มีแค่ {inp["current_stock"]:,} {inp["unit_type"]}'
            )
    if shortage:
        conn.close()
        return False, 'สต็อกไม่พอ: ' + ' | '.join(shortage), {}

    # ── WACC: คำนวณต้นทุน output จาก input WACCs ──────────────────────────
    total_input_cost = 0.0
    for inp in inputs:
        needed   = inp['quantity'] * multiplier
        inp_wacc = get_current_wacc(inp['product_id'], conn)
        total_input_cost += needed * inp_wacc

    # cost spreads over GOOD output only (scrap loss raises good-unit cost)
    output_unit_cost = total_input_cost / good_qty if good_qty > 0 else 0.0

    # ใช้ reference_no ที่ user ส่งมา หรือ generate ใหม่
    conv_ref = reference_no or f'CONV{formula_id}-{_dt.now().strftime("%Y%m%d%H%M%S")}'

    note_text = f'แปลง: {formula["name"]}'
    if extra_note:
        note_text += f' | {extra_note}'
    if writeoff_qty:
        note_text += f' | ตัดของเสีย {writeoff_qty:,}'

    for inp in inputs:
        needed = inp['quantity'] * multiplier
        conn.execute(
            "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
            " VALUES (?,?,?,?,?,?)",
            (inp['product_id'], 'OUT', -needed, 'unit', conv_ref, note_text)
        )

    # only GOOD units enter stock; a total loss (good_qty=0) adds nothing
    if good_qty > 0:
        conn.execute(
            "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
            " VALUES (?,?,?,?,?,?)",
            (formula['output_product_id'], 'IN', good_qty, 'unit', conv_ref, note_text)
        )

    # บันทึก conversion cost log (ใช้ตอน recalculate WACC output)
    conn.execute(
        "INSERT INTO conversion_cost_log"
        " (output_product_id, reference_no, event_date, output_qty, total_input_cost, unit_cost, writeoff_qty)"
        " VALUES (?,?,date('now'),?,?,?,?)",
        (formula['output_product_id'], conv_ref, good_qty, total_input_cost, output_unit_cost, writeoff_qty)
    )

    conn.commit()

    # Recalculate WACC for all involved products
    involved = [inp['product_id'] for inp in inputs] + [formula['output_product_id']]
    recalculate_waccs_for_products(involved)

    conn.close()
    msg = f'แปลงสำเร็จ: ได้ {good_qty:,} {formula["output_product_name"]}'
    if writeoff_qty:
        msg += f' (ตัดของเสีย {writeoff_qty:,})'
    return True, msg, {
        'output_qty': good_qty,
        'writeoff_qty': writeoff_qty,
        'output_name': formula['output_product_name'],
    }


# ── Accounting Summary ────────────────────────────────────────────────────────

def get_accounting_summary(date_from=None, date_to=None):
    """
    Aggregate profit / cost / expenses / commission for the /accounting page.

    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has sales data.

    Revenue  = SUM(net) from sales_transactions          — pre-VAT, post-doc-discount
    COGS     = SUM(qty * cost_price) from products        — current cost_price (WACC basis)
               Lines where product has no cost_price are counted separately (no_cost_lines)
    Expenses = SUM(amount_pre_vat) from expense_log       — 0 rows currently, shown as 0
    Commission = SUM(amount_paid) from commission_payouts — actual paid, by year_month overlap

    company_id = 1 (BSN) is the only scope in this DB.
    """
    import calendar as _cal

    conn = get_connection()

    # ── Resolve default period ────────────────────────────────────────────────
    if not date_from and not date_to:
        # Latest month with sales data
        row = conn.execute(
            "SELECT MAX(date_iso) AS mx FROM sales_transactions"
        ).fetchone()
        if row and row['mx']:
            from datetime import datetime as _dt
            latest = _dt.strptime(row['mx'][:7], '%Y-%m')
            date_from = latest.strftime('%Y-%m-01')
            date_to = latest.strftime(
                f'%Y-%m-{_cal.monthrange(latest.year, latest.month)[1]:02d}'
            )
        else:
            today = date.today()
            date_from = today.strftime('%Y-%m-01')
            date_to = today.strftime(
                f'%Y-%m-{_cal.monthrange(today.year, today.month)[1]:02d}'
            )
    elif date_from and not date_to:
        date_to = date.today().isoformat()
    elif date_to and not date_from:
        date_from = '2000-01-01'

    # ── Revenue (sales net) ───────────────────────────────────────────────────
    s = conn.execute("""
        SELECT COALESCE(SUM(net), 0)  AS total_net,
               COUNT(*)               AS line_count,
               COUNT(DISTINCT doc_no) AS doc_count
          FROM sales_transactions
         WHERE date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()
    sales_net = float(s['total_net'])

    # ── COGS (current cost_price × qty; unmapped lines counted separately) ────
    cogs_row = conn.execute("""
        SELECT COALESCE(SUM(st.qty * COALESCE(p.cost_price, 0)), 0) AS cogs,
               COUNT(CASE WHEN p.cost_price IS NULL THEN 1 END)     AS no_cost_lines,
               COUNT(CASE WHEN p.cost_price = 0    THEN 1 END)      AS zero_cost_lines
          FROM sales_transactions st
          LEFT JOIN products p ON p.id = st.product_id
         WHERE st.date_iso >= ? AND st.date_iso <= ?
    """, (date_from, date_to)).fetchone()
    cogs = float(cogs_row['cogs'])
    no_cost_lines = cogs_row['no_cost_lines'] or 0
    zero_cost_lines = cogs_row['zero_cost_lines'] or 0

    # ── Gross profit ──────────────────────────────────────────────────────────
    gross_profit = sales_net - cogs
    margin_pct = (gross_profit / sales_net * 100.0) if sales_net > 0 else 0.0

    # ── Expenses (expense_log, BSN = company_id 1) ────────────────────────────
    exp_total = conn.execute("""
        SELECT COALESCE(SUM(amount_pre_vat), 0) AS total
          FROM expense_log
         WHERE company_id = 1
           AND date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()
    expenses = float(exp_total['total'])

    # Expenses by category
    exp_by_cat = conn.execute("""
        SELECT ec.name_th AS category_name,
               ec.code    AS category_code,
               COALESCE(SUM(el.amount_pre_vat), 0) AS total
          FROM expense_categories ec
          LEFT JOIN expense_log el ON el.category_id = ec.id
                AND el.company_id = 1
                AND el.date_iso >= ? AND el.date_iso <= ?
         WHERE ec.is_active = 1
         GROUP BY ec.id, ec.code, ec.name_th, ec.sort_order
         ORDER BY ec.sort_order
    """, (date_from, date_to)).fetchall()

    # ── Commission (actual paid, overlapping the period's months) ─────────────
    # Extract YYYY-MM range from the date filter, match commission_payouts.year_month
    ym_from = date_from[:7]
    ym_to = date_to[:7]
    comm_row = conn.execute("""
        SELECT COALESCE(SUM(amount_paid), 0) AS total
          FROM commission_payouts
         WHERE year_month >= ? AND year_month <= ?
    """, (ym_from, ym_to)).fetchone()
    commission_total = float(comm_row['total'])

    # ── Net profit (approximate) ──────────────────────────────────────────────
    net_profit = gross_profit - expenses - commission_total

    # ── Brand breakdown (own-brands first per CLAUDE.md priority) ────────────
    # Own-brand order: Golden Lion (sort 10) → A-SPEC (sort 20) → Sendai (sort 30)
    # then 3rd-party by sort_order → finally NULL brand rows
    brand_rows = conn.execute("""
        SELECT
          COALESCE(b.name_th, b.name, '(ไม่ระบุแบรนด์)') AS brand_label,
          b.is_own_brand,
          COALESCE(b.sort_order, 9999)                    AS sort_ord,
          ROUND(SUM(st.net), 2)                           AS sales_net,
          ROUND(SUM(st.qty * COALESCE(p.cost_price, 0)), 2) AS cogs_approx,
          COUNT(st.id)                                    AS line_count,
          COUNT(CASE WHEN p.cost_price IS NULL OR p.cost_price = 0 THEN 1 END)
                                                          AS no_cost_lines
        FROM sales_transactions st
        LEFT JOIN products  p ON p.id = st.product_id
        LEFT JOIN brands    b ON b.id = p.brand_id
        WHERE st.date_iso >= ? AND st.date_iso <= ?
        GROUP BY b.id, b.name, b.name_th, b.is_own_brand, b.sort_order
        ORDER BY COALESCE(b.is_own_brand, 0) DESC,
                 COALESCE(b.sort_order, 9999),
                 SUM(st.net) DESC
    """, (date_from, date_to)).fetchall()

    brand_breakdown = []
    for r in brand_rows:
        sn = float(r['sales_net'] or 0)
        cg = float(r['cogs_approx'] or 0)
        gp = sn - cg
        mp = (gp / sn * 100.0) if sn > 0 else 0.0
        brand_breakdown.append({
            'brand_label': r['brand_label'],
            'is_own_brand': bool(r['is_own_brand']),
            'sales_net': sn,
            'cogs_approx': cg,
            'gross_profit': gp,
            'margin_pct': mp,
            'line_count': r['line_count'],
            'no_cost_lines': r['no_cost_lines'] or 0,
        })

    # ── Available months (for period selector) ────────────────────────────────
    months_rows = conn.execute("""
        SELECT DISTINCT strftime('%Y-%m', date_iso) AS ym
          FROM sales_transactions
         ORDER BY ym DESC
         LIMIT 36
    """).fetchall()
    available_months = [r['ym'] for r in months_rows]

    conn.close()

    return {
        'date_from': date_from,
        'date_to': date_to,
        'sales_net': sales_net,
        'doc_count': s['doc_count'],
        'line_count': s['line_count'],
        'cogs': cogs,
        'no_cost_lines': no_cost_lines,
        'zero_cost_lines': zero_cost_lines,
        'gross_profit': gross_profit,
        'margin_pct': margin_pct,
        'expenses': expenses,
        'expenses_by_category': [dict(r) for r in exp_by_cat],
        'commission_total': commission_total,
        'net_profit': net_profit,
        'brand_breakdown': brand_breakdown,
        'available_months': available_months,
    }


# ── Ecommerce Listing Mapping ──────────────────────────────────────────────────

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



# ── Pending product suggestions (smart BSN mapping) ─────────────────────────

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


# ---------------------------------------------------------------------------
# Marketplace orders (Shopee/Lazada order-export import)
#
# Operational tracking only — kept SEPARATE from sales_transactions (marketplace
# revenue already enters the ledger via the weekly Express import as หน้าร้านS/B/L,
# so writing it here too would double-count). See migration 093 + parse_orders.py.
# ---------------------------------------------------------------------------

def resolve_marketplace_product_id(conn, platform, item):
    """Resolve one parsed order line -> internal products.id via platform_skus.

    Tries, in order: variation_id (Lazada lazadaSku), seller_sku, then
    (product_name, variation_name) (Shopee, which exports no SKU/variation id).
    Returns the product id or None (None => surfaced on /marketplace/unmapped).
    """
    vid = item.get('variation_id')
    if vid:
        r = conn.execute(
            "SELECT internal_product_id FROM platform_skus "
            "WHERE platform=? AND variation_id=? AND internal_product_id IS NOT NULL "
            "LIMIT 1", (platform, vid)).fetchone()
        if r:
            return r[0]
    ssku = item.get('seller_sku')
    if ssku:
        r = conn.execute(
            "SELECT internal_product_id FROM platform_skus "
            "WHERE platform=? AND seller_sku=? AND internal_product_id IS NOT NULL "
            "LIMIT 1", (platform, ssku)).fetchone()
        if r:
            return r[0]
    name = item.get('item_name')
    if name:
        r = conn.execute(
            "SELECT internal_product_id FROM platform_skus "
            "WHERE platform=? AND product_name=? AND IFNULL(variation_name,'')=? "
            "AND internal_product_id IS NOT NULL LIMIT 1",
            (platform, name, item.get('variation_name') or '')).fetchone()
        if r:
            return r[0]
    return None


def import_marketplace_orders(conn, orders, source_file=None):
    """Upsert parsed marketplace orders (from parse_orders.py) into
    marketplace_orders / marketplace_order_items. Idempotent: re-importing the
    same order updates the header and rebuilds its lines (handles edits/removals).
    Returns stats dict. Caller owns the connection (commits here)."""
    stats = {'orders': 0, 'items': 0, 'unmapped': 0, 'lines_resolved': 0}
    for o in orders:
        conn.execute(
            """INSERT INTO marketplace_orders
                   (platform, order_sn, status, buyer_name, buyer_phone, ship_address,
                    order_date, paid_date, item_total, marketplace_fee, payout,
                    currency, source_file, raw_json, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
               ON CONFLICT(platform, order_sn) DO UPDATE SET
                   status=excluded.status, buyer_name=excluded.buyer_name,
                   buyer_phone=excluded.buyer_phone, ship_address=excluded.ship_address,
                   order_date=excluded.order_date, paid_date=excluded.paid_date,
                   item_total=excluded.item_total, marketplace_fee=excluded.marketplace_fee,
                   payout=excluded.payout, currency=excluded.currency,
                   source_file=excluded.source_file, raw_json=excluded.raw_json,
                   last_synced_at=datetime('now','localtime')""",
            (o['platform'], o['order_sn'], o.get('status'), o.get('buyer_name'),
             o.get('buyer_phone'), o.get('ship_address'), o.get('order_date'),
             o.get('paid_date'), o.get('item_total'), o.get('marketplace_fee'),
             o.get('payout'), o.get('currency', 'THB'), source_file,
             json.dumps(o, ensure_ascii=False)))

        oid = conn.execute(
            "SELECT id FROM marketplace_orders WHERE platform=? AND order_sn=?",
            (o['platform'], o['order_sn'])).fetchone()[0]

        # Rebuild this order's lines so re-import reflects the latest export.
        conn.execute("DELETE FROM marketplace_order_items WHERE order_id=?", (oid,))
        for it in o.get('items', []):
            pid = resolve_marketplace_product_id(conn, o['platform'], it)
            if pid is None:
                stats['unmapped'] += 1
            else:
                stats['lines_resolved'] += 1
            conn.execute(
                """INSERT INTO marketplace_order_items
                       (order_id, platform, order_sn, line_key, seller_sku, variation_id,
                        item_name, variation_name, internal_product_id, qty, unit_price,
                        item_subtotal)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, o['platform'], o['order_sn'], it['line_key'], it.get('seller_sku'),
                 it.get('variation_id'), it.get('item_name'), it.get('variation_name'),
                 pid, it.get('qty'), it.get('unit_price'), it.get('item_subtotal')))
            stats['items'] += 1
        stats['orders'] += 1

    conn.commit()
    return stats


def upsert_marketplace_settlements(conn, settlements, source_file=None,
                                   platform='shopee'):
    """Stamp actual_payout + settled_at on marketplace_orders matched by order_sn.

    Args:
        conn: DB connection.
        settlements: list of {order_sn, actual_payout, settled_at}.
        source_file: filename of the Income Transfer file (for traceability).
        platform: marketplace this Income file belongs to. The table key is
            UNIQUE(platform, order_sn), so we scope the UPDATE by platform too —
            a bare order_sn match could stamp a different platform's row that
            happens to share the same order number.

    Returns:
        {'updated': int, 'not_found': int, 'skipped_no_date': int}

    A settlement with a blank settled_at is NOT actually settled (the parser
    emits '' for an empty transfer-date cell). Stamping it would both create a
    phantom batch keyed on '' and set actual_payout, hiding the order from the
    pending list. So we skip those rows entirely, leaving the order NULL/pending.
    """
    updated = 0
    not_found = 0
    skipped_no_date = 0
    for s in settlements:
        settled_at = (s.get('settled_at') or '').strip()
        if not settled_at:
            skipped_no_date += 1
            continue
        cur = conn.execute(
            """UPDATE marketplace_orders
               SET actual_payout = ?, settled_at = ?, settlement_source = ?
               WHERE platform = ? AND order_sn = ?""",
            (s['actual_payout'], settled_at, source_file, platform, s['order_sn']),
        )
        if cur.rowcount:
            updated += 1
        else:
            not_found += 1
    conn.commit()
    return {'updated': updated, 'not_found': not_found,
            'skipped_no_date': skipped_no_date}


def upsert_marketplace_fees(conn, fee_rows, source_file=None, platform='shopee'):
    """Insert/replace per-order fee rows into marketplace_order_fees.
    Keyed UNIQUE(platform, order_sn). Returns count upserted."""
    n = 0
    for f in fee_rows:
        conn.execute(
            """INSERT INTO marketplace_order_fees
                 (platform, order_sn, item_value, fee_commission, fee_service,
                  fee_transaction, fee_platform, fee_ads_escrow, fee_tax,
                  shipping_net, fee_saver, fee_total, net_payout, fee_pct,
                  fee_raw_json, source_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform, order_sn) DO UPDATE SET
                  item_value=excluded.item_value, fee_commission=excluded.fee_commission,
                  fee_service=excluded.fee_service, fee_transaction=excluded.fee_transaction,
                  fee_platform=excluded.fee_platform, fee_ads_escrow=excluded.fee_ads_escrow,
                  fee_tax=excluded.fee_tax, shipping_net=excluded.shipping_net,
                  fee_saver=excluded.fee_saver, fee_total=excluded.fee_total,
                  net_payout=excluded.net_payout, fee_pct=excluded.fee_pct,
                  fee_raw_json=excluded.fee_raw_json, source_file=excluded.source_file""",
            (platform, f['order_sn'], f.get('item_value'), f.get('fee_commission', 0),
             f.get('fee_service', 0), f.get('fee_transaction', 0), f.get('fee_platform', 0),
             f.get('fee_ads_escrow', 0), f.get('fee_tax', 0), f.get('shipping_net', 0),
             f.get('fee_saver', 0), f.get('fee_total'), f.get('net_payout'),
             f.get('fee_pct'), f.get('fee_raw_json'), source_file))
        n += 1
    conn.commit()
    return n


def import_wallet_txns(conn, wallet_rows, source_file=None, platform='shopee'):
    """Insert wallet ledger rows. Idempotent via UNIQUE(platform,txn_time,
    txn_type,order_sn,amount) + INSERT OR IGNORE. Returns count newly inserted.
    order_sn is stored as '' (not NULL) so the UNIQUE index fires on re-import
    (SQLite treats two NULLs as distinct in UNIQUE constraints)."""
    n = 0
    for r in wallet_rows:
        sn = r.get('order_sn') or ''
        cur = conn.execute(
            """INSERT OR IGNORE INTO marketplace_wallet_txns
                 (platform, txn_time, txn_type, order_sn, amount, running_balance,
                  description, source_file)
               VALUES (?,?,?,?,?,?,?,?)""",
            (platform, r['txn_time'], r['txn_type'], sn,
             r['amount'], r.get('running_balance'), r.get('description'), source_file))
        n += cur.rowcount
    conn.commit()
    return n


def upsert_lazada_settlements(conn, settlements):
    """Insert/replace per-statement (รอบบิล) settlement times from the Lazada wallet
    Deposit/Settlement rows. Keyed by statement (PK) so a re-import corrects in place.
    reconcile_payouts re-anchors Lazada income timing to these. Returns row count."""
    n = 0
    for s in settlements:
        conn.execute(
            """INSERT OR REPLACE INTO lazada_statement_settlement (statement, settled_at, amount)
               VALUES (?,?,?)""",
            (s['statement'], s['settled_at'], s['amount']))
        n += 1
    conn.commit()
    return n


def get_payout_years(conn, platform='shopee'):
    """Distinct years that have bank deposits, newest first (for the year filter)."""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT substr(deposit_date, 1, 4) AS y
           FROM marketplace_payouts WHERE platform = ?
           ORDER BY y DESC""", (platform,)).fetchall()]


def get_payout_summaries(conn, platform='shopee', year=None, limit=1000):
    """Bank-deposit summaries (newest first), NO per-order rows — the lazy-load
    payload for the deposits tab. Each: id, deposit_date, amount, n_orders,
    status, fee_total (Σ per-order fee, with the same settled/estimate fallback
    as get_payout_orders, aggregated in one GROUP BY). Order rows are fetched
    on expand via get_payout_orders so the page ships ~20KB not ~300KB.
    year=None = all years; 'YYYY' scopes to one (limit is a high backstop only)."""
    where = "WHERE mp.platform = ?"
    params = [platform]
    if year:
        where += " AND substr(mp.deposit_date, 1, 4) = ?"
        params.append(str(year))
    rows = conn.execute(
        f"""SELECT mp.id, mp.deposit_date, mp.amount, mp.n_orders, mp.status,
                   ROUND(COALESCE(SUM(
                       COALESCE(f.fee_total,
                                CASE WHEN o.item_total IS NOT NULL
                                      AND COALESCE(w.wallet_net, o.actual_payout) IS NOT NULL
                                     THEN ROUND(o.item_total
                                                - COALESCE(w.wallet_net, o.actual_payout), 2)
                                END)), 0), 2) AS fee_total
            FROM marketplace_payouts mp
            LEFT JOIN marketplace_orders o
                   ON o.platform = mp.platform AND o.payout_id = mp.id
            LEFT JOIN marketplace_order_fees f
                   ON f.platform = o.platform AND f.order_sn = o.order_sn
            LEFT JOIN (SELECT order_sn, SUM(amount) AS wallet_net
                       FROM marketplace_wallet_txns
                       WHERE platform = ? AND txn_type = 'income'
                       GROUP BY order_sn) w
                   ON w.order_sn = o.order_sn
            {where}
            GROUP BY mp.id, mp.deposit_date, mp.amount, mp.n_orders, mp.status
            ORDER BY mp.deposit_date DESC, mp.id DESC LIMIT ?""",
        (platform, *params, limit)).fetchall()
    return [dict(r) for r in rows]


# Fee buckets → Thai category labels for the fee breakdown (settlement-page hover
# tooltip + order-detail modal tooltip). Both Shopee and Lazada parsers fill these
# typed columns (Lazada maps its raw English statement lines into the same buckets),
# so ONE clean Thai-category breakdown serves both platforms — not the raw rows.
from marketplace_fee_buckets import LAZADA_BUCKET, GRANULAR_LABEL

_FEE_LABELS = [
    ('fee_commission',  'ค่าคอมมิชชั่น'),
    ('fee_service',     'ค่าบริการ'),
    ('fee_transaction', 'ค่าธุรกรรมการชำระเงิน'),
    ('fee_platform',    'ค่าธรรมเนียมแพลตฟอร์ม'),
    ('fee_ads_escrow',  'ค่าโฆษณา/โปรโมชั่น'),
    ('fee_tax',         'ภาษี'),
    ('shipping_net',    'ค่าจัดส่ง (สุทธิ)'),
    ('fee_saver',       'ค่าโปรแกรมประหยัดค่าจัดส่ง'),
]


def _smart_label(col, generic, raw):
    """Lazada smart label: when a bucket came from ONE underlying fee type, show
    that fee's real name (e.g. a LazCoins-only ค่าโฆษณา/โปรโมชั่น bucket → 'ส่วนลด
    LazCoins'); keep the generic category when 2+ fee types combined into the bucket.
    `raw` = the parsed fee_raw_json dict ({raw_label: amount})."""
    names = set()
    for lbl, amt in raw.items():
        if isinstance(amt, (int, float)) and round(amt, 2) != 0.0 \
                and LAZADA_BUCKET.get(lbl, 'fee_platform') == col:
            names.add(GRANULAR_LABEL.get(lbl, generic))
    return names.pop() if len(names) == 1 else generic


def _bucket_fee_lines(d, fee_raw_json=None, platform=None):
    """Ordered fee-breakdown lines for ONE settled order (Shopee or Lazada), from
    the typed fee bucket columns: positive มูลค่าสินค้า first, each non-zero fee next
    (biggest deduction first), then a reconciling residual so Σ == net_payout (the
    footer). Returns None when there is no settled breakdown (item/net missing).
    For Lazada, a single-source bucket is relabelled to its real fee name (see
    _smart_label); Shopee keeps the generic categories (its buckets are its real fees).
    `d` is a row dict carrying item_value, net_payout + the fee_* bucket columns."""
    item, net = d.get('item_value'), d.get('net_payout')
    if item is None or net is None:
        return None
    raw = None
    if platform == 'lazada' and fee_raw_json:
        try:
            raw = json.loads(fee_raw_json)
        except (ValueError, TypeError):
            raw = None
    lines = [{'label': 'มูลค่าสินค้า', 'amount': round(item, 2)}]
    fees = 0.0
    for col, label in _FEE_LABELS:
        v = d.get(col) or 0.0
        if round(v, 2) != 0.0:
            disp = _smart_label(col, label, raw) if raw else label
            lines.append({'label': disp, 'amount': round(v, 2)})
            fees += v
    residual = round(net - item - fees, 2)
    if abs(residual) >= 0.01:
        lines.append({'label': 'อื่นๆ', 'amount': residual})
    lines.sort(key=lambda x: (x['amount'] < 0, -abs(x['amount'])))
    return lines


def _fee_pct_str(item_value, fee_total):
    """Total take-rate 'X.X%' = fee_total / item_value, computed IDENTICALLY for both
    platforms so the settlement % is comparable. Shopee's Income file ships a partial
    'ค่าธรรมเนียม (%)' column (~3.21% — only the transaction fee); Lazada already
    computes the total. We compute the true total deduction here for both instead of
    trusting the per-platform stored value. Blank when there is no positive ยอดสินค้า."""
    if not item_value or item_value <= 0 or fee_total is None:
        return ''
    return f"{round(fee_total / item_value * 100, 1)}%"


def get_payout_orders(conn, platform, payout_id):
    """The order rows for ONE bank deposit (fetched on expand). Fills the 3
    columns (item_value / fee_total / net_payout) from the best source, tagged
    via fee_source so the UI can badge it:
      settled — marketplace_order_fees row (Income file, authoritative breakdown)
      wallet  — no Income, but Seller-Balance net credit (w.wallet_net);
                item_value from Order export, fee_total = item_total − net estimate
      order   — Order export only (no wallet, no Income)
    f.* and actual_payout both come from the same Income import → f ⇒ settled."""
    rows = conn.execute(
        """SELECT o.id, o.order_sn, o.settled_at,
                  COALESCE(f.item_value, o.item_total) AS item_value,
                  COALESCE(f.fee_total,
                           CASE WHEN o.item_total IS NOT NULL
                                 AND COALESCE(w.wallet_net, o.actual_payout) IS NOT NULL
                                THEN ROUND(o.item_total
                                           - COALESCE(w.wallet_net, o.actual_payout), 2)
                           END) AS fee_total,
                  COALESCE(f.net_payout, w.wallet_net, o.actual_payout) AS net_payout,
                  f.fee_pct, f.fee_raw_json,
                  f.fee_commission, f.fee_service, f.fee_transaction, f.fee_platform,
                  f.fee_ads_escrow, f.fee_tax, f.shipping_net, f.fee_saver,
                  CASE WHEN f.order_sn IS NOT NULL THEN 'settled'
                       WHEN w.wallet_net IS NOT NULL OR o.actual_payout IS NOT NULL THEN 'wallet'
                       WHEN o.item_total IS NOT NULL OR o.payout IS NOT NULL THEN 'order'
                       ELSE 'none' END AS fee_source,
                  moi.doc_base   AS matched_iv,
                  moi.confidence AS iv_confidence
           FROM marketplace_orders o
           LEFT JOIN marketplace_order_fees f
                  ON f.platform = o.platform AND f.order_sn = o.order_sn
           LEFT JOIN marketplace_order_invoice moi
                  ON moi.platform = o.platform AND moi.order_sn = o.order_sn
           LEFT JOIN (SELECT order_sn, SUM(amount) AS wallet_net
                      FROM marketplace_wallet_txns
                      WHERE platform = ? AND txn_type = 'income'
                      GROUP BY order_sn) w
                  ON w.order_sn = o.order_sn
           WHERE o.platform = ? AND o.payout_id = ?
           ORDER BY o.settled_at, o.order_sn""",
        (platform, platform, payout_id)).fetchall()
    out = []
    for o in rows:
        d = dict(o)
        # One clean Thai-category breakdown for both platforms, from the typed
        # bucket columns (only when settled — else show the estimate notice).
        # Lazada uses fee_raw_json to smart-label single-source buckets.
        d['fee_lines'] = (_bucket_fee_lines(d, d.get('fee_raw_json'), platform)
                          if d.get('fee_source') == 'settled' else None)
        # Unify the settlement % across platforms: total take-rate = fee ÷ ยอดสินค้า,
        # computed here for both (Shopee's stored fee_pct is only its partial
        # transaction-fee %). Only settled rows carry a %, same footprint as before.
        d['fee_pct'] = (_fee_pct_str(d.get('item_value'), d.get('fee_total'))
                        if d.get('fee_source') == 'settled' else None)
        d.pop('fee_raw_json', None)
        for col, _label in _FEE_LABELS:
            d.pop(col, None)
        out.append(d)
    return out


def get_deposit_tab_extras(conn, platform='shopee'):
    """Two light buckets shown under the deposit cards so no order is invisible
    once the per-settled-date 'daily' view is gone:
      'orphan'  — settled (actual_payout set) but not tied to any bank deposit
                  (e.g. Income imported before the Seller-Balance file). These
                  would otherwise show under NO deposit card.
      'pending' — not yet settled (no actual_payout), excluding cancelled.
    No per-order N+1; two flat queries."""
    orphan = conn.execute(
        """SELECT id, order_sn, settled_at, COALESCE(item_total, 0) AS item_total,
                  actual_payout
           FROM marketplace_orders
           WHERE platform = ? AND actual_payout IS NOT NULL AND payout_id IS NULL
           ORDER BY settled_at ASC, order_sn ASC""",
        (platform,)).fetchall()
    pending = conn.execute(
        """SELECT id, order_sn, COALESCE(item_total, 0) AS item_total,
                  status, order_date
           FROM marketplace_orders
           WHERE platform = ? AND actual_payout IS NULL
             AND status NOT IN ('ยกเลิกแล้ว')
           ORDER BY order_date DESC""",
        (platform,)).fetchall()
    return {'orphan': [dict(r) for r in orphan],
            'pending': [dict(r) for r in pending]}


def get_payout_report(conn, platform='shopee', year=None, limit=1000):
    """Bank deposits (newest first) each WITH their order rows attached —
    summaries composed with get_payout_orders. Kept for callers/tests that want
    the full nested shape; the deposits tab itself uses get_payout_summaries +
    lazy get_payout_orders to keep the page light."""
    deposits = get_payout_summaries(conn, platform, year, limit)
    for d in deposits:
        d['orders'] = get_payout_orders(conn, platform, d['id'])
    return deposits


def get_settlement_report(conn, platform='shopee'):
    """Return settlement data grouped by payout date for the AR clearance report.

    Returns:
        {
          'batches': [
            {
              'settled_at': '2026-06-01',
              'order_count': 39,
              'total_payout': 8149.0,
              'orders': [{order_sn, item_total, actual_payout, fee_diff, status, ...}]
            },
            ...
          ],
          'pending': [{order_sn, item_total, status, order_date}]  # not yet settled
        }
    """
    # Settled orders grouped by date
    batch_rows = conn.execute(
        """SELECT settled_at, COUNT(*) as order_count, SUM(actual_payout) as total_payout
           FROM marketplace_orders
           WHERE platform = ? AND settled_at IS NOT NULL
           GROUP BY settled_at
           ORDER BY settled_at DESC""",
        (platform,)
    ).fetchall()

    batches = []
    for batch in batch_rows:
        orders = conn.execute(
            """SELECT mo.id, mo.order_sn, COALESCE(mo.item_total, 0) as item_total,
                      mo.marketplace_fee, mo.actual_payout,
                      ROUND(COALESCE(mo.item_total, 0) - mo.actual_payout, 2) as fee_diff,
                      mo.status, mo.order_date, mo.settlement_source,
                      moi.doc_base AS matched_iv, moi.confidence AS iv_confidence
               FROM marketplace_orders mo
               LEFT JOIN marketplace_order_invoice moi
                      ON moi.platform = mo.platform AND moi.order_sn = mo.order_sn
               WHERE mo.platform = ? AND mo.settled_at = ?
               ORDER BY mo.order_date""",
            (platform, batch[0])
        ).fetchall()
        batches.append({
            'settled_at':   batch[0],
            'order_count':  batch[1],
            'total_payout': round(batch[2] or 0, 2),
            'orders': [dict(zip(
                ['id', 'order_sn', 'item_total', 'marketplace_fee', 'actual_payout',
                 'fee_diff', 'status', 'order_date', 'settlement_source',
                 'matched_iv', 'iv_confidence'], o
            )) for o in orders],
        })

    # Pending: settled orders not yet stamped
    pending_rows = conn.execute(
        """SELECT id, order_sn, COALESCE(item_total, 0) as item_total, status, order_date
           FROM marketplace_orders
           WHERE platform = ? AND actual_payout IS NULL
             AND status NOT IN ('ยกเลิกแล้ว')
           ORDER BY order_date DESC""",
        (platform,)
    ).fetchall()
    pending = [dict(zip(['id', 'order_sn', 'item_total', 'status', 'order_date'], r))
               for r in pending_rows]

    return {'batches': batches, 'pending': pending}


def get_marketplace_order(conn, order_id):
    """One marketplace order row (needed by the IV picker / matcher)."""
    return conn.execute(
        """SELECT mo.*,
                  CASE WHEN mo.platform='lazada'
                       THEN COALESCE(f.item_value, mo.item_total, mo.actual_payout)
                       ELSE mo.actual_payout END AS billed_basis
           FROM marketplace_orders mo
           LEFT JOIN marketplace_order_fees f
                  ON f.platform=mo.platform AND f.order_sn=mo.order_sn
           WHERE mo.id = ?""",
        (order_id,)
    ).fetchone()


def get_marketplace_order_detail(conn, order_id):
    """Order header + line items + matched IV, for the drill-down modal.
    Returns None if the order id doesn't exist."""
    o = conn.execute(
        """SELECT mo.id, mo.platform, mo.order_sn, mo.status, mo.buyer_name,
                  mo.buyer_phone, mo.ship_address,
                  mo.order_date, mo.settled_at, mo.item_total, mo.marketplace_fee,
                  mo.payout, mo.actual_payout, mo.settlement_source,
                  moi.doc_base AS matched_iv, moi.confidence AS iv_confidence,
                  moi.match_method AS iv_method
           FROM marketplace_orders mo
           LEFT JOIN marketplace_order_invoice moi
                  ON moi.platform = mo.platform AND moi.order_sn = mo.order_sn
           WHERE mo.id = ?""",
        (order_id,)
    ).fetchone()
    if o is None:
        return None
    items = conn.execute(
        """SELECT it.item_name, it.variation_name, it.seller_sku, it.qty,
                  it.unit_price, it.item_subtotal, it.internal_product_id,
                  p.product_name AS resolved_name
           FROM marketplace_order_items it
           LEFT JOIN products p ON p.id = it.internal_product_id
           WHERE it.order_id = ?
           ORDER BY it.id""",
        (order_id,)
    ).fetchall()
    fees = conn.execute(
        """SELECT item_value, fee_commission, fee_service, fee_transaction,
                  fee_platform, fee_ads_escrow, fee_tax, shipping_net, fee_saver,
                  fee_total, net_payout, fee_pct, fee_raw_json
           FROM marketplace_order_fees
           WHERE platform = ? AND order_sn = ?""",
        (o['platform'], o['order_sn'])).fetchone()
    payout = conn.execute(
        """SELECT p.deposit_date, p.amount FROM marketplace_payouts p
           JOIN marketplace_orders mo ON mo.payout_id = p.id
           WHERE mo.id = ?""", (order_id,)).fetchone()
    # Refunds / Seller-Balance adjustments booked against this order (txn_type
    # 'adjustment' only — income credits are the normal payout, not adjustments).
    adjustments = conn.execute(
        """SELECT txn_time, amount, description
           FROM marketplace_wallet_txns
           WHERE platform = ? AND order_sn = ? AND txn_type = 'adjustment'
           ORDER BY txn_time""", (o['platform'], o['order_sn'])).fetchall()
    fees_d = dict(fees) if fees else None
    # Build the breakdown from the raw lines (Lazada smart label), then drop the
    # raw row before returning — it must not leak to the client (Shopee's raw row
    # carries buyer name / order id / etc.).
    fee_lines = None
    if fees_d:
        fee_lines = _bucket_fee_lines(fees_d, fees_d.get('fee_raw_json'), o['platform'])
        fees_d.pop('fee_raw_json', None)
    return {'order': dict(o), 'items': [dict(r) for r in items],
            'fees': fees_d,
            # Same clean Thai-category breakdown as the settlement-page tooltip,
            # for the modal's ค่าธรรมเนียม hover. None when no Income breakdown.
            'fee_lines': fee_lines,
            'payout': dict(payout) if payout else None,
            'adjustments': [dict(a) for a in adjustments],
            'margin': get_order_margin(conn, order_id)}


def resolve_line_ratio(conn, platform, internal_product_id, variation_name=None):
    """Resolve qty_per_sale (how many base product units = 1 marketplace sale unit)
    for an order line. Order lines carry no variation_id, so we resolve from
    platform_skus by product, then disambiguate by variation_name.

    Returns (ratio, source):
        ('single')      product sold at one ratio across its listings → use it
        ('matched')     multi-ratio product, variation_name matched a listing
        (None,'ambiguous')  multi-ratio, no variation_name match → DON'T guess
        (None,'no_listing') product has no platform_skus row
    """
    rows = conn.execute(
        """SELECT variation_name, qty_per_sale FROM platform_skus
           WHERE platform = ? AND internal_product_id = ?""",
        (platform, internal_product_id)).fetchall()
    if not rows:
        return (None, 'no_listing')
    distinct = {r['qty_per_sale'] for r in rows}
    if len(distinct) == 1:
        return (rows[0]['qty_per_sale'], 'single')
    if variation_name:
        for r in rows:
            if r['variation_name'] == variation_name:
                return (r['qty_per_sale'], 'matched')
    return (None, 'ambiguous')


def get_order_margin(conn, order_id):
    """Contribution margin for one marketplace order = net payout − COGS.

    COGS = Σ over lines of cost_price × qty × ratio, with the per-line ratio
    resolved via resolve_line_ratio (the pack/โหล multiplier). net is the true
    received amount: settled net_payout (Income) else the wallet income credit
    else actual_payout.

    margin / margin_pct are None when ANY line's ratio is unresolved OR a line's
    product has no cost_price — reporting a partial total as if complete would
    mislead. The caller can still show the resolved `cogs` plus the `unresolved`
    / `cost_gap` counts and badge it "ไม่ครบ".
    """
    net = conn.execute(
        """SELECT COALESCE(
                    f.net_payout,
                    (SELECT SUM(w.amount) FROM marketplace_wallet_txns w
                      WHERE w.platform = mo.platform AND w.order_sn = mo.order_sn
                        AND w.txn_type = 'income'),
                    mo.actual_payout) AS net
           FROM marketplace_orders mo
           LEFT JOIN marketplace_order_fees f
                  ON f.platform = mo.platform AND f.order_sn = mo.order_sn
           WHERE mo.id = ?""", (order_id,)).fetchone()
    net_val = net['net'] if net else None

    lines = conn.execute(
        """SELECT it.internal_product_id, it.variation_name, it.qty, p.cost_price
           FROM marketplace_order_items it
           LEFT JOIN products p ON p.id = it.internal_product_id
           WHERE it.order_id = ?""", (order_id,)).fetchall()

    platform = conn.execute(
        "SELECT platform FROM marketplace_orders WHERE id = ?", (order_id,)).fetchone()
    platform = platform['platform'] if platform else 'shopee'

    cogs = 0.0
    unresolved = 0
    cost_gap = 0
    out_lines = []
    for ln in lines:
        pid = ln['internal_product_id']
        ratio = source = None
        line_cogs = None
        if pid is None:
            unresolved += 1                 # unmapped line: can't cost it
            source = 'unmapped'
        else:
            ratio, source = resolve_line_ratio(conn, platform, pid, ln['variation_name'])
            if ratio is None:
                unresolved += 1
            elif ln['cost_price'] is None or ln['cost_price'] == 0:
                cost_gap += 1
            else:
                line_cogs = round(ln['cost_price'] * (ln['qty'] or 0) * ratio, 2)
                cogs += line_cogs
        out_lines.append({'product_id': pid, 'variation_name': ln['variation_name'],
                          'qty': ln['qty'], 'cost_price': ln['cost_price'],
                          'ratio': ratio, 'ratio_source': source, 'line_cogs': line_cogs})

    cogs = round(cogs, 2)
    complete = unresolved == 0 and cost_gap == 0 and net_val is not None
    margin = round(net_val - cogs, 2) if complete else None
    margin_pct = (round(margin / net_val * 100, 1)
                  if complete and net_val else None)
    return {'net': net_val, 'cogs': cogs, 'margin': margin, 'margin_pct': margin_pct,
            'unresolved': unresolved, 'cost_gap': cost_gap, 'lines': out_lines}


# Customer NAME (not code) per platform, for the payments-received lookup.
_RECON_CUSTOMER = {'shopee': 'หน้าร้านS', 'lazada': 'หน้าร้านL'}


def get_marketplace_reconciliation(conn, platform='shopee'):
    """Reconcile, per settlement month, three numbers for the หน้าร้าน B2C books:

        Shopee payout (actual_payout)  ↔  matched IV billed  ↔  รับชำระหนี้ (collected)

    The team books each order as one IV at the net payout, then records รับชำระหนี้
    when the marketplace settles — so ideally payout == billed == collected. This
    surfaces where they diverge (timing across months, cancellations, fee gaps) and
    lists orders with no IV link and IVs with no order link.

    Returns {months, unmatched_orders, unmatched_ivs, summary}.
    """
    import payments_alloc
    from collections import OrderedDict

    cust_name = _RECON_CUSTOMER.get(platform, 'หน้าร้านS')
    settle = {r['doc_base']: r
              for r in payments_alloc.invoice_settlement(customer=cust_name, conn=conn)}
    # Manager acknowledgements of billed≠payout discrepancies (survive re-matching).
    reviews = {r['order_sn']: r for r in conn.execute(
        "SELECT order_sn, doc_base, d_bill, reviewed_by FROM marketplace_amount_review WHERE platform=?",
        (platform,))}

    rows = conn.execute(
        """SELECT mo.id, mo.order_sn, mo.settled_at, mo.actual_payout,
                  moi.doc_base AS iv, moi.confidence AS iv_confidence,
                  CASE WHEN mo.platform='lazada'
                       THEN COALESCE(f.item_value, mo.item_total, mo.actual_payout)
                       ELSE mo.actual_payout END AS billed_basis
           FROM marketplace_orders mo
           LEFT JOIN marketplace_order_fees f
                  ON f.platform=mo.platform AND f.order_sn=mo.order_sn
           LEFT JOIN marketplace_order_invoice moi
                  ON moi.platform = mo.platform AND moi.order_sn = mo.order_sn
           WHERE mo.platform = ? AND mo.settled_at IS NOT NULL
           ORDER BY mo.settled_at DESC, mo.order_date""",
        (platform,)
    ).fetchall()

    months = OrderedDict()
    matched_ivs = set()
    unmatched_orders = []
    s_payout = s_billed = s_collected = 0.0
    n_amount_mismatch = 0
    n_reviewed = 0
    amount_mismatch_total = 0.0

    for r in rows:
        ym = (r['settled_at'] or '')[:7]
        payout = round(r['actual_payout'] or 0, 2)          # net — shown as "ยอดโอนจริง" (info)
        basis  = round(r['billed_basis'] or 0, 2)           # what the IV should equal
        iv = r['iv']
        s = settle.get(iv) if iv else None
        billed = round(s['billed'], 2) if s else None
        collected = round(s['collected'], 2) if s else None
        if iv:
            matched_ivs.add(iv)
        # billed − basis: IV amount vs what the team should have keyed (gross for Lazada, net for Shopee).
        d_bill = round(billed - basis, 2) if billed is not None else None
        amount_mismatch = d_bill is not None and abs(d_bill) >= 0.01
        # A manager acknowledgement only counts if it still matches this exact
        # invoice + discrepancy (else the situation changed → re-flag).
        rv = reviews.get(r['order_sn'])
        reviewed = bool(amount_mismatch and rv and rv['doc_base'] == iv
                        and abs((rv['d_bill'] or 0) - (d_bill or 0)) < 0.01)
        if amount_mismatch:
            n_amount_mismatch += 1
            amount_mismatch_total += abs(d_bill)
            if reviewed:
                n_reviewed += 1
        row = {
            'order_id': r['id'], 'order_sn': r['order_sn'], 'settled_at': r['settled_at'],
            'payout': payout, 'basis': basis, 'iv': iv, 'iv_confidence': r['iv_confidence'],
            'billed': billed, 'collected': collected,
            'd_bill': d_bill,
            'amount_mismatch': amount_mismatch,
            'reviewed': reviewed,
            'reviewed_by': rv['reviewed_by'] if reviewed else None,
            'd_coll': round(payout - collected, 2) if collected is not None else None,
            'ok': (billed is not None and collected is not None
                   and abs(basis - billed) < 0.01 and abs(payout - collected) < 0.01),
        }
        m = months.setdefault(ym, {'ym': ym, 'orders': [], 'payout': 0.0,
                                   'billed': 0.0, 'collected': 0.0, 'n_unmatched': 0})
        m['orders'].append(row)
        m['payout'] += payout
        if billed is not None:
            m['billed'] += billed
        if collected is not None:
            m['collected'] += collected
        if iv is None:
            m['n_unmatched'] += 1
            unmatched_orders.append(row)
        s_payout += payout
        if billed is not None:
            s_billed += billed
        if collected is not None:
            s_collected += collected

    # IVs booked in the same months as our settled orders but linked to no order.
    order_months = set(months.keys())
    unmatched_ivs = [
        {'doc_base': doc, 'invoice_date': s['invoice_date'],
         'billed': round(s['billed'], 2), 'collected': round(s['collected'], 2)}
        for doc, s in settle.items()
        if doc not in matched_ivs and (s['invoice_date'] or '')[:7] in order_months
    ]
    unmatched_ivs.sort(key=lambda x: (x['invoice_date'] or ''), reverse=True)

    for m in months.values():
        m['payout'] = round(m['payout'], 2)
        m['billed'] = round(m['billed'], 2)
        m['collected'] = round(m['collected'], 2)

    return {
        'months': list(months.values()),
        'unmatched_orders': unmatched_orders,
        'unmatched_ivs': unmatched_ivs,
        'summary': {
            'orders': len(rows),
            'matched': len(rows) - len(unmatched_orders),
            'unmatched_orders': len(unmatched_orders),
            'unmatched_ivs': len(unmatched_ivs),
            'payout': round(s_payout, 2),
            'billed': round(s_billed, 2),
            'collected': round(s_collected, 2),
            'amount_mismatch': n_amount_mismatch,
            'amount_mismatch_total': round(amount_mismatch_total, 2),
            'amount_mismatch_reviewed': n_reviewed,
            'amount_mismatch_open': n_amount_mismatch - n_reviewed,
        },
    }


def set_amount_review(conn, order_id, accept, reviewed_by=None):
    """Manager acknowledges (accept=True) or un-acknowledges (False) an order's
    billed≠payout discrepancy. Stores the current invoice + d_bill so the
    acknowledgement auto-invalidates if the match or amount later changes.
    Returns {'accepted': True} / {'cleared': True} / None if the order has no match."""
    o = conn.execute(
        """SELECT mo.order_sn, mo.platform, mo.actual_payout, moi.doc_base
           FROM marketplace_orders mo
           JOIN marketplace_order_invoice moi
             ON moi.platform = mo.platform AND moi.order_sn = mo.order_sn
           WHERE mo.id = ?""",
        (order_id,)
    ).fetchone()
    if o is None:
        return None
    if not accept:
        conn.execute(
            "DELETE FROM marketplace_amount_review WHERE platform=? AND order_sn=?",
            (o['platform'], o['order_sn']))
        conn.commit()
        return {'cleared': True}
    billed = conn.execute(
        """SELECT ROUND(SUM(CASE WHEN vat_type=2 THEN net*1.07 ELSE net END), 2) AS b
           FROM sales_transactions WHERE doc_base = ?""",
        (o['doc_base'],)
    ).fetchone()['b'] or 0.0
    d_bill = round(billed - round(o['actual_payout'] or 0, 2), 2)
    conn.execute(
        """INSERT INTO marketplace_amount_review
               (platform, order_sn, doc_base, d_bill, reviewed_by)
           VALUES (?,?,?,?,?)
           ON CONFLICT(platform, order_sn) DO UPDATE SET
               doc_base    = excluded.doc_base,
               d_bill      = excluded.d_bill,
               reviewed_by = excluded.reviewed_by,
               reviewed_at = datetime('now','localtime')""",
        (o['platform'], o['order_sn'], o['doc_base'], d_bill, reviewed_by))
    conn.commit()
    return {'accepted': True, 'd_bill': d_bill}


def get_marketplace_summary():
    """Per-platform counts for the dashboard cards."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT platform,
                      COUNT(*)                           AS orders,
                      COALESCE(SUM(item_total), 0)       AS gmv,
                      MAX(last_synced_at)                AS last_import
                 FROM marketplace_orders GROUP BY platform""").fetchall()
        summary = {r['platform']: dict(r) for r in rows}
        unmapped = conn.execute(
            """SELECT COUNT(*) FROM marketplace_order_items
                WHERE internal_product_id IS NULL""").fetchone()[0]
        summary['_unmapped_lines'] = unmapped
        return summary
    finally:
        conn.close()


def get_marketplace_orders(platform=None, limit=500):
    """Order headers (newest first) with per-order line rollups, for the table."""
    conn = get_connection()
    try:
        sql = """
            SELECT o.*,
                   (SELECT COUNT(*) FROM marketplace_order_items i WHERE i.order_id=o.id) AS n_items,
                   (SELECT COALESCE(SUM(qty),0) FROM marketplace_order_items i WHERE i.order_id=o.id) AS total_qty,
                   (SELECT COUNT(*) FROM marketplace_order_items i
                     WHERE i.order_id=o.id AND i.internal_product_id IS NULL) AS n_unmapped
              FROM marketplace_orders o"""
        params = []
        if platform in ('shopee', 'lazada'):
            sql += " WHERE o.platform = ?"
            params.append(platform)
        sql += " ORDER BY o.order_date DESC, o.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_marketplace_unmapped():
    """Distinct unmapped order lines (need a platform_skus mapping), with how
    many order lines each represents."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT platform, item_name, variation_name,
                      MAX(seller_sku)    AS seller_sku,
                      MAX(variation_id)  AS variation_id,
                      COUNT(*)           AS line_count,
                      COALESCE(SUM(qty), 0) AS total_qty
                 FROM marketplace_order_items
                WHERE internal_product_id IS NULL
                GROUP BY platform, item_name, variation_name
                ORDER BY line_count DESC""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# status values that mean an order was cancelled or returned (no real sale)
_CANCEL_RETURN_STATUSES = (
    'canceled', 'ยกเลิกแล้ว', 'Package Returned', 'returned',
    'Package scrapped', 'Lost by 3PL', 'In Transit: Returning to seller',
)


def get_marketplace_returns_cancelled():
    """Orders that are NOT real sales — excluded from margin analysis.

    return  = settlement net_payout < 0 (buyer refunded; seller ate two-way
              shipping). These can show status 'completed' yet be a financial
              loss, so they are detected by net_payout, not status.
    cancel  = order status in the cancel/return set (never paid out).
    """
    conn = get_connection()
    try:
        ph = ','.join('?' * len(_CANCEL_RETURN_STATUSES))
        returns = [dict(r) for r in conn.execute(
            """SELECT f.platform, f.order_sn, substr(o.order_date, 1, 10) AS date,
                      o.status, f.item_value, f.net_payout, f.shipping_net,
                      GROUP_CONCAT(DISTINCT COALESCE(p.product_name, oi.item_name)) AS product
                 FROM marketplace_order_fees f
                 JOIN marketplace_orders o
                   ON o.platform = f.platform AND o.order_sn = f.order_sn
                 LEFT JOIN marketplace_order_items oi
                   ON oi.platform = f.platform AND oi.order_sn = f.order_sn
                 LEFT JOIN products p ON p.id = oi.internal_product_id
                WHERE f.net_payout < 0
                GROUP BY f.platform, f.order_sn
                ORDER BY f.net_payout""").fetchall()]
        cancelled = [dict(r) for r in conn.execute(
            f"""SELECT o.platform, o.order_sn, substr(o.order_date, 1, 10) AS date,
                       o.status, o.item_total,
                       GROUP_CONCAT(DISTINCT COALESCE(p.product_name, oi.item_name)) AS product
                  FROM marketplace_orders o
                  LEFT JOIN marketplace_order_items oi ON oi.order_id = o.id
                  LEFT JOIN products p ON p.id = oi.internal_product_id
                 WHERE o.status IN ({ph})
                 GROUP BY o.id
                 ORDER BY o.order_date DESC""", _CANCEL_RETURN_STATUSES).fetchall()]
        total_orders = conn.execute(
            "SELECT COUNT(*) FROM marketplace_orders").fetchone()[0] or 0
        return_loss = sum((r['net_payout'] or 0) for r in returns)
        return {
            'returns': returns,
            'cancelled': cancelled,
            'n_returns': len(returns),
            'n_cancelled': len(cancelled),
            'return_loss': return_loss,
            'total_orders': total_orders,
            'return_rate': (len(returns) / total_orders * 100) if total_orders else 0,
            'cancel_rate': (len(cancelled) / total_orders * 100) if total_orders else 0,
        }
    finally:
        conn.close()


# ── Payout batch functions (mig 105) ─────────────────────────────────────────

def create_payout_batch(deposit_date, deposit_amount, bank_ref=None, note=None,
                        created_by=None, conn=None):
    """Insert a new payout_batches row and return its id.

    Args:
        deposit_date:   ISO date string, e.g. '2026-06-06'.
        deposit_amount: The bank-transfer amount (฿).
        bank_ref:       Optional bank reference string.
        note:           Optional free-text note.
        created_by:     Username of the creator.
        conn:           DB connection (callers that manage their own connection
                        pass it here; routes that don't pass None → opens one).
    Returns:
        int: the new batch id.
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO payout_batches
                   (deposit_date, deposit_amount, bank_ref, note, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (deposit_date, deposit_amount, bank_ref, note, created_by),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if _own_conn:
            conn.close()


_BATCH_TOLERANCE = 0.005  # ฿ tolerance for floating-point prefix-sum comparison


def match_orders_to_amount(deposit_amount, conn=None):
    """Pure dry-run greedy prefix matcher — reads only, writes nothing.

    Candidate set = Shopee marketplace_orders with
        actual_payout IS NOT NULL AND payout_batch_id IS NULL
    ordered by settled_at ASC, order_sn ASC (deterministic tiebreak).

    Returns:
        {'status':'matched', 'order_ids':[...], 'n':N, 'sum':S}
        OR
        {'status':'no_exact_match', 'candidates':[{order_sn,settled_at,
            actual_payout,running_sum}], 'closest_n':N, 'closest_sum':S}

    Callers that want to commit must create a batch row first, then call
    assign_orders_to_batch (which reuses this logic with a write step), or
    call this function and handle the result themselves.

    Negative actual_payout (refund netting) is handled naturally.
    closest_sum tracks the prefix with the smallest absolute distance to the
    target (it may overshoot or undershoot — it is diagnostic only).
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        candidates = conn.execute(
            """SELECT id, order_sn, settled_at, actual_payout
               FROM marketplace_orders
               WHERE platform = 'shopee'
                 AND actual_payout IS NOT NULL
                 AND payout_batch_id IS NULL
               ORDER BY settled_at ASC, order_sn ASC"""
        ).fetchall()

        target = round(deposit_amount, 2)
        running = 0.0
        match_ids = []
        closest_n = 0
        closest_sum = 0.0

        for row in candidates:
            running = round(running + (row[3] or 0), 2)
            match_ids.append(row[0])

            if abs(running - target) <= _BATCH_TOLERANCE:
                return {
                    'status': 'matched',
                    'order_ids': list(match_ids),
                    'n': len(match_ids),
                    'sum': running,
                }

            # Track the prefix with smallest absolute distance to target
            # (may overshoot or undershoot — diagnostic only).
            if abs(running - target) < abs(closest_sum - target):
                closest_n = len(match_ids)
                closest_sum = running

        # No exact prefix — build full candidate list with running sums.
        running2 = 0.0
        cand_list = []
        for row in candidates:
            running2 = round(running2 + (row[3] or 0), 2)
            cand_list.append({
                'order_sn':      row[1],
                'settled_at':    row[2],
                'actual_payout': row[3],
                'running_sum':   running2,
            })

        return {
            'status':       'no_exact_match',
            'candidates':   cand_list,
            'closest_n':    closest_n,
            'closest_sum':  closest_sum,
        }
    finally:
        if _own_conn:
            conn.close()


def assign_orders_to_batch(batch_id, deposit_amount, conn=None):
    """Greedy prefix matcher that commits on an exact hit.

    Delegates the pure matching logic to match_orders_to_amount, then writes
    payout_batch_id to the matched rows if an exact prefix is found.

    Returns the same dict shape as match_orders_to_amount so all existing
    callers/tests continue to work unchanged.
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        result = match_orders_to_amount(deposit_amount, conn=conn)
        if result['status'] == 'matched':
            ids = result['order_ids']
            placeholders = ','.join('?' for _ in ids)
            conn.execute(
                f"UPDATE marketplace_orders SET payout_batch_id = ? WHERE id IN ({placeholders})",
                [batch_id] + ids,
            )
            conn.commit()
        return result
    finally:
        if _own_conn:
            conn.close()


def assign_orders_manual(batch_id, order_sns, conn=None):
    """Assign an explicit list of order_sns to the batch (manual-adjust path).

    Idempotent: re-running with the same order_sns simply sets payout_batch_id
    again (no harm; no duplicate).  Only Shopee orders are targeted.
    """
    if not order_sns:
        return
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        placeholders = ','.join('?' for _ in order_sns)
        conn.execute(
            f"""UPDATE marketplace_orders
                   SET payout_batch_id = ?
                 WHERE platform = 'shopee' AND order_sn IN ({placeholders})""",
            [batch_id] + list(order_sns),
        )
        conn.commit()
    finally:
        if _own_conn:
            conn.close()


def get_deposit_batch_report(conn=None):
    """Return all payout batches with per-batch stats and an unbatched bucket.

    Returns:
        {
          'batches': [
            {
              'id': int,
              'deposit_date': str,
              'deposit_amount': float,
              'bank_ref': str | None,
              'note': str | None,
              'created_at': str,
              'order_count': int,
              'sum_payout': float,
              'tied': bool,      # True when sum_payout == deposit_amount (±tolerance)
              'orders': [{order_sn, settled_at, actual_payout}]
            }, ...
          ],
          'unbatched': [{order_sn, settled_at, actual_payout}]
              # Shopee orders with actual_payout NOT NULL but payout_batch_id IS NULL
        }
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        # Guard: mig 105 may not yet be applied on older DBs.
        _has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payout_batches'"
        ).fetchone()
        if not _has_table:
            return {'batches': [], 'unbatched': []}

        batch_rows = conn.execute(
            """SELECT pb.id, pb.deposit_date, pb.deposit_amount, pb.bank_ref,
                      pb.note, pb.created_at, pb.is_baseline,
                      COUNT(mo.id)            AS order_count,
                      COALESCE(SUM(mo.actual_payout), 0) AS sum_payout
               FROM payout_batches pb
               LEFT JOIN marketplace_orders mo
                      ON mo.payout_batch_id = pb.id AND mo.platform = 'shopee'
               GROUP BY pb.id
               ORDER BY pb.deposit_date DESC, pb.id DESC"""
        ).fetchall()

        batches = []
        for b in batch_rows:
            bid, dep_date, dep_amt, bank_ref, note, created_at, is_baseline, cnt, s_pay = b
            s_pay = round(s_pay or 0, 2)
            dep_amt_r = round(dep_amt or 0, 2)
            tied = abs(s_pay - dep_amt_r) <= _BATCH_TOLERANCE

            orders = conn.execute(
                """SELECT order_sn, settled_at, actual_payout
                   FROM marketplace_orders
                   WHERE payout_batch_id = ? AND platform = 'shopee'
                   ORDER BY settled_at ASC, order_sn ASC""",
                (bid,)
            ).fetchall()

            batches.append({
                'id':             bid,
                'deposit_date':   dep_date,
                'deposit_amount': dep_amt,
                'bank_ref':       bank_ref,
                'note':           note,
                'created_at':     created_at,
                'is_baseline':    bool(is_baseline),
                'order_count':    cnt,
                'sum_payout':     s_pay,
                'tied':           tied,
                'orders':         [{'order_sn': r[0], 'settled_at': r[1],
                                    'actual_payout': r[2]} for r in orders],
            })

        unbatched_rows = conn.execute(
            """SELECT order_sn, settled_at, actual_payout
               FROM marketplace_orders
               WHERE platform = 'shopee'
                 AND actual_payout IS NOT NULL
                 AND payout_batch_id IS NULL
               ORDER BY settled_at ASC, order_sn ASC"""
        ).fetchall()
        unbatched = [{'order_sn': r[0], 'settled_at': r[1], 'actual_payout': r[2]}
                     for r in unbatched_rows]

        return {'batches': batches, 'unbatched': unbatched}
    finally:
        if _own_conn:
            conn.close()


def create_baseline_batch(cutoff_date, created_by=None, conn=None):
    """Absorb all pre-tracking settled orders into a single "ยอดยกมา" batch.

    Inserts a payout_batches row with is_baseline=1 and deposit_amount equal to
    the sum of the absorbed orders. Assigns payout_batch_id to every Shopee order
    with actual_payout IS NOT NULL AND payout_batch_id IS NULL AND
    settled_at <= cutoff_date.

    This is a one-time operation to clear the historical backlog so the greedy
    matcher only sees post-cutoff orders when matching real bank deposits.
    Deleting the baseline (via unassign_batch) frees the orders back to unbatched.

    Returns:
        {'batch_id': int, 'n_absorbed': int, 'sum_absorbed': float}
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        # Calculate sum and count of orders to absorb before inserting the batch.
        agg = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(actual_payout), 0)
               FROM marketplace_orders
               WHERE platform = 'shopee'
                 AND actual_payout IS NOT NULL
                 AND payout_batch_id IS NULL
                 AND settled_at <= ?""",
            (cutoff_date,)
        ).fetchone()
        n_absorbed = agg[0]
        sum_absorbed = round(agg[1] or 0, 2)

        cur = conn.execute(
            """INSERT INTO payout_batches
                   (deposit_date, deposit_amount, bank_ref, note, created_by, is_baseline)
               VALUES (?, ?, NULL, 'ยอดยกมา (โอนเข้าบัญชีก่อนเริ่มบันทึก)', ?, 1)""",
            (cutoff_date, sum_absorbed, created_by),
        )
        batch_id = cur.lastrowid

        conn.execute(
            """UPDATE marketplace_orders
                  SET payout_batch_id = ?
                WHERE platform = 'shopee'
                  AND actual_payout IS NOT NULL
                  AND payout_batch_id IS NULL
                  AND settled_at <= ?""",
            (batch_id, cutoff_date),
        )
        conn.commit()
        return {'batch_id': batch_id, 'n_absorbed': n_absorbed, 'sum_absorbed': sum_absorbed}
    finally:
        if _own_conn:
            conn.close()


def unassign_batch(batch_id, conn=None):
    """Clear payout_batch_id on all orders in the batch, then delete the batch.

    Idempotent: if the batch_id doesn't exist, this is a no-op.
    """
    _own_conn = conn is None
    if _own_conn:
        from database import get_connection
        conn = get_connection()
    try:
        conn.execute(
            "UPDATE marketplace_orders SET payout_batch_id = NULL WHERE payout_batch_id = ?",
            (batch_id,),
        )
        conn.execute("DELETE FROM payout_batches WHERE id = ?", (batch_id,))
        conn.commit()
    finally:
        if _own_conn:
            conn.close()

