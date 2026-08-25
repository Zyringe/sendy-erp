"""Brands — extracted verbatim from models.py (behavior-preserving split,
Phase 11) — see models/__init__.py's module docstring for the overall
file-split rationale. No behavior changes.
"""
from database import get_connection


def get_brands(conn=None):
    """All brands sorted: own brands first (sort_order, then name)."""
    owned = conn is None
    if owned:
        conn = get_connection()
    rows = conn.execute("""
        SELECT id, code, name, name_th, is_own_brand, sort_order
          FROM brands
         ORDER BY is_own_brand DESC, sort_order, name
    """).fetchall()
    if owned:
        conn.close()
    return [dict(r) for r in rows]


def get_brand(brand_id, conn=None):
    owned = conn is None
    if owned:
        conn = get_connection()
    row = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if owned:
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


def derive_brand_short_code(name: str) -> str:
    """Default `brands.short_code` proposed for a brand the operator is
    creating inline. UPPERCASE, ASCII-alnum only, capped at 6 — matching the
    shape of every short_code already in use (SD, GL, CHG, ALTECO, 4STAR).

    This is only ever a DEFAULT shown in the form for the human to accept or
    replace. It is deliberately not applied silently: short_code is a segment
    of every `sku_code` in the brand, so a wrong guess that nobody saw becomes
    a rename debt across the whole brand (see the sku_code warning in
    `naming_cascade.save_product`). A name with no ASCII at all (a purely Thai
    brand) yields '' — the caller must then ask rather than invent one.
    """
    import re as _re
    ascii_only = _re.sub(r'[^A-Za-z0-9]+', '', (name or ''))
    return ascii_only.upper()[:6]


def upsert_brand(conn, name, *, name_th=None, short_code=None, is_own=False):
    """Resolve a typed brand name to a brand id, creating the row if new.

    ONE creation path for both entry points (`/products/new`'s hand form via
    `create_brand` below, and `create_structured_product`'s inline
    `brand_other_name`). They used to disagree in two ways that both showed up
    in live data:

      * the inline path wrote `name_th = name`, so an English-only brand
        rendered as "SONAX / SONAX" in every picker (mapping.html builds the
        label as `name ~ ' / ' ~ name_th`). `name_th` is now NULL unless a real
        Thai name is supplied — matching the 56 of 76 brands that already have
        it empty.
      * neither path set `short_code`, which is a SEGMENT OF `sku_code`. SONAX
        was the only brand of 76 missing one, and its five products lost the
        brand segment from their codes.

    Reuses an existing brand when the trimmed name matches case-insensitively,
    rather than minting a second row under a suffixed code — `create_brand`
    used to produce `sonax_2` alongside `sonax`, i.e. a duplicate brand with an
    identical display name. Does NOT commit: the caller owns the transaction.
    """
    if not name or not name.strip():
        raise ValueError('ชื่อแบรนด์ว่างเปล่า')
    name = name.strip()

    # ORDER BY id: if two legacy rows already share a display name (none do on
    # prod today, but the constraint is on `code`, not `name`), reuse must be
    # DETERMINISTIC — an unordered LIMIT-less query lets SQLite hand back
    # whichever row it likes, so the same typed name could attach a different
    # brand_id, sku segment and own-brand flag on different days. Oldest wins
    # (Codex review 2026-08-25).
    existing = conn.execute(
        'SELECT id FROM brands WHERE lower(trim(name)) = lower(?) '
        'ORDER BY id LIMIT 1', (name,)
    ).fetchone()
    if existing:
        return existing['id'] if hasattr(existing, 'keys') else existing[0]

    import re as _re
    code_base = _re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or 'brand'
    code = code_base
    n = 2
    while conn.execute('SELECT 1 FROM brands WHERE code = ?', (code,)).fetchone():
        code = f'{code_base}_{n}'
        n += 1

    cur = conn.execute("""
        INSERT INTO brands (code, name, name_th, short_code, is_own_brand, sort_order)
        VALUES (?, ?, ?, ?, ?, 100)
    """, (code, name,
          (name_th or '').strip() or None,
          (short_code or '').strip().upper() or None,
          1 if is_own else 0))
    return cur.lastrowid


def create_brand(name, name_th=None, is_own=False, short_code=None):
    """Create (or reuse) a brand row and commit. Returns the brand id.
    Thin owning-connection wrapper around `upsert_brand`.
    Raises ValueError if `name` is empty.
    """
    conn = get_connection()
    try:
        new_id = upsert_brand(conn, name, name_th=name_th,
                              short_code=short_code, is_own=is_own)
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
