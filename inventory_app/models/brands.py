"""Brands — extracted verbatim from models.py (behavior-preserving split,
Phase 11) — see models/__init__.py's module docstring for the overall
file-split rationale. No behavior changes.
"""
from database import get_connection


def get_brands():
    """All brands sorted: own brands first (sort_order, then name)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, code, name, name_th, is_own_brand, sort_order
          FROM brands
         ORDER BY is_own_brand DESC, sort_order, name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_brand(brand_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_product_brand(product_id, brand_id):
    """Assign (or clear) a brand on a product. Pass None to clear.

    Side effects to keep commission state consistent:
      1. express_sales.brand_kind was removed in mig 068. Brand-kind is now
         derived at read time in commission._BASE_QUERY's CASE expression
         from brands.is_own_brand. No cache, no trigger, no drift risk.
         The trigger refresh_brand_kind_on_product_brand_change (mig 063)
         was also removed in mig 068 — no in-DB side effect fires on
         brand_id UPDATE after that migration.
      2. Top-up auto-pay for pre-2026-02 invoices that include this
         product and whose commission_due just changed (e.g. third → own
         flips a 5% line to 10% — without a top-up the invoice would
         resurface as 'partial').
    """
    conn = get_connection()
    conn.execute("UPDATE products SET brand_id = ? WHERE id = ?",
                 (brand_id, product_id))
    conn.commit()
    conn.close()

    # Top up auto-pay for any pre-Feb-2026 invoices touched by this
    # product. Imported lazily so models.py stays usable in non-Flask
    # contexts (CLI scripts, tests).
    try:
        import commission as _commission
        _commission.clear_override_cache()
        _topup_pre_feb_for_product(product_id, _commission)
    except Exception as e:
        # don't break the brand UPDATE if the top-up fails for any
        # reason — log and move on
        import sys
        print(f'[set_product_brand] warning: top-up failed: {e}', file=sys.stderr)


def _topup_pre_feb_for_product(product_id, commission_mod, cutoff='2026-02-01'):
    """For every pre-cutoff invoice that includes this product, recompute
    commission and insert payout rows for any new shortfall. Marker:
    note='pre-Feb 2026 auto-paid (top-up after brand change)'.
    """
    conn = get_connection()
    # cheap guard: nothing to do if this product has no mapping at all
    if conn.execute(
        'SELECT 1 FROM product_code_mapping WHERE product_id = ? LIMIT 1',
        (product_id,)
    ).fetchone() is None:
        conn.close()
        return
    # Unit-aware (mig 061/063): a split bsn_code resolves to DIFFERENT
    # products by express_sales.unit. Selecting invoices by
    # `es.product_code IN (codes)` would auto-pay invoices that only
    # contain ANOTHER product sharing the code. Match each es row through
    # the SAME resolver the trigger/import use, so only invoices whose
    # (product_code, unit) actually resolves to THIS product are topped up.
    # (Codex adversarial review high finding, 2026-05-20.)
    triples = conn.execute("""
        SELECT DISTINCT pin.salesperson_code,
                        substr(pin.date_iso, 1, 7) AS ym,
                        ref.invoice_no
          FROM express_payments_in pin
          JOIN express_payment_in_invoice_refs ref ON ref.payment_in_id = pin.id
          JOIN express_sales es ON es.doc_no = ref.invoice_no
         WHERE pin.is_void = 0
           AND pin.salesperson_code <> ''
           AND es.date_iso < ?
           AND ? = (
               SELECT m.product_id
                 FROM product_code_mapping m
                WHERE m.bsn_code = es.product_code
                  AND m.product_id IS NOT NULL
                LIMIT 1
           )
    """, (cutoff, product_id)).fetchall()
    conn.close()

    inserted = 0
    for sp, ym, inv_no in triples:
        invs = commission_mod.get_invoice_commission_for_sp(ym, sp)
        for inv in invs:
            if inv['invoice_no'] != inv_no:
                continue
            if inv['remaining'] > 0.05:
                commission_mod.record_payout(
                    year_month=ym, salesperson_code=sp,
                    amount_paid=inv['remaining'], paid_date='2026-02-01',
                    paid_method='auto', paid_by='system',
                    note='pre-Feb 2026 auto-paid (top-up after brand change)',
                    invoice_no=inv_no,
                )
                inserted += 1
            break
    if inserted:
        print(f'[set_product_brand] topped up {inserted} pre-Feb payouts for product {product_id}')


def create_brand(name, name_th=None, is_own=False):
    """Create a new brand row. `code` derived from name (lowercased, words
    joined by '_'). Returns the new brand id.
    Raises ValueError if `name` is empty or `code` already exists.
    """
    if not name or not name.strip():
        raise ValueError('ชื่อแบรนด์ว่างเปล่า')
    name = name.strip()
    # generate slug-ish code from name
    import re as _re
    code_base = _re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    if not code_base:
        # fall back to a numeric suffix from the next id
        code_base = 'brand'
    conn = get_connection()
    code = code_base
    n = 2
    while conn.execute('SELECT 1 FROM brands WHERE code = ?', (code,)).fetchone():
        code = f'{code_base}_{n}'
        n += 1
    cur = conn.execute("""
        INSERT INTO brands (code, name, name_th, is_own_brand, sort_order)
        VALUES (?, ?, ?, ?, 100)
    """, (code, name, (name_th or '').strip() or None, 1 if is_own else 0))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id
