import re
import sqlite3

from database import get_connection
import bsn_units
from collections import defaultdict
from datetime import date


# ── Unit conversion ──────────────────────────────────────────────────────────

def to_base_units(quantity: int, mode: str, product) -> int:
    if mode == 'carton':
        return quantity * (product['units_per_carton'] or 1)
    if mode == 'box':
        return quantity * (product['units_per_box'] or 1)
    return quantity


# ── Products ─────────────────────────────────────────────────────────────────

def get_products(search=None, low_stock=False, hard_to_sell=False,
                 location=None, in_stock=False, page=1, per_page=50):
    conn = get_connection()
    conditions = ["p.is_active = 1"]
    params = []
    if search:
        conditions.append("(p.product_name LIKE ? OR CAST(p.sku AS TEXT) LIKE ? OR p.sku_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if hard_to_sell:
        conditions.append("p.hard_to_sell = 1")
    if location:
        conditions.append(
            "EXISTS (SELECT 1 FROM product_locations pl"
            " WHERE pl.product_id = p.id AND pl.floor_no LIKE ?)"
        )
        params.append(f"%{location}%")

    where = " AND ".join(conditions)
    having_clauses = []
    if low_stock:
        having_clauses.append("COALESCE(s.quantity, 0) <= p.low_stock_threshold")
    if in_stock:
        having_clauses.append("COALESCE(s.quantity, 0) > 0")
    having = ("HAVING " + " AND ".join(having_clauses)) if having_clauses else ""

    sql = f"""
        SELECT p.id, p.sku, p.sku_code, p.product_name, p.units_per_carton, p.units_per_box,
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
        ORDER BY p.sku
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
        SELECT p.id, p.sku, p.sku_code, p.sku_code_locked,
               p.product_name, p.units_per_carton, p.units_per_box,
               p.unit_type, p.hard_to_sell, p.cost_price, p.base_sell_price,
               p.low_stock_threshold, p.is_active, p.brand_id, p.category_id,
               p.sub_category, p.series, p.model, p.size,
               p.color_code, p.packaging, p.condition, p.pack_variant,
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


def get_product_by_sku(sku):
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    conn.close()
    return row


def create_product(data: dict) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO products (sku, product_name, units_per_carton, units_per_box,
            unit_type, hard_to_sell, cost_price, base_sell_price, low_stock_threshold,
            shopee_stock, lazada_stock)
        VALUES (:sku, :product_name, :units_per_carton, :units_per_box,
            :unit_type, :hard_to_sell, :cost_price, :base_sell_price, :low_stock_threshold,
            :shopee_stock, :lazada_stock)
    """, data)
    # ensure stock_levels row exists
    conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (cur.lastrowid,))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def update_product(product_id: int, data: dict):
    conn = get_connection()
    conn.execute("""
        UPDATE products SET
            sku=:sku, product_name=:product_name,
            units_per_carton=:units_per_carton, units_per_box=:units_per_box,
            unit_type=:unit_type, hard_to_sell=:hard_to_sell,
            cost_price=:cost_price, base_sell_price=:base_sell_price,
            low_stock_threshold=:low_stock_threshold,
            shopee_stock=:shopee_stock, lazada_stock=:lazada_stock
        WHERE id=:id
    """, {**data, 'id': product_id})
    conn.commit()
    conn.close()


def deactivate_product(product_id: int):
    conn = get_connection()
    conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()


# ── Brands ──────────────────────────────────────────────────────────────────
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
      1. Refresh express_sales.brand_kind for any rows whose product_code
         maps to this product (so the commission engine sees the new
         classification immediately).
      2. Top-up auto-pay for pre-2026-02 invoices that include this
         product and whose commission_due just changed (e.g. third → own
         flips a 5% line to 10% — without a top-up the invoice would
         resurface as 'partial').
    """
    conn = get_connection()
    conn.execute("UPDATE products SET brand_id = ? WHERE id = ?",
                 (brand_id, product_id))
    conn.execute("""
        UPDATE express_sales
           SET brand_kind = (
               SELECT CASE WHEN b.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
                 FROM brands b WHERE b.id = ?
           )
         WHERE product_code IN (
             SELECT bsn_code FROM product_code_mapping WHERE product_id = ?
         )
    """, (brand_id, product_id))
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
    codes = [r[0] for r in conn.execute(
        'SELECT bsn_code FROM product_code_mapping WHERE product_id = ?',
        (product_id,)
    ).fetchall()]
    if not codes:
        conn.close()
        return
    placeholders = ','.join(['?'] * len(codes))
    triples = conn.execute(f"""
        SELECT DISTINCT pin.salesperson_code,
                        substr(pin.date_iso, 1, 7) AS ym,
                        ref.invoice_no
          FROM express_payments_in pin
          JOIN express_payment_in_invoice_refs ref ON ref.payment_in_id = pin.id
          JOIN express_sales es ON es.doc_no = ref.invoice_no
         WHERE pin.is_void = 0
           AND pin.salesperson_code <> ''
           AND es.product_code IN ({placeholders})
           AND es.date_iso < ?
    """, codes + [cutoff]).fetchall()
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


# ── Alerts ───────────────────────────────────────────────────────────────────

def get_stock_alerts():
    """Return products where shopee_stock + lazada_stock > warehouse quantity."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.id, p.sku, p.product_name, p.unit_type,
               COALESCE(s.quantity, 0)   AS quantity,
               p.shopee_stock,
               p.lazada_stock,
               (p.shopee_stock + p.lazada_stock) AS online_total,
               (p.shopee_stock + p.lazada_stock - COALESCE(s.quantity, 0)) AS excess
        FROM products p
        LEFT JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1
          AND (p.shopee_stock + p.lazada_stock) > COALESCE(s.quantity, 0)
        ORDER BY excess DESC
    """).fetchall()
    conn.close()
    return rows


def count_stock_alerts():
    conn = get_connection()
    n = conn.execute("""
        SELECT COUNT(*) FROM products p
        LEFT JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1
          AND (p.shopee_stock + p.lazada_stock) > COALESCE(s.quantity, 0)
    """).fetchone()[0]
    conn.close()
    return n


# ── Product Locations ─────────────────────────────────────────────────────────

def get_product_locations(product_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT floor_no FROM product_locations WHERE product_id = ? ORDER BY floor_no",
        (product_id,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_product_locations(product_id: int, locations: list):
    conn = get_connection()
    conn.execute("DELETE FROM product_locations WHERE product_id = ?", (product_id,))
    for loc in locations:
        loc = loc.strip()
        if loc:
            conn.execute(
                "INSERT INTO product_locations (product_id, floor_no) VALUES (?, ?)",
                (product_id, loc)
            )
    conn.commit()
    conn.close()


def count_low_stock():
    conn = get_connection()
    n = conn.execute("""
        SELECT COUNT(*) FROM products p
        JOIN stock_levels s ON s.product_id = p.id
        WHERE p.is_active = 1 AND s.quantity <= p.low_stock_threshold
    """).fetchone()[0]
    conn.close()
    return n


# ── Transactions ─────────────────────────────────────────────────────────────

def add_transaction(product_id: int, txn_type: str, quantity_change: int,
                    unit_mode: str, reference_no=None, note=None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO transactions (product_id, txn_type, quantity_change, unit_mode, reference_no, note)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (product_id, txn_type, quantity_change, unit_mode, reference_no, note))
    conn.commit()
    conn.close()


def get_current_stock(product_id: int) -> int:
    conn = get_connection()
    row = conn.execute("SELECT quantity FROM stock_levels WHERE product_id = ?", (product_id,)).fetchone()
    conn.close()
    return row['quantity'] if row else 0


def get_transactions(product_id=None, txn_type=None, date_from=None, date_to=None, page=1, per_page=50):
    conn = get_connection()
    conditions = ["1=1"]
    params = []
    if product_id:
        conditions.append("t.product_id = ?")
        params.append(product_id)
    if txn_type:
        conditions.append("t.txn_type = ?")
        params.append(txn_type)
    if date_from:
        conditions.append("DATE(t.created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(t.created_at) <= ?")
        params.append(date_to)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT t.*, p.product_name, p.sku, p.unit_type
        FROM transactions t
        JOIN products p ON p.id = t.product_id
        WHERE {where}
        ORDER BY t.created_at DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {where}", params).fetchone()[0]
    conn.close()
    return rows, total


def get_recent_transactions(limit=10):
    conn = get_connection()
    rows = conn.execute("""
        SELECT t.*, p.product_name, p.sku, p.unit_type
        FROM transactions t
        JOIN products p ON p.id = t.product_id
        ORDER BY t.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


# ── Promotions ────────────────────────────────────────────────────────────────

def get_promotions(product_id: int, active_only=False):
    conn = get_connection()
    cond = "WHERE product_id = ?"
    params = [product_id]
    if active_only:
        cond += " AND is_active = 1"
    rows = conn.execute(f"SELECT * FROM promotions {cond} ORDER BY created_at DESC", params).fetchall()
    conn.close()
    return rows


def get_active_promotion(product_id: int):
    today = date.today().isoformat()
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM promotions
        WHERE product_id = ? AND is_active = 1
          AND (date_start IS NULL OR date_start <= ?)
          AND (date_end IS NULL OR date_end >= ?)
        ORDER BY created_at DESC
        LIMIT 1
    """, (product_id, today, today)).fetchone()
    conn.close()
    return row


def effective_price(product) -> float:
    promo = get_active_promotion(product['id'])
    if promo is None:
        return product['base_sell_price']
    if promo['promo_type'] == 'percent':
        return round(product['base_sell_price'] * (1 - promo['discount_value'] / 100), 2)
    return promo['discount_value']  # fixed price


def create_promotion(data: dict) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, date_start, date_end)
        VALUES (:product_id, :promo_name, :promo_type, :discount_value, :date_start, :date_end)
    """, data)
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def deactivate_promotion(promo_id: int):
    conn = get_connection()
    conn.execute("UPDATE promotions SET is_active = 0 WHERE id = ?", (promo_id,))
    conn.commit()
    conn.close()


# ── CSV Import ────────────────────────────────────────────────────────────────

def bulk_import_products(rows: list, overwrite=False) -> tuple:
    """rows: list of dicts with CSV fields. Returns (imported, skipped)."""
    conn = get_connection()
    imported = skipped = 0
    for r in rows:
        existing = conn.execute("SELECT id FROM products WHERE sku = ?", (r['sku'],)).fetchone()
        if existing and not overwrite:
            skipped += 1
            continue
        if existing and overwrite:
            conn.execute("""
                UPDATE products SET product_name=?, units_per_carton=?, units_per_box=?,
                    unit_type=?, hard_to_sell=?
                WHERE sku=?
            """, (r['product_name'], r['units_per_carton'], r['units_per_box'],
                  r['unit_type'], r['hard_to_sell'], r['sku']))
            skipped += 1
        else:
            cur = conn.execute("""
                INSERT INTO products (sku, product_name, units_per_carton, units_per_box, unit_type, hard_to_sell)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (r['sku'], r['product_name'], r['units_per_carton'], r['units_per_box'],
                  r['unit_type'], r['hard_to_sell']))
            conn.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (cur.lastrowid,))
            imported += 1
    conn.commit()
    conn.close()
    return imported, skipped


# ── BSN → Stock sync helpers ─────────────────────────────────────────────────

def _get_base_qty(conn, product_id: int, product_unit_type: str, bsn_unit: str, qty):
    """
    Convert BSN qty to base-unit qty.
    Returns float if conversion is known, None if the ratio is not yet defined.
    ไม่ปัดทศนิยม เพื่อรองรับ qty เช่น 0.5 หล
    """
    if bsn_unit is not None and bsn_unit.strip() == product_unit_type.strip():
        return qty
    row = conn.execute(
        "SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?",
        (product_id, bsn_unit)
    ).fetchone()
    if row:
        return qty * row['ratio']
    return None  # ratio not defined yet


def _sync_bsn_to_stock(conn, table: str, file_type: str):
    """
    สร้าง transaction ย้อนหลังสำหรับแถว BSN ที่มี product_id แล้ว
    แต่ยังไม่ถูก sync (synced_to_stock = 0)
    file_type: 'sales' → OUT,  'purchase' → IN
    """
    txn_type = 'IN' if file_type == 'purchase' else 'OUT'

    rows = conn.execute(
        f"SELECT * FROM {table} WHERE product_id IS NOT NULL AND synced_to_stock = 0"
    ).fetchall()

    for row in rows:
        product = conn.execute(
            "SELECT * FROM products WHERE id = ?", (row['product_id'],)
        ).fetchone()
        if not product:
            # mark synced เพื่อไม่วนซ้ำ
            conn.execute(f"UPDATE {table} SET synced_to_stock=1 WHERE id=?", (row['id'],))
            continue

        qty = row['qty'] or 0
        base_qty = _get_base_qty(conn, row['product_id'], product['unit_type'], row['unit'], qty)

        if base_qty is None:
            # Ratio not defined yet — skip until user defines it
            continue

        if base_qty > 0:
            change = base_qty if txn_type == 'IN' else -base_qty
            label  = 'ซื้อ' if file_type == 'purchase' else 'ขาย'
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                row['product_id'], txn_type, change, 'unit',
                row['doc_no'],
                f'BSN {label}',
                row['date_iso'] + ' 00:00:00',
            ))

            # Deduct online stock for Shopee/Lazada store customers
            if txn_type == 'OUT':
                customer = (row['customer'] or '').strip()
                platform = None
                if customer == 'หน้าร้านL':
                    platform = 'lazada'
                    conn.execute(
                        "UPDATE products SET lazada_stock = MAX(0, lazada_stock - ?) WHERE id = ?",
                        (base_qty, row['product_id'])
                    )
                elif customer == 'หน้าร้านS':
                    platform = 'shopee'
                    conn.execute(
                        "UPDATE products SET shopee_stock = MAX(0, shopee_stock - ?) WHERE id = ?",
                        (base_qty, row['product_id'])
                    )

                # Also deduct platform_skus.stock if mapped
                if platform and row['product_id']:
                    skus = conn.execute("""
                        SELECT id, qty_per_sale, stock FROM platform_skus
                        WHERE platform = ? AND internal_product_id = ?
                          AND qty_per_sale > 0
                        ORDER BY stock DESC
                    """, (platform, row['product_id'])).fetchall()
                    remaining = float(base_qty)
                    for sku in skus:
                        if remaining <= 0:
                            break
                        qps = float(sku['qty_per_sale'])
                        platform_units = remaining / qps
                        platform_deduct = round(platform_units)
                        if platform_deduct < 1:
                            platform_deduct = 1
                        conn.execute("""
                            UPDATE platform_skus
                            SET stock = MAX(0, stock - ?)
                            WHERE id = ?
                        """, (platform_deduct, sku['id']))
                        remaining -= platform_deduct * qps

            # history_import: สร้าง IN คู่เพื่อไม่ให้กระทบสต็อค
            if row['batch_id'] == 'history_import' and txn_type == 'OUT':
                conn.execute("""
                    INSERT INTO transactions
                        (product_id, txn_type, quantity_change, unit_mode,
                         reference_no, note, created_at)
                    VALUES (?, 'IN', ?, 'unit', ?, ?, ?)
                """, (
                    row['product_id'], base_qty,
                    row['doc_no'],
                    f'ประวัติขาย (ไม่นับสต็อค): {row["product_name_raw"]}',
                    row['date_iso'] + ' 00:00:00',
                ))

        conn.execute(f"UPDATE {table} SET synced_to_stock=1 WHERE id=?", (row['id'],))


def get_pending_unit_conversions(search=None):
    conn = get_connection()
    sql = """
        SELECT t.product_id, t.bsn_unit, p.product_name, p.unit_type,
               t.row_count, t.example_doc, t.bsn_raw_name
        FROM (
            SELECT product_id, unit AS bsn_unit,
                   COUNT(*) AS row_count,
                   MIN(doc_no) AS example_doc,
                   MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
            FROM sales_transactions
            WHERE product_id IS NOT NULL AND synced_to_stock = 0
            GROUP BY product_id, unit
            UNION ALL
            SELECT product_id, unit AS bsn_unit,
                   COUNT(*) AS row_count,
                   MIN(doc_no) AS example_doc,
                   MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
            FROM purchase_transactions
            WHERE product_id IS NOT NULL AND synced_to_stock = 0
            GROUP BY product_id, unit
        ) t
        JOIN products p ON p.id = t.product_id
        WHERE t.bsn_unit != p.unit_type
          AND NOT EXISTS (
              SELECT 1 FROM unit_conversions uc
              WHERE uc.product_id = t.product_id AND uc.bsn_unit = t.bsn_unit
          )
    """
    params = []
    if search:
        sql += " AND (p.product_name LIKE ? OR CAST(p.sku AS TEXT) LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " GROUP BY t.product_id, t.bsn_unit ORDER BY p.product_name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    # Flag rows whose bsn_unit is still an UNKNOWN acronym (import already
    # normalises known ones) so the UI can ask Put for the full unit name.
    out = []
    for r in rows:
        d = dict(r)
        d['is_acronym'] = not bsn_units.is_known(d['bsn_unit'])
        out.append(d)
    return out


def learn_acronyms_normalize(pairs: dict):
    """For each acronym→full Put typed on /unit-conversions: persist it to
    bsn_unit_full.json and rewrite that acronym → full across the BSN
    ledger (so it matches unit_conversions and never recurs)."""
    if not pairs:
        return
    conn = get_connection()
    for acr, full in pairs.items():
        bsn_units.add_acronym(acr, full)
        for t in ('sales_transactions', 'purchase_transactions'):
            conn.execute(f"UPDATE {t} SET unit=? WHERE unit=?", (full, acr))
    conn.commit()
    conn.close()


def save_unit_conversions(items: list):
    conn = get_connection()
    for item in items:
        conn.execute("""
            INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
            VALUES (?, ?, ?)
            ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio = excluded.ratio
        """, (item['product_id'], item['bsn_unit'], item['ratio']))
    # After saving, re-run sync for both tables
    _sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    _sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    conn.close()


def update_unit_conversion_ratio(product_id, bsn_unit, new_ratio):
    """อัปเดต ratio ที่มีอยู่แล้ว แล้ว re-sync BSN transactions ที่เกี่ยวข้อง"""
    conn = get_connection()

    # Update ratio
    conn.execute("""
        UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=?
    """, (new_ratio, product_id, bsn_unit))

    # Delete old BSN-generated stock transactions for this product
    conn.execute("""
        DELETE FROM transactions
        WHERE product_id=? AND note LIKE 'BSN %'
          AND reference_no IN (
              SELECT doc_no FROM sales_transactions
              WHERE product_id=? AND unit=? AND synced_to_stock=1
              UNION ALL
              SELECT doc_no FROM purchase_transactions
              WHERE product_id=? AND unit=? AND synced_to_stock=1
          )
    """, (product_id, product_id, bsn_unit, product_id, bsn_unit))

    # Reset synced_to_stock for affected BSN rows
    conn.execute("""
        UPDATE sales_transactions SET synced_to_stock=0
        WHERE product_id=? AND unit=?
    """, (product_id, bsn_unit))
    conn.execute("""
        UPDATE purchase_transactions SET synced_to_stock=0
        WHERE product_id=? AND unit=?
    """, (product_id, bsn_unit))

    # Re-sync
    _sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    _sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')

    # Recalculate stock_levels
    conn.execute("DELETE FROM stock_levels WHERE product_id=?", (product_id,))
    conn.execute("""
        INSERT INTO stock_levels (product_id, quantity)
        SELECT product_id, COALESCE(SUM(quantity_change), 0)
        FROM transactions WHERE product_id=?
    """, (product_id,))

    conn.commit()

    # WACC: recalculate after ratio change
    recalculate_product_wacc(product_id)

    conn.close()


def get_all_unit_conversions(search=None, page=1, per_page=50):
    conn = get_connection()
    where = ""
    params = []
    if search:
        where = "WHERE p.product_name LIKE ? OR CAST(p.sku AS TEXT) LIKE ?"
        params += [f"%{search}%", f"%{search}%"]

    sql = f"""
        SELECT uc.id, uc.product_id, uc.bsn_unit, uc.ratio,
               p.product_name, p.unit_type, p.sku,
               COALESCE(s.cnt, 0) + COALESCE(pu.cnt, 0) AS row_count,
               COALESCE(s.bsn_raw_name, pu.bsn_raw_name) AS bsn_raw_name
        FROM unit_conversions uc
        JOIN products p ON p.id = uc.product_id
        LEFT JOIN (
            SELECT product_id, unit, COUNT(*) AS cnt,
                   MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
            FROM sales_transactions
            GROUP BY product_id, unit
        ) s ON s.product_id = uc.product_id AND s.unit = uc.bsn_unit
        LEFT JOIN (
            SELECT product_id, unit, COUNT(*) AS cnt,
                   MIN(NULLIF(product_name_raw, '')) AS bsn_raw_name
            FROM purchase_transactions
            GROUP BY product_id, unit
        ) pu ON pu.product_id = uc.product_id AND pu.unit = uc.bsn_unit
        {where}
        ORDER BY p.product_name, uc.bsn_unit
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        SELECT COUNT(*) FROM unit_conversions uc
        JOIN products p ON p.id = uc.product_id
        {where}
    """
    total = conn.execute(count_sql, params).fetchone()[0]
    conn.close()
    return rows, total


def get_uncertain_no_ref_transactions():
    """ดึง transactions ที่ไม่มี reference_no จาก 2026-03-04 ที่ไม่มีคู่ซ้ำ (ที่มี ref_no)"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT t.id, t.product_id, t.txn_type, t.quantity_change,
               t.unit_mode, t.created_at,
               p.product_name, p.sku, p.unit_type
        FROM transactions t
        JOIN products p ON t.product_id=p.id
        WHERE (t.reference_no IS NULL OR t.reference_no='')
          AND t.created_at >= '2026-03-04'
          AND t.txn_type = 'OUT'
          AND t.note IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM transactions t2
              WHERE t2.product_id = t.product_id
                AND t2.quantity_change = t.quantity_change
                AND date(t2.created_at) = date(t.created_at)
                AND t2.txn_type = 'OUT'
                AND t2.reference_no IS NOT NULL AND t2.reference_no != ''
          )
        ORDER BY t.created_at, p.product_name
    """).fetchall()
    conn.close()
    return rows


def delete_transactions_by_ids(ids):
    if not ids:
        return
    conn = get_connection()
    try:
        placeholders = ','.join(['?']*len(ids))
        affected = [r['product_id'] for r in conn.execute(
            f"SELECT DISTINCT product_id FROM transactions WHERE id IN ({placeholders})", ids
        ).fetchall()]
        conn.execute(f"DELETE FROM transactions WHERE id IN ({placeholders})", ids)
        for pid in affected:
            conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
            conn.execute("""
                INSERT INTO stock_levels (product_id, quantity)
                SELECT product_id, COALESCE(SUM(quantity_change), 0)
                FROM transactions WHERE product_id=?
                GROUP BY product_id
            """, (pid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Product Code Mapping (BSN ↔ internal SKU) ─────────────────────────────────

def get_mapping(bsn_code: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM product_code_mapping WHERE bsn_code = ?", (bsn_code,)
    ).fetchone()
    conn.close()
    return row


def upsert_mapping(bsn_code: str, bsn_name: str, product_id=None, is_ignored=0,
                   ignore_reason=None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored, ignore_reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(bsn_code) DO UPDATE SET
            bsn_name      = excluded.bsn_name,
            product_id    = excluded.product_id,
            is_ignored    = excluded.is_ignored,
            ignore_reason = excluded.ignore_reason
    """, (bsn_code, bsn_name, product_id, is_ignored, ignore_reason))
    conn.commit()
    conn.close()


def upsert_unit_conversion(product_id: int, bsn_unit: str, ratio: float):
    """Set unit_conversion ratio for a (product, bsn_unit) pair.
    UNIQUE constraint on (product_id, bsn_unit) ensures upsert semantics."""
    if not bsn_unit or not ratio or float(ratio) <= 0:
        return
    conn = get_connection()
    conn.execute("""
        INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
        VALUES (?, ?, ?)
        ON CONFLICT(product_id, bsn_unit) DO UPDATE SET
            ratio = excluded.ratio
    """, (product_id, bsn_unit, float(ratio)))
    conn.commit()
    conn.close()


def get_pending_mappings():
    """Return all BSN codes not yet mapped and not ignored."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM product_code_mapping
        WHERE product_id IS NULL AND is_ignored = 0
        ORDER BY bsn_code
    """).fetchall()
    conn.close()
    return rows


def resolve_pending_mappings(conn):
    """
    เติม product_id ให้แถว BSN ที่ยังไม่มี แล้ว sync ไปยัง stock ทันที
    """
    for table, file_type in (
        ('sales_transactions',    'sales'),
        ('purchase_transactions', 'purchase'),
    ):
        conn.execute(f"""
            UPDATE {table}
            SET product_id = (
                SELECT m.product_id FROM product_code_mapping m
                WHERE m.bsn_code = {table}.bsn_code AND m.product_id IS NOT NULL
            )
            WHERE product_id IS NULL AND bsn_code IS NOT NULL
        """)
        # sync แถวที่เพิ่ง resolve ไปยัง transactions/stock
        _sync_bsn_to_stock(conn, table, file_type)
    conn.commit()


# ── Weekly Import ─────────────────────────────────────────────────────────────

def import_weekly(entries: list, file_type: str, filename: str) -> dict:
    """
    Insert sales or purchase entries; skip duplicates by doc_no.
    Returns stats dict.
    """
    assert file_type in ('sales', 'purchase')
    table = 'sales_transactions' if file_type == 'sales' else 'purchase_transactions'
    party_col = 'customer' if file_type == 'sales' else 'supplier'
    party_code_col = 'customer_code' if file_type == 'sales' else 'supplier_code'

    conn = get_connection()

    # Log the batch
    cur = conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) VALUES (?,0,0,?)",
        (filename, file_type)
    )
    batch_id = cur.lastrowid

    imported = skipped_dup = overwritten = 0
    new_bsn_codes = {}   # code → name for codes not yet in mapping table

    for e in entries:
        # Auto-normalise the BSN unit acronym → full Thai so it matches the
        # (already-normalised) unit_conversions table → far fewer pending.
        # Unknown acronyms are left as-is and surface on /unit-conversions.
        e['unit'] = bsn_units.normalize_unit(e.get('unit'))
        # คำนวณ doc_base ก่อน (IV6900527-1 → IV6900527, IV6900527 → IV6900527)
        doc_no   = e['doc_no']
        doc_base = doc_no.rsplit('-', 1)[0] if '-' in doc_no else doc_no
        is_weekly = (doc_no == doc_base)  # weekly format ไม่มี line suffix

        # Duplicate check แบบ 2 โหมด — ดึงแถวเก่ามาเพื่อ overwrite
        if is_weekly:
            old_rows = conn.execute(
                f"SELECT id, product_id, doc_no, synced_to_stock FROM {table}"
                f" WHERE doc_base = ? AND bsn_code = ? AND unit_price = ?",
                (doc_base, e['product_code_raw'], e['unit_price'])
            ).fetchall()
        else:
            old_rows = conn.execute(
                f"SELECT id, product_id, doc_no, synced_to_stock FROM {table}"
                f" WHERE bsn_code = ? AND (doc_no = ? OR doc_no = ?)",
                (e['product_code_raw'], doc_no, doc_base)
            ).fetchall()

        if old_rows:
            for old in old_rows:
                # ถ้า sync ไปสต็อกแล้ว ให้ลบ transaction เดิมและคำนวณสต็อกใหม่
                if old['synced_to_stock'] == 1 and old['product_id']:
                    conn.execute(
                        "DELETE FROM transactions WHERE product_id=? AND reference_no=? AND note LIKE 'BSN%'",
                        (old['product_id'], old['doc_no'])
                    )
                    conn.execute("DELETE FROM stock_levels WHERE product_id=?", (old['product_id'],))
                    # Use a literal product_id, not the SELECTed column: if
                    # no transactions remain, "SELECT product_id, SUM(...)"
                    # yields a (NULL, 0) row → stock_levels.product_id is an
                    # INTEGER PK so NULL auto-assigns a rowid not in
                    # products → orphan row (silent when FK off) / FK-fail
                    # (FK on, as in get_connection). Subquery keeps it safe.
                    conn.execute("""
                        INSERT INTO stock_levels (product_id, quantity)
                        VALUES (?, COALESCE(
                            (SELECT SUM(quantity_change) FROM transactions
                             WHERE product_id=?), 0))
                    """, (old['product_id'], old['product_id']))
                conn.execute(f"DELETE FROM {table} WHERE id=?", (old['id'],))
            overwritten += len(old_rows)

        # Resolve product_id via mapping table
        mapping = conn.execute(
            "SELECT product_id, is_ignored FROM product_code_mapping WHERE bsn_code = ?",
            (e['product_code_raw'],)
        ).fetchone()
        product_id = mapping['product_id'] if mapping else None
        is_ignored = mapping['is_ignored'] if mapping else 0

        if is_ignored:
            skipped_dup += 1
            continue

        # Track new BSN codes for mapping page
        if not mapping and e['product_code_raw']:
            new_bsn_codes[e['product_code_raw']] = e['product_name_raw']
        cur2 = conn.execute(f"""
            INSERT INTO {table}
                (batch_id, date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,
                 {party_col}, {party_code_col}, qty, unit, unit_price,
                 vat_type, discount, total, net)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            batch_id, e['date_iso'], doc_no, doc_base, product_id,
            e['product_code_raw'], e['product_name_raw'],
            e['party'], e['party_code'],
            e['qty'], e['unit'], e['unit_price'],
            e['vat_type'], e['discount'], e['total'], e['net']
        ))
        imported += 1

        # sync ไปยัง stock ทันทีถ้ารู้ product_id แล้ว
        if product_id:
            _sync_bsn_to_stock(conn, table, file_type)

    # Register new BSN codes in mapping table (unmapped)
    for code, name in new_bsn_codes.items():
        conn.execute("""
            INSERT OR IGNORE INTO product_code_mapping (bsn_code, bsn_name)
            VALUES (?, ?)
        """, (code, name))

    # Update batch log
    conn.execute(
        "UPDATE import_log SET rows_imported=?, rows_skipped=? WHERE id=?",
        (imported, skipped_dup, batch_id)
    )
    conn.commit()

    # WACC: recalculate for all products in this purchase batch
    if file_type == 'purchase':
        affected = [r[0] for r in conn.execute(
            "SELECT DISTINCT product_id FROM purchase_transactions"
            " WHERE batch_id=? AND product_id IS NOT NULL", (batch_id,)
        ).fetchall()]
        if affected:
            for pid in affected:
                recalculate_product_wacc(pid, conn)
            conn.commit()

    conn.close()

    return {
        'imported': imported,
        'skipped_dup': skipped_dup,
        'overwritten': overwritten,
        'new_unmapped': len(new_bsn_codes),
        'batch_id': batch_id,
    }


def get_recent_imports(limit=5):
    conn = get_connection()
    rows = conn.execute(
        "SELECT filename, rows_imported, rows_skipped, imported_at, notes "
        "FROM import_log ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


# ── Sales Queries ─────────────────────────────────────────────────────────────

def get_sales(product_id=None, date_from=None, date_to=None,
              vat_type=None, page=1, per_page=50):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if product_id:
        conds.append('s.product_id = ?'); params.append(product_id)
    if date_from:
        conds.append('s.date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('s.date_iso <= ?'); params.append(date_to)
    if vat_type is not None:
        conds.append('s.vat_type = ?'); params.append(vat_type)
    where = ' AND '.join(conds)
    sql = f"""
        SELECT s.*,
               COALESCE(p.product_name, s.product_name_raw) AS display_name,
               p.sku
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        ORDER BY s.date_iso DESC, s.doc_no
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page-1)*per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM sales_transactions s WHERE {where}", params
    ).fetchone()[0]
    conn.close()
    return rows, total


def get_purchases_by_doc(doc_base):
    """ดึงทุก line item ของใบสั่งซื้อ (เช่น HP6900017 → HP6900017-1, -2, ...)"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p2.*,
               COALESCE(p.product_name, p2.product_name_raw) AS display_name,
               p.sku
        FROM purchase_transactions p2
        LEFT JOIN products p ON p.id = p2.product_id
        WHERE p2.doc_no LIKE ? OR p2.doc_no = ?
        ORDER BY p2.doc_no
    """, (doc_base + '-%', doc_base)).fetchall()
    conn.close()
    return rows


def get_sales_summary(date_from=None, date_to=None):
    """Returns totals split by vat_type."""
    conn = get_connection()
    conds = ['1=1']
    params = []
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)
    rows = conn.execute(f"""
        SELECT vat_type,
               COUNT(*)       AS txn_count,
               SUM(qty)       AS total_qty,
               SUM(net)       AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY vat_type
    """, params).fetchall()
    conn.close()
    return rows


def get_sales_by_doc(doc_base):
    """ดึงทุก line item ของ invoice (เช่น IV6900394 → IV6900394-1, -2, ...)"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.*,
               COALESCE(p.product_name, s.product_name_raw) AS display_name,
               p.sku
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE s.doc_no LIKE ? OR s.doc_no = ?
        ORDER BY CAST(SUBSTR(s.doc_no, INSTR(s.doc_no, '-') + 1) AS INTEGER)
    """, (doc_base + '-%', doc_base)).fetchall()
    conn.close()
    return rows


# ── Trade Dashboard ───────────────────────────────────────────────────────────

def get_trade_dashboard(date_from=None, date_to=None):
    """
    date_from / date_to: 'YYYY-MM-DD' strings.
    Defaults to the most recent month that has actual data.
    Returns dict with summary cards, weekly trend, top products/customers/suppliers.
    """
    import calendar as _cal

    conn = get_connection()

    if not date_from and not date_to:
        today = date.today()
        date_from = today.strftime('%Y-%m-01')
        date_to   = today.strftime(f'%Y-%m-{_cal.monthrange(today.year, today.month)[1]:02d}')
    elif date_from and not date_to:
        date_to = date.today().isoformat()
    elif date_to and not date_from:
        date_from = '2000-01-01'

    # ── Summary this month ────────────────────────────────────────────────────
    s = conn.execute("""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()

    p = conn.execute("""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
    """, (date_from, date_to)).fetchone()

    # ── Weekly trend (within selected date range) ─────────────────────────────
    weekly_sales = conn.execute("""
        SELECT strftime('%Y-W%W', date_iso) AS week,
               COALESCE(SUM(net), 0) AS net
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
        GROUP BY week ORDER BY week
    """, (date_from, date_to)).fetchall()

    weekly_pur = conn.execute("""
        SELECT strftime('%Y-W%W', date_iso) AS week,
               COALESCE(SUM(net), 0) AS net
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
        GROUP BY week ORDER BY week
    """, (date_from, date_to)).fetchall()

    all_weeks   = sorted(set(r['week'] for r in weekly_sales) |
                         set(r['week'] for r in weekly_pur))
    s_by_week   = {r['week']: r['net'] for r in weekly_sales}
    p_by_week   = {r['week']: r['net'] for r in weekly_pur}
    weekly_trend = [
        {'week': w, 'sales': s_by_week.get(w, 0), 'purchases': p_by_week.get(w, 0)}
        for w in all_weeks
    ]

    # ── Top 10 สินค้าขายดี (by net) ──────────────────────────────────────────
    top_products = conn.execute("""
        SELECT COALESCE(pr.product_name, s.product_name_raw) AS name,
               COALESCE(pr.sku, 0) AS sku,
               s.product_id,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net
        FROM sales_transactions s
        LEFT JOIN products pr ON pr.id = s.product_id
        WHERE s.date_iso >= ? AND s.date_iso <= ?
        GROUP BY s.product_id, s.product_name_raw
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    # ── Top 10 ลูกค้า ─────────────────────────────────────────────────────────
    top_customers = conn.execute("""
        SELECT customer,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net)               AS total_net
        FROM sales_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND customer IS NOT NULL AND customer != ''
        GROUP BY customer
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    # ── Top 10 ซัพพลายเออร์ ──────────────────────────────────────────────────
    top_suppliers = conn.execute("""
        SELECT supplier,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net)               AS total_net
        FROM purchase_transactions
        WHERE date_iso >= ? AND date_iso <= ?
          AND supplier IS NOT NULL AND supplier != ''
        GROUP BY supplier
        ORDER BY total_net DESC
        LIMIT 10
    """, (date_from, date_to)).fetchall()

    conn.close()

    return {
        'date_from': date_from,
        'date_to': date_to,
        'sales': {
            'doc_count': s['doc_count'],
            'total_net': float(s['total_net']),
            'total_qty': s['total_qty'],
        },
        'purchases': {
            'doc_count': p['doc_count'],
            'total_net': float(p['total_net']),
            'total_qty': p['total_qty'],
        },
        'gross_profit': float(s['total_net']) - float(p['total_net']),
        'weekly_trend': weekly_trend,
        'top_products':  [dict(r) for r in top_products],
        'top_customers': [dict(r) for r in top_customers],
        'top_suppliers': [dict(r) for r in top_suppliers],
    }


# ── Product Trade Summary ─────────────────────────────────────────────────────

def get_product_trade_summary(product_id, date_from=None, date_to=None):
    """
    Returns sales summary for a specific product:
    top customers, monthly trend, recent docs.
    """
    conn = get_connection()
    conds = ['s.product_id = ?']
    params = [product_id]
    if date_from:
        conds.append('s.date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('s.date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

    product = conn.execute(
        'SELECT id, sku, product_name FROM products WHERE id = ?', (product_id,)
    ).fetchone()

    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT s.doc_no) AS doc_count,
               COALESCE(SUM(s.net), 0)  AS total_net,
               COALESCE(SUM(s.qty), 0)  AS total_qty,
               MIN(s.date_iso)          AS first_date,
               MAX(s.date_iso)          AS last_date
        FROM sales_transactions s
        WHERE {where}
    """, params).fetchone()

    top_customers = conn.execute(f"""
        SELECT s.customer,
               SUM(s.qty)            AS total_qty,
               SUM(s.net)            AS total_net,
               COUNT(DISTINCT s.doc_no) AS doc_count
        FROM sales_transactions s
        WHERE {where}
          AND s.customer IS NOT NULL AND s.customer != ''
        GROUP BY s.customer
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', s.date_iso) AS month,
               COUNT(DISTINCT s.doc_no) AS doc_count,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net
        FROM sales_transactions s
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    docs = conn.execute(f"""
        SELECT s.date_iso, s.doc_no, s.customer,
               SUM(s.qty) AS total_qty,
               SUM(s.net) AS total_net
        FROM sales_transactions s
        WHERE {where}
        GROUP BY s.doc_no
        ORDER BY s.date_iso DESC, s.doc_no
        LIMIT 200
    """, params).fetchall()

    conn.close()
    return {
        'product':    dict(product) if product else {},
        'date_from':  date_from,
        'date_to':    date_to,
        'summary':    dict(summary),
        'top_customers': [dict(r) for r in top_customers],
        'monthly':    [dict(r) for r in monthly],
        'docs':       [dict(r) for r in docs],
    }


# ── Customer Summary ──────────────────────────────────────────────────────────

def get_customer_summary(customer, date_from=None, date_to=None):
    """
    Returns summary + top products + monthly trend for a specific customer.
    """
    conn = get_connection()
    conds = ['customer = ?']
    params = [customer]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM sales_transactions
        WHERE {where}
    """, params).fetchone()

    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, s.product_name_raw) AS name,
               COALESCE(p.sku, 0) AS sku,
               p.id AS product_id,
               s.unit,
               SUM(s.qty)  AS total_qty,
               SUM(s.net)  AS total_net,
               COUNT(DISTINCT s.doc_no) AS doc_count
        FROM sales_transactions s
        LEFT JOIN products p ON p.id = s.product_id
        WHERE {where}
        GROUP BY s.product_id, s.product_name_raw
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    # All invoices (paginated not needed here — keep it simple, limit 200)
    docs = conn.execute(f"""
        SELECT date_iso, doc_no,
               COUNT(*) AS line_count,
               SUM(qty) AS total_qty,
               SUM(net) AS total_net
        FROM sales_transactions
        WHERE {where}
        GROUP BY doc_no
        ORDER BY date_iso DESC, doc_no
        LIMIT 200
    """, params).fetchall()

    # Pull region + salesperson from customers MASTER (post-D1 view migration).
    # 3-way fallback: salespersons.name → customers.salesperson code → '(ไม่กำหนด)'.
    # Same for region: regions.name_th → regions.code → '(ไม่ระบุ)'.
    master_row = conn.execute("""
        SELECT s.customer_code,
               c.code AS master_code, c.name AS master_name,
               c.salesperson AS sp_code, c.region_id,
               sp.name AS sp_name, sp.is_active AS sp_active,
               r.code AS region_code, r.name_th AS region_name
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        LEFT JOIN regions       r  ON r.id    = c.region_id
        WHERE s.customer = ?
        LIMIT 1
    """, [customer]).fetchone()

    customer_info = None
    customer_code = None
    salesperson_code = None
    salesperson_display = None
    salesperson_orphan = False
    region_code = None
    region_display = None

    if master_row:
        customer_code = master_row['customer_code']
        if master_row['master_code']:
            row = conn.execute(
                "SELECT * FROM customers WHERE code=?", [master_row['master_code']]
            ).fetchone()
            if row:
                customer_info = dict(row)
            salesperson_code = master_row['sp_code']
            if salesperson_code:
                if master_row['sp_name']:
                    salesperson_display = master_row['sp_name']
                else:
                    salesperson_display = salesperson_code
                    salesperson_orphan = True
            region_code = master_row['region_code']
            region_display = master_row['region_name'] or master_row['region_code']

    conn.close()
    return {
        'customer': customer,
        'customer_code': customer_code,
        'region': region_display,
        'region_code': region_code,
        'salesperson': salesperson_display,
        'salesperson_code': salesperson_code,
        'salesperson_orphan': salesperson_orphan,
        'customer_info': customer_info,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


# ── Customer List ─────────────────────────────────────────────────────────────

def get_regions():
    """Region list for filter dropdowns. Returns [{id, code, name_th}].
    Driven by the regions master (migration 010), not the legacy
    customer_regions snapshot."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, code, name_th FROM regions ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customers(search=None, region=None, region_id=None, page=1, per_page=50):
    """Customer list backed by customers master + salespersons + regions.

    Filter precedence: region_id (FK, new) > region (text, legacy URL).
    Returns customer rows with display fields:
        salesperson  → name from salespersons master, or raw code if orphan
        region       → name_th from regions, or code as fallback
    """
    conn = get_connection()
    conds = []
    params = []
    if search:
        conds.append("(s.customer LIKE ? OR s.customer_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]

    rid_int = None
    if region_id is not None and str(region_id).strip():
        try:
            rid_int = int(region_id)
        except (ValueError, TypeError):
            rid_int = None
    elif region:
        # Legacy URL: ?region=<code or name_th>. Resolve to id.
        match = conn.execute(
            "SELECT id FROM regions WHERE code = ? OR name_th = ? LIMIT 1",
            (region, region),
        ).fetchone()
        if match:
            rid_int = match['id']
    if rid_int is not None:
        conds.append("c.region_id = ?")
        params.append(rid_int)

    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT s.customer, s.customer_code,
               COALESCE(r.name_th, r.code)              AS region,
               r.code                                   AS region_code,
               c.region_id,
               COALESCE(sp.name, c.salesperson)         AS salesperson,
               c.salesperson                            AS salesperson_code,
               (c.salesperson IS NOT NULL
                  AND c.salesperson != ''
                  AND sp.code IS NULL)                  AS salesperson_orphan,
               COUNT(DISTINCT s.doc_no)                 AS doc_count,
               COALESCE(SUM(s.net), 0)                  AS total_net,
               MAX(s.date_iso)                          AS last_date,
               (c.code IS NULL)                         AS missing_master
        FROM sales_transactions s
        LEFT JOIN customers     c  ON c.code  = s.customer_code
        LEFT JOIN salespersons  sp ON sp.code = c.salesperson
        LEFT JOIN regions       r  ON r.id    = c.region_id
        {where}
        GROUP BY s.customer_code
        ORDER BY s.customer
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        SELECT COUNT(DISTINCT s.customer_code)
        FROM sales_transactions s
        LEFT JOIN customers c ON c.code = s.customer_code
        {where}
    """
    total = conn.execute(count_sql, params).fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


# ── Customer Assignment (salesperson + region on customers master) ────────────
# Migration 010 introduced customers.salesperson (TEXT code) + customers.region_id
# (FK regions.id). The legacy customer_regions table is the *display* source
# (read by get_customer_summary / get_customers above) until UI migration D1
# lands. The helpers below write to the MASTER table only — audit triggers on
# customers cover the change automatically.

def get_all_regions_with_counts():
    conn = get_connection()
    rows = conn.execute("""
        SELECT r.id, r.code, r.name_th, r.sort_order, r.note,
               COUNT(c.code) AS customer_count
          FROM regions r
          LEFT JOIN customers c ON c.region_id = r.id
         GROUP BY r.id
         ORDER BY r.sort_order, r.code
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_region(region_id, name_th, sort_order, note):
    name_th = (name_th or '').strip() or None
    note    = (note or '').strip() or None
    try:
        sort_order = int(sort_order) if str(sort_order).strip() else 100
    except (ValueError, TypeError):
        return {'ok': False, 'error': 'sort_order ต้องเป็นจำนวนเต็ม'}

    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE regions SET name_th = ?, sort_order = ?, note = ? WHERE id = ?",
            (name_th, sort_order, note, region_id),
        )
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบ region id {region_id}'}
        conn.commit()
        return {'ok': True, 'error': None}
    finally:
        conn.close()


def get_active_salespersons():
    conn = get_connection()
    rows = conn.execute(
        "SELECT code, name FROM salespersons WHERE is_active = 1 ORDER BY code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_regions():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, code, name_th FROM regions ORDER BY sort_order, code"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_orphan_salesperson_codes():
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT salesperson AS code
        FROM customers
        WHERE salesperson IS NOT NULL
          AND salesperson != ''
          AND salesperson NOT IN (SELECT code FROM salespersons)
    """).fetchall()
    conn.close()
    return {r['code'] for r in rows}


def get_customer_master(customer_code):
    conn = get_connection()
    row = conn.execute(
        "SELECT code, name, salesperson, region_id FROM customers WHERE code = ?",
        [customer_code],
    ).fetchone()
    conn.close()
    return dict(row) if row else None


_BULK_MAX = 5000  # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; cap well below.


def update_customer_assignment(customer_code, salesperson_code, region_id):
    sp = (salesperson_code or '').strip() or None
    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is not None:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            return {'ok': False, 'error': 'region_id ไม่ถูกต้อง'}

    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT salesperson FROM customers WHERE code = ?", (customer_code,)
        ).fetchone()
        if current is None:
            return {'ok': False, 'error': f'ไม่พบ customer code "{customer_code}"'}

        # Skip the active-salesperson check when the value is unchanged so a
        # customer with a legacy/orphan code can re-save other fields without
        # being forced to switch salesperson.
        if sp is not None and sp != current['salesperson']:
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}
        if rid is not None:
            if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
                return {'ok': False, 'error': f'ไม่พบ region id {rid}'}

        conn.execute(
            "UPDATE customers SET salesperson = ?, region_id = ? WHERE code = ?",
            (sp, rid, customer_code),
        )
        conn.commit()
        return {'ok': True, 'error': None}
    finally:
        conn.close()


def bulk_reassign_customers(customer_codes, salesperson_code, region_id, mode='both'):
    if mode not in ('salesperson', 'region', 'both'):
        return {'ok': False, 'updated': 0, 'error': 'mode ไม่ถูกต้อง'}
    if not customer_codes:
        return {'ok': False, 'updated': 0, 'error': 'ไม่มีลูกค้าที่เลือก'}
    if len(customer_codes) > _BULK_MAX:
        return {'ok': False, 'updated': 0,
                'error': f'เลือกได้สูงสุด {_BULK_MAX} ลูกค้า (เลือก {len(customer_codes)})'}

    sp = (salesperson_code or '').strip() or None
    rid = region_id if region_id not in ('', None, 'null') else None
    if rid is not None:
        try:
            rid = int(rid)
        except (ValueError, TypeError):
            return {'ok': False, 'updated': 0, 'error': 'region_id ไม่ถูกต้อง'}

    # Block silent mass-NULL clearing: when a column is in scope it must have a
    # non-empty target. (Future feature can add an explicit "clear" mode.)
    if mode in ('salesperson', 'both') and sp is None:
        return {'ok': False, 'updated': 0, 'error': 'กรุณาเลือก salesperson ปลายทาง'}
    if mode in ('region', 'both') and rid is None:
        return {'ok': False, 'updated': 0, 'error': 'กรุณาเลือก region ปลายทาง'}

    conn = get_connection()
    try:
        if mode in ('salesperson', 'both'):
            if not conn.execute(
                "SELECT 1 FROM salespersons WHERE code = ? AND is_active = 1", (sp,)
            ).fetchone():
                return {'ok': False, 'updated': 0,
                        'error': f'ไม่พบ salesperson code "{sp}" (หรือ inactive)'}
        if mode in ('region', 'both'):
            if not conn.execute("SELECT 1 FROM regions WHERE id = ?", (rid,)).fetchone():
                return {'ok': False, 'updated': 0, 'error': f'ไม่พบ region id {rid}'}

        placeholders = ','.join(['?'] * len(customer_codes))
        if mode == 'salesperson':
            sql = f"UPDATE customers SET salesperson = ? WHERE code IN ({placeholders})"
            params = [sp, *customer_codes]
        elif mode == 'region':
            sql = f"UPDATE customers SET region_id = ? WHERE code IN ({placeholders})"
            params = [rid, *customer_codes]
        else:
            sql = (f"UPDATE customers SET salesperson = ?, region_id = ? "
                   f"WHERE code IN ({placeholders})")
            params = [sp, rid, *customer_codes]

        with conn:
            cur = conn.execute(sql, params)
        return {'ok': True, 'updated': cur.rowcount, 'error': None}
    finally:
        conn.close()


def get_customers_master(search=None, salesperson=None, region_id=None,
                         orphan_only=False, page=1, per_page=100):
    conn = get_connection()
    conds = []
    params = []
    if search:
        conds.append("(c.code LIKE ? OR c.name LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if salesperson == '__none__':
        conds.append("(c.salesperson IS NULL OR c.salesperson = '')")
    elif salesperson:
        conds.append("c.salesperson = ?")
        params.append(salesperson)
    if region_id:
        conds.append("c.region_id = ?")
        params.append(int(region_id))
    if orphan_only:
        conds.append(
            "c.salesperson IS NOT NULL AND c.salesperson != '' "
            "AND c.salesperson NOT IN (SELECT code FROM salespersons)"
        )
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT c.code, c.name, c.salesperson AS salesperson_code,
               s.name AS salesperson_name, s.is_active AS salesperson_active,
               c.region_id, r.code AS region_code, r.name_th AS region_name
        FROM customers c
        LEFT JOIN salespersons s ON s.code = c.salesperson
        LEFT JOIN regions      r ON r.id   = c.region_id
        {where}
        ORDER BY c.name
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM customers c {where}", params
    ).fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


# ── Commission Overrides (CRUD) ──────────────────────────────────────────────
# commission_overrides table holds per-product or per-brand commission rules
# that beat the tier rate. Schema invariants (DB CHECK):
#   - exactly one of (product_id, brand_id) is set
#   - exactly one of (fixed_per_unit, custom_rate_pct) is set
# Resolution priority (commission.py): product > brand; salesperson-specific
# > generic.
#
# Audit triggers (migration 023) capture every INSERT/UPDATE/DELETE.
# Callers writing to this table MUST call commission.clear_override_cache()
# after a successful write so the engine reloads.

def _normalise_override_payload(data):
    """Coerce form values into the shape stored in DB. Returns
    (normalised_dict, error_str_or_None)."""
    scope = (data.get('scope') or '').strip()
    rate_kind = (data.get('rate_kind') or '').strip()

    out = {
        'product_id':           None,
        'brand_id':              None,
        'salesperson_code':      None,
        'fixed_per_unit':        None,
        'custom_rate_pct':       None,
        'apply_when_price_gt':   0.0,
        'apply_when_price_lte':  None,
        'is_active':             1,
        'effective_from':        (data.get('effective_from') or '').strip() or None,
        'note':                  (data.get('note') or '').strip() or None,
    }

    if scope == 'product':
        pid_raw = (data.get('product_id') or '').strip()
        if not pid_raw.isdigit():
            return None, 'กรุณาเลือกสินค้า'
        out['product_id'] = int(pid_raw)
    elif scope == 'brand':
        bid_raw = (data.get('brand_id') or '').strip()
        if not bid_raw.isdigit():
            return None, 'กรุณาเลือกแบรนด์'
        out['brand_id'] = int(bid_raw)
    else:
        return None, 'กรุณาเลือก scope (product / brand)'

    if rate_kind == 'fixed':
        try:
            v = float((data.get('fixed_per_unit') or '').strip())
        except ValueError:
            return None, 'fixed_per_unit ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'fixed_per_unit ต้อง ≥ 0'
        out['fixed_per_unit'] = v
    elif rate_kind == 'percent':
        try:
            v = float((data.get('custom_rate_pct') or '').strip())
        except ValueError:
            return None, 'custom_rate_pct ต้องเป็นตัวเลข'
        if v < 0 or v > 100:
            return None, 'custom_rate_pct ต้องอยู่ระหว่าง 0 และ 100'
        out['custom_rate_pct'] = v
    else:
        return None, 'กรุณาเลือกประเภทอัตรา (fixed / percentage)'

    sp = (data.get('salesperson_code') or '').strip() or None
    if sp:
        out['salesperson_code'] = sp

    gt_raw = (data.get('apply_when_price_gt') or '').strip()
    if gt_raw:
        try:
            v = float(gt_raw)
        except ValueError:
            return None, 'price_gt ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'price_gt ต้อง ≥ 0'
        out['apply_when_price_gt'] = v

    lte_raw = (data.get('apply_when_price_lte') or '').strip()
    if lte_raw:
        try:
            v = float(lte_raw)
        except ValueError:
            return None, 'price_lte ต้องเป็นตัวเลข'
        if v < 0:
            return None, 'price_lte ต้อง ≥ 0'
        if v <= out['apply_when_price_gt']:
            return None, 'price_lte ต้องมากกว่า price_gt'
        out['apply_when_price_lte'] = v

    out['is_active'] = 1 if (data.get('is_active') in (1, '1', 'on', True)) else 0
    return out, None


def _validate_override_targets(conn, payload):
    if payload['product_id'] is not None:
        if not conn.execute(
            "SELECT 1 FROM products WHERE id = ?", (payload['product_id'],)
        ).fetchone():
            return f'ไม่พบ product id {payload["product_id"]}'
    if payload['brand_id'] is not None:
        if not conn.execute(
            "SELECT 1 FROM brands WHERE id = ?", (payload['brand_id'],)
        ).fetchone():
            return f'ไม่พบ brand id {payload["brand_id"]}'
    if payload['salesperson_code'] is not None:
        if not conn.execute(
            "SELECT 1 FROM salespersons WHERE code = ?", (payload['salesperson_code'],)
        ).fetchone():
            return f'ไม่พบ salesperson code "{payload["salesperson_code"]}"'
    return None


def list_commission_overrides(active_only=False):
    conn = get_connection()
    where = "WHERE co.is_active = 1" if active_only else ""
    sql = f"""
        SELECT co.id, co.product_id, co.brand_id, co.salesperson_code,
               co.fixed_per_unit, co.custom_rate_pct,
               co.apply_when_price_gt, co.apply_when_price_lte,
               co.is_active, co.effective_from, co.note,
               co.created_at, co.updated_at,
               p.product_name, p.sku,
               b.name AS brand_name, b.code AS brand_code, b.is_own_brand,
               s.name AS salesperson_name
          FROM commission_overrides co
          LEFT JOIN products     p ON p.id   = co.product_id
          LEFT JOIN brands       b ON b.id   = co.brand_id
          LEFT JOIN salespersons s ON s.code = co.salesperson_code
          {where}
         ORDER BY co.is_active DESC, co.id DESC
    """
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_commission_override(override_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT co.*, p.product_name, p.sku,
               b.name AS brand_name, b.code AS brand_code,
               s.name AS salesperson_name
          FROM commission_overrides co
          LEFT JOIN products     p ON p.id   = co.product_id
          LEFT JOIN brands       b ON b.id   = co.brand_id
          LEFT JOIN salespersons s ON s.code = co.salesperson_code
         WHERE co.id = ?
    """, (override_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_commission_override(form_data):
    payload, err = _normalise_override_payload(form_data)
    if err:
        return {'ok': False, 'id': None, 'error': err}

    conn = get_connection()
    try:
        err = _validate_override_targets(conn, payload)
        if err:
            return {'ok': False, 'id': None, 'error': err}
        cur = conn.execute("""
            INSERT INTO commission_overrides
                (product_id, brand_id, salesperson_code,
                 fixed_per_unit, custom_rate_pct,
                 apply_when_price_gt, apply_when_price_lte,
                 is_active, effective_from, note)
            VALUES (:product_id, :brand_id, :salesperson_code,
                    :fixed_per_unit, :custom_rate_pct,
                    :apply_when_price_gt, :apply_when_price_lte,
                    :is_active, COALESCE(:effective_from, date('now')), :note)
        """, payload)
        conn.commit()
        return {'ok': True, 'id': cur.lastrowid, 'error': None}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'id': None, 'error': f'ข้อมูลไม่ตรงตามข้อกำหนด: {e}'}
    finally:
        conn.close()


def update_commission_override(override_id, form_data):
    payload, err = _normalise_override_payload(form_data)
    if err:
        return {'ok': False, 'error': err}

    conn = get_connection()
    try:
        if not conn.execute(
            "SELECT 1 FROM commission_overrides WHERE id = ?", (override_id,)
        ).fetchone():
            return {'ok': False, 'error': f'ไม่พบ override id {override_id}'}
        err = _validate_override_targets(conn, payload)
        if err:
            return {'ok': False, 'error': err}
        payload_with_id = dict(payload)
        payload_with_id['id'] = override_id
        conn.execute("""
            UPDATE commission_overrides
               SET product_id           = :product_id,
                   brand_id             = :brand_id,
                   salesperson_code     = :salesperson_code,
                   fixed_per_unit       = :fixed_per_unit,
                   custom_rate_pct      = :custom_rate_pct,
                   apply_when_price_gt  = :apply_when_price_gt,
                   apply_when_price_lte = :apply_when_price_lte,
                   is_active            = :is_active,
                   effective_from       = COALESCE(:effective_from, effective_from),
                   note                 = :note,
                   updated_at           = datetime('now','localtime')
             WHERE id = :id
        """, payload_with_id)
        conn.commit()
        return {'ok': True, 'error': None}
    except sqlite3.IntegrityError as e:
        return {'ok': False, 'error': f'ข้อมูลไม่ตรงตามข้อกำหนด: {e}'}
    finally:
        conn.close()


def toggle_commission_override(override_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_active FROM commission_overrides WHERE id = ?", (override_id,)
        ).fetchone()
        if row is None:
            return {'ok': False, 'is_active': None, 'error': f'ไม่พบ override id {override_id}'}
        new_state = 0 if row['is_active'] else 1
        conn.execute(
            "UPDATE commission_overrides SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (new_state, override_id),
        )
        conn.commit()
        return {'ok': True, 'is_active': new_state, 'error': None}
    finally:
        conn.close()


def delete_commission_override(override_id):
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM commission_overrides WHERE id = ?", (override_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return {'ok': False, 'error': f'ไม่พบ override id {override_id}'}
        return {'ok': True, 'error': None}
    finally:
        conn.close()


# ── Purchase Queries ──────────────────────────────────────────────────────────

def get_purchases(product_id=None, date_from=None, date_to=None, page=1, per_page=50):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if product_id:
        conds.append('p2.product_id = ?'); params.append(product_id)
    if date_from:
        conds.append('p2.date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('p2.date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)
    sql = f"""
        SELECT p2.*,
               COALESCE(p.product_name, p2.product_name_raw) AS display_name,
               p.sku
        FROM purchase_transactions p2
        LEFT JOIN products p ON p.id = p2.product_id
        WHERE {where}
        ORDER BY p2.date_iso DESC, p2.doc_no
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page-1)*per_page]).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM purchase_transactions p2 WHERE {where}", params
    ).fetchone()[0]
    conn.close()
    return rows, total


# ── Payment Status ─────────────────────────────────────────────────────────────

def parse_payment_csv(filepath):
    """Parse การรับชำระหนี้ CSV (cp874). Returns list of RE dicts with iv_list.

    iv_list shape: list of dicts, each {'iv_no': str, 'amount': float}.

    total (per RE record): sum of iv_list amounts. The RE header line carries
    what appear to be matching totals but they vary in layout depending on
    whether a cheque column is present, so the sum-of-IVs is the reliable
    source of truth. A receipt total equals what it applied across invoices,
    so sum-of-IVs is mathematically correct and avoids header-column ambiguity.
    """
    import re as _re
    records = []
    current = None
    with open(filepath, encoding='cp874') as f:
        for line in f:
            text = line.strip().strip('"').replace('\xa0', ' ')
            if not text:
                continue
            # RE header row.  Salesperson can be "06" (digits) or "06-L" (with branch
            # suffix), so allow non-space chars rather than requiring \w+.
            m = _re.match(r'^(\d{2}/\d{2}/\d{2})\s+(\*?RE\S+)\s+(.+?)\s{2,}(\S+)\s', text)
            if m:
                if current:
                    current['total'] = sum(iv['amount'] for iv in current['iv_list'])
                    records.append(current)
                d, re_no, customer, sp = m.groups()
                cancelled = re_no.startswith('*')
                re_no_clean = re_no.lstrip('*')
                dd, mm, yy = d.split('/')
                year_ce = int(yy) + 2500 - 543
                date_iso = f"{year_ce}-{mm}-{dd}"
                current = {
                    're_no': re_no_clean,
                    'cancelled': cancelled,
                    'date_iso': date_iso,
                    'customer': customer.strip(),
                    'salesperson': sp.strip(),
                    'iv_list': []
                }
                continue
            # IV sub-row — capture both the IV number (group 1) and the amount
            # (group 2: may contain thousands commas, e.g. "1,234.56").
            m2 = _re.match(r'\s*(IV\S+)\s+\d{2}/\d{2}/\d{2}\s+([\d,]+\.\d{2})', text)
            if m2 and current:
                iv_no = m2.group(1)
                amount = float(m2.group(2).replace(',', ''))
                current['iv_list'].append({'iv_no': iv_no, 'amount': amount})
    if current:
        current['total'] = sum(iv['amount'] for iv in current['iv_list'])
        records.append(current)
    return records


def import_payments(filepath):
    """Import payment CSV into received_payments + paid_invoices tables.

    Uses idempotent upserts (ON CONFLICT DO UPDATE) so re-importing the same
    file any number of times leaves row counts and every amount/total identical
    after the first successful run.

    Re_id resolution
    ----------------
    We use ``INSERT ... ON CONFLICT(re_no) DO UPDATE ... RETURNING id`` (SQLite
    ≥3.35, available here as sqlite 3.35+).  RETURNING delivers the canonical
    row id for BOTH the INSERT path and the UPDATE path in a single statement,
    removing all dependence on ``cur.lastrowid`` which is unreliable for the
    conflict/UPDATE path (it retains a stale value from the most recent plain
    INSERT on that connection, not 0 as the old guard assumed).

    Per-record transactional boundary
    ----------------------------------
    Each RE record is wrapped in a SAVEPOINT so that a single bad record
    (malformed amount, FK violation, etc.) is fully rolled back without
    discarding the good work that came before.  After the loop a single
    ``conn.commit()`` flushes all survivors.

    Returns dict
    ------------
      imported  — brand-new RE rows (did not exist before this run)
      updated   — existing RE rows refreshed (upsert took the UPDATE path)
      skipped   — RE records that raised an exception (isolated, rolled back)
      total     — total RE records parsed from the file
      errors    — list of up to 5 distinct exception reprs from skipped records
                  (empty list when all records imported cleanly)

    Invariant: ``imported + updated + skipped == total`` always holds.

    Note: legacy rows imported before migration 058 have amount/total = NULL;
    they are updated to carry real amounts the first time that RE is re-imported.
    """
    records = parse_payment_csv(filepath)
    conn = get_connection()
    imported = 0
    updated = 0
    skipped = 0
    errors = []          # up to 5 distinct repr strings

    for i, r in enumerate(records):
        sp = f"sp_re_{i}"
        try:
            conn.execute(f"SAVEPOINT {sp}")

            # --- Classify as new vs existing BEFORE the upsert ---
            # (rowcount after an UPSERT is always 1 in SQLite regardless of path,
            # so we must pre-check existence to distinguish insert from update.)
            existing = conn.execute(
                "SELECT 1 FROM received_payments WHERE re_no=?", (r['re_no'],)
            ).fetchone()
            is_new = existing is None

            # --- Authoritative upsert with RETURNING id ---
            # RETURNING delivers the real id for BOTH the INSERT and the UPDATE
            # conflict path — no dependence on cur.lastrowid.
            row = conn.execute(
                """INSERT INTO received_payments
                       (re_no, date_iso, customer, salesperson, cancelled, total)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(re_no) DO UPDATE SET
                       date_iso=excluded.date_iso,
                       customer=excluded.customer,
                       salesperson=excluded.salesperson,
                       cancelled=excluded.cancelled,
                       total=excluded.total
                   RETURNING id""",
                (r['re_no'], r['date_iso'], r['customer'], r['salesperson'],
                 1 if r['cancelled'] else 0, r.get('total'))
            ).fetchone()

            assert row is not None, f"RETURNING id returned nothing for re_no={r['re_no']!r}"
            re_id = row[0]
            assert re_id, f"re_id resolved to falsy value for re_no={r['re_no']!r}"

            for iv in r['iv_list']:
                conn.execute(
                    """INSERT INTO paid_invoices (re_id, iv_no, amount)
                       VALUES (?,?,?)
                       ON CONFLICT(re_id, iv_no) DO UPDATE SET
                           amount=excluded.amount""",
                    (re_id, iv['iv_no'], iv['amount'])
                )

            conn.execute(f"RELEASE SAVEPOINT {sp}")

            if is_new:
                imported += 1
            else:
                updated += 1

        except Exception as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
            skipped += 1
            if len(errors) < 5:
                errors.append(repr(exc))

    conn.commit()
    conn.close()
    return {
        'imported': imported,
        'updated': updated,
        'skipped': skipped,
        'total': len(records),
        'errors': errors,
    }


def get_payment_status(status='all', search='', date_from='', date_to='', page=1, per_page=50):
    """Get IV invoices with payment status.
    Uses pre-computed doc_base column + index for performance.
    """
    conn = get_connection()

    conds = ["st.doc_base IS NOT NULL", "st.doc_base NOT LIKE 'SR%'", "st.doc_base NOT LIKE 'HS%'"]
    params = []

    if search:
        conds.append("(st.doc_base LIKE ? OR st.customer LIKE ?)")
        params += [f'%{search}%', f'%{search}%']
    if date_from:
        conds.append("st.date_iso >= ?"); params.append(date_from)
    if date_to:
        conds.append("st.date_iso <= ?"); params.append(date_to)

    paid_filter = ''
    if status == 'paid':
        paid_filter = 'HAVING is_paid = 1'
    elif status == 'unpaid':
        paid_filter = 'HAVING is_paid = 0 AND total_net > 0'
    else:
        paid_filter = 'HAVING total_net > 0'

    where = ' AND '.join(conds)

    sql = f"""
        SELECT
            st.doc_base,
            MIN(st.date_iso) AS bill_date,
            st.customer,
            SUM(CASE WHEN st.vat_type = 2 THEN st.net * 1.07 ELSE st.net END) AS total_net,
            MAX(CASE WHEN pi.iv_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
            MAX(rp.date_iso) AS paid_date,
            MAX(rp.re_no) AS re_no
        FROM sales_transactions st
        LEFT JOIN paid_invoices pi ON pi.iv_no = st.doc_base
        LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
        WHERE {where}
        GROUP BY st.doc_base
        {paid_filter}
        ORDER BY bill_date DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()

    count_sql = f"""
        SELECT COUNT(*) FROM (
            SELECT st.doc_base,
                MAX(CASE WHEN pi.iv_no IS NOT NULL THEN 1 ELSE 0 END) AS is_paid,
                SUM(CASE WHEN st.vat_type = 2 THEN st.net * 1.07 ELSE st.net END) AS total_net
            FROM sales_transactions st
            LEFT JOIN paid_invoices pi ON pi.iv_no = st.doc_base
            LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
            WHERE {where}
            GROUP BY st.doc_base
            {paid_filter}
        )
    """
    total = conn.execute(count_sql, params).fetchone()[0]
    conn.close()
    return rows, total


def get_payment_summary():
    """Quick stats for payment status page."""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT st.doc_base) AS total_bills,
            SUM(CASE WHEN pi.iv_no IS NOT NULL THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN pi.iv_no IS NULL THEN 1 ELSE 0 END) AS unpaid_count,
            SUM(CASE WHEN pi.iv_no IS NOT NULL THEN st.net ELSE 0 END) AS paid_amount,
            SUM(CASE WHEN pi.iv_no IS NULL THEN st.net ELSE 0 END) AS unpaid_amount
        FROM (
            SELECT doc_base,
                   SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END) AS net
            FROM sales_transactions
            WHERE doc_base IS NOT NULL AND doc_base NOT LIKE 'SR%' AND doc_base NOT LIKE 'HS%'
            GROUP BY doc_base
            HAVING SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END) > 0
        ) st
        LEFT JOIN paid_invoices pi ON pi.iv_no = st.doc_base
        LEFT JOIN received_payments rp ON rp.id = pi.re_id AND rp.cancelled = 0
    """).fetchone()
    conn.close()
    return row


def get_customer_debt_summary(search=''):
    """สรุปหนี้ค้างชำระรายลูกค้า เรียงตามยอดค้างมากสุด.

    Sourced from express_ar_outstanding (latest snapshot) — same data as
    /express/ar — filtered to doc_date_iso >= 2024-01-01 (Sendy import
    window). Per Put 2026-05-02: BSN sync และ Express ใช้แหล่งเดียวกัน,
    ใช้ Express snapshot เป็น source of truth, จำกัดช่วงเดียวกับ Sendy
    import (2024-01-01 ถึงปัจจุบัน) เพื่อไม่นับ legacy debt ก่อนยุคนั้น.
    """
    conn = get_connection()
    cond = ""
    params = []
    if search:
        cond = "AND (ao.customer_name LIKE ? OR ao.customer_code LIKE ?)"
        params += [f'%{search}%', f'%{search}%']

    rows = conn.execute(f"""
        SELECT
            COALESCE(c.name, ao.customer_name) AS customer,
            ao.customer_code,
            COUNT(*)                           AS unpaid_bills,
            ROUND(SUM(ao.outstanding_amount), 2) AS outstanding_amount
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.snapshot_date_iso = (SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding)
          AND ao.doc_date_iso >= '2024-01-01'
          {cond}
        GROUP BY ao.customer_code
        HAVING outstanding_amount > 0
        ORDER BY outstanding_amount DESC
    """, params).fetchall()

    conn.close()
    return rows


def find_payment_candidates(amount, tolerance_pct=5):
    """คาดคะเนลูกค้าที่น่าจะโอนเงิน amount บาท
    ลองทุก subset ของบิลที่ค้างชำระของแต่ละลูกค้า
    คืนค่า list of dict เรียงตาม abs(diff) ASC
    """
    from itertools import combinations

    conn = get_connection()
    # ดึงบิลค้างชำระทั้งหมดแยกรายบิล (รวม vat_type ที่พบมากที่สุดในบิล)
    bill_rows = conn.execute("""
        SELECT st.customer, st.customer_code, st.doc_base,
               SUM(CASE WHEN st.vat_type=2 THEN st.net*1.07 ELSE st.net END) AS bill_net,
               MAX(st.vat_type) AS vat_type
        FROM sales_transactions st
        LEFT JOIN paid_invoices pi ON pi.iv_no = st.doc_base
        WHERE st.doc_base IS NOT NULL
          AND st.doc_base NOT LIKE 'SR%' AND st.doc_base NOT LIKE 'HS%'
          AND pi.iv_no IS NULL
        GROUP BY st.customer, st.customer_code, st.doc_base
        HAVING bill_net > 0
        ORDER BY st.customer, st.doc_base
    """).fetchall()
    conn.close()

    # จัดกลุ่มตามลูกค้า
    customers = {}
    for r in bill_rows:
        key = r['customer']
        if key not in customers:
            customers[key] = {'customer_code': r['customer_code'], 'bills': []}
        customers[key]['bills'].append({'doc_base': r['doc_base'], 'net': r['bill_net'], 'vat_type': r['vat_type']})

    tolerance = max(amount * tolerance_pct / 100, 200)
    results = []

    for customer, data in customers.items():
        bills = data['bills']
        if len(bills) > 15:
            # ถ้าบิลเยอะเกินไป ตรวจแค่ยอดรวมทั้งหมด
            total = sum(b['net'] for b in bills)
            if abs(total - amount) <= tolerance:
                results.append({
                    'customer': customer,
                    'customer_code': data['customer_code'],
                    'matched_bills': [{'doc_base': b['doc_base'], 'vat_type': b['vat_type']} for b in bills],
                    'matched_sum': total,
                    'diff': total - amount,
                    'total_unpaid_bills': len(bills),
                    'total_outstanding': total,
                })
            continue

        best_per_customer = []
        for r in range(1, len(bills) + 1):
            for combo in combinations(bills, r):
                combo_sum = sum(b['net'] for b in combo)
                diff = combo_sum - amount
                if abs(diff) <= tolerance:
                    best_per_customer.append({
                        'customer': customer,
                        'customer_code': data['customer_code'],
                        'matched_bills': [{'doc_base': b['doc_base'], 'vat_type': b['vat_type']} for b in combo],
                        'matched_sum': combo_sum,
                        'diff': diff,
                        'total_unpaid_bills': len(bills),
                        'total_outstanding': sum(b['net'] for b in bills),
                    })

        # เก็บแค่ 3 combo ที่ใกล้ที่สุดต่อลูกค้า
        best_per_customer.sort(key=lambda x: abs(x['diff']))
        results.extend(best_per_customer[:3])

    results.sort(key=lambda x: abs(x['diff']))
    return results[:20]


def get_product_pricing_summary(product_id):
    """สรุปราคา BSN สำหรับหน้า product detail (avg_list_price, avg_effective)"""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            SUM(unit_price * qty) / NULLIF(SUM(qty), 0) AS avg_list_price,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0)                      AS avg_effective,
            COUNT(DISTINCT unit_price)                   AS price_variants
        FROM sales_transactions
        WHERE product_id = ? AND qty > 0 AND unit_price > 0
    """, [product_id]).fetchone()
    conn.close()
    return {
        'avg_list_price': row['avg_list_price'] or 0.0,
        'avg_effective':  row['avg_effective']  or 0.0,
        'price_variants': row['price_variants'] or 0,
    }


def get_product_pricing(product_id):
    """ราคาขายสินค้า: list_prices (GROUP BY unit_price,vat_type) + effective_per_customer"""
    from collections import defaultdict

    conn = get_connection()

    # ── ราคาตั้งต่อ (unit_price, vat_type) ──────────────────────────────────
    price_rows = conn.execute("""
        SELECT
            unit_price,
            vat_type,
            COUNT(DISTINCT doc_no)  AS invoice_count,
            SUM(qty)                AS total_qty,
            MAX(date_iso)           AS last_sale,
            COUNT(DISTINCT customer) AS customer_count
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY unit_price, vat_type
        ORDER BY invoice_count DESC
    """, [product_id]).fetchall()

    # ── รายร้านค้าต่อ (unit_price, vat_type, customer) ───────────────────────
    cust_rows = conn.execute("""
        SELECT
            unit_price,
            vat_type,
            customer,
            customer_code,
            COUNT(DISTINCT doc_no)          AS invoice_count,
            SUM(qty)                        AS total_qty,
            MAX(date_iso)                   AS last_sale,
            GROUP_CONCAT(DISTINCT discount) AS discounts
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY unit_price, vat_type, customer
        ORDER BY unit_price, last_sale DESC
    """, [product_id]).fetchall()

    # ── ราคาจริงเฉลี่ยต่อร้าน ────────────────────────────────────────────────
    eff_rows = conn.execute("""
        SELECT
            customer,
            customer_code,
            COUNT(DISTINCT doc_no)  AS invoice_count,
            SUM(qty)                AS total_qty,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0) AS avg_effective,
            MAX(date_iso)           AS last_sale
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
        GROUP BY customer
        ORDER BY avg_effective DESC
    """, [product_id]).fetchall()

    # ── สรุปภาพรวม ────────────────────────────────────────────────────────────
    summary = conn.execute("""
        SELECT
            SUM(unit_price * qty) / NULLIF(SUM(qty), 0)                          AS avg_list_price,
            SUM(CASE WHEN vat_type = 2 THEN net * 1.07 ELSE net END)
              / NULLIF(SUM(qty), 0)                                               AS avg_effective,
            COUNT(DISTINCT doc_no)                                                AS total_invoices,
            SUM(qty)                                                              AS total_qty
        FROM sales_transactions
        WHERE product_id = ?
          AND qty > 0
          AND unit_price > 0
    """, [product_id]).fetchone()

    conn.close()

    # ── group customers เข้า list_prices ─────────────────────────────────────
    cust_map = defaultdict(list)
    for r in cust_rows:
        key = (r['unit_price'], r['vat_type'])
        cust_map[key].append({
            'customer':       r['customer'],
            'customer_code':  r['customer_code'],
            'invoice_count':  r['invoice_count'],
            'total_qty':      r['total_qty'],
            'last_sale':      r['last_sale'],
            'discounts':      r['discounts'] or '',
        })

    list_prices = []
    for r in price_rows:
        key = (r['unit_price'], r['vat_type'])
        list_prices.append({
            'unit_price':     r['unit_price'],
            'vat_type':       r['vat_type'],
            'invoice_count':  r['invoice_count'],
            'total_qty':      r['total_qty'],
            'last_sale':      r['last_sale'],
            'customer_count': r['customer_count'],
            'customers':      cust_map.get(key, []),
        })

    effective_per_customer = [
        {
            'customer':      r['customer'],
            'customer_code': r['customer_code'],
            'invoice_count': r['invoice_count'],
            'total_qty':     r['total_qty'],
            'avg_effective': r['avg_effective'],
            'last_sale':     r['last_sale'],
        }
        for r in eff_rows
    ]

    return {
        'list_prices':            list_prices,
        'effective_per_customer': effective_per_customer,
        'avg_list_price':         summary['avg_list_price'] or 0.0,
        'avg_effective':          summary['avg_effective'] or 0.0,
        'total_invoices':         summary['total_invoices'] or 0,
        'total_qty':              summary['total_qty'] or 0.0,
    }


def get_customer_unpaid_bills(customer_name):
    """รายการบิลค้างชำระของลูกค้าคนนี้.

    Sourced from express_ar_outstanding (latest snapshot, doc_date >= 2024).
    Customer matched first by customers.name → customer_code, then falls
    back to ao.customer_name LIKE for legacy/typo cases.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            ao.doc_no                    AS doc_base,
            ao.doc_date_iso              AS bill_date,
            COALESCE(c.name, ao.customer_name) AS customer,
            ao.customer_code,
            NULL                         AS vat_type,    -- placeholder; Express totals are already as-billed
            ao.outstanding_amount        AS total_net,
            ao.bill_amount,
            ao.paid_amount,
            ao.is_anomalous,
            ao.has_warning
        FROM express_ar_outstanding ao
        LEFT JOIN customers c ON c.code = ao.customer_code
        WHERE ao.snapshot_date_iso = (SELECT MAX(snapshot_date_iso) FROM express_ar_outstanding)
          AND ao.doc_date_iso >= '2024-01-01'
          AND (
                COALESCE(c.name, '') = ?
             OR ao.customer_name = ?
          )
          AND ao.outstanding_amount > 0
        ORDER BY ao.doc_date_iso DESC
    """, [customer_name, customer_name]).fetchall()
    conn.close()
    return rows


# ── E-commerce Platform SKUs ──────────────────────────────────────────────────

def import_platform_skus(platform, records):
    """Replace all SKUs for a platform with new records. Returns count inserted."""
    conn = get_connection()
    conn.execute("DELETE FROM platform_skus WHERE platform = ?", (platform,))
    count = 0
    for r in records:
        conn.execute("""
            INSERT INTO platform_skus
              (platform, product_id_str, product_name, variation_id, variation_name,
               parent_sku, seller_sku, price, special_price, stock, qty_per_sale, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(platform, variation_id) DO UPDATE SET
              product_name  = excluded.product_name,
              product_id_str= excluded.product_id_str,
              variation_name= excluded.variation_name,
              parent_sku    = excluded.parent_sku,
              seller_sku    = excluded.seller_sku,
              price         = excluded.price,
              special_price = excluded.special_price,
              stock         = excluded.stock,
              raw_json      = excluded.raw_json,
              imported_at   = datetime('now','localtime')
        """, (
            platform,
            r.get('product_id_str'), r.get('product_name', ''),
            r.get('variation_id'),   r.get('variation_name'),
            r.get('parent_sku'),     r.get('seller_sku'),
            r.get('price'),          r.get('special_price'),
            r.get('stock'),          r.get('raw_json'),
        ))
        count += 1
    propagated = _propagate_listings_to_platform_skus(conn, platform)
    conn.commit()
    conn.close()
    return count, propagated


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
            p.sku AS internal_sku, p.product_name AS internal_product_name,
            p.unit_type
        FROM platform_skus ps
        LEFT JOIN products p ON p.id = ps.internal_product_id
        ORDER BY ps.platform, ps.product_name, ps.variation_name
    """).fetchall()
    conn.close()
    return rows


def apply_platform_mapping(rows):
    """
    rows: list of dicts with keys: platform_sku_id, internal_sku, qty_per_sale
    Returns (updated, not_found) counts.
    """
    conn = get_connection()
    updated, not_found = 0, 0
    for r in rows:
        sku_id      = r.get('platform_sku_id')
        int_sku     = r.get('internal_sku')
        qty_per_sale = r.get('qty_per_sale')

        if not sku_id:
            continue

        if int_sku:
            product = conn.execute(
                "SELECT id FROM products WHERE sku = ? AND is_active = 1",
                (int_sku,)
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
    Returns dict: { platform_sku_id -> {suggested_sku, suggested_name, confidence} }
    """
    import re
    import numpy as np
    from rapidfuzz import fuzz
    from rapidfuzz.process import cdist

    conn = get_connection()
    product_list = list(conn.execute(
        "SELECT id, sku, product_name FROM products WHERE is_active = 1"
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
                    'suggested_sku':  matched['sku'],
                    'suggested_name': matched['product_name'],
                    'confidence':     100,
                }
                continue

        score = int(best_score[i])
        if score < 25:
            continue
        product = product_list[best_idx[i]]
        results[sku_id] = {
            'suggested_sku':  product['sku'],
            'suggested_name': product['product_name'],
            'confidence':     score,
        }

    return results


import re as _re_mod
# Noise words to strip before matching (brands, filler marketing words)
_NOISE_WORDS = _re_mod.compile(
    r'\b(sendai|golden\s*lion|ม้าลอดห่วง|สิงห์|คุณภาพดี|อย่างดี|ราคาถูก'
    r'|ของแท้|สินค้าดี|มีให้เลือก|เกรดa|เกรด\s*a|ฟรี|ส่งฟรี|แพ็ค|pack'
    r'|แถมฟรี|โปรโมชั่น|ราคาพิเศษ)\b',
    _re_mod.IGNORECASE
)
_QTY_PREFIX = _re_mod.compile(r'[\(\[【]\s*[\d,./]+\s*[^\)\]】]*[\)\]】]')


def _clean_for_match(text):
    """Strip brand noise & qty-prefixes, return lowercase normalized string."""
    text = _QTY_PREFIX.sub(' ', text or '')
    text = _NOISE_WORDS.sub(' ', text)
    text = text.lower()
    text = _re_mod.sub(r'[()（）【】\[\]\'""]', ' ', text)
    text = _re_mod.sub(r'\s+', ' ', text).strip()
    return text


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


def create_conversion_formula(name, output_product_id, output_qty, inputs, note=''):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO conversion_formulas(name, output_product_id, output_qty, note) VALUES (?,?,?,?)",
        (name, output_product_id, output_qty, note or None)
    )
    formula_id = cur.lastrowid
    for inp in inputs:
        conn.execute(
            "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
            (formula_id, inp['product_id'], inp['quantity'])
        )
    conn.commit()
    conn.close()
    return formula_id


def update_conversion_formula(formula_id, name, output_product_id, output_qty, inputs, note=''):
    conn = get_connection()
    conn.execute(
        "UPDATE conversion_formulas SET name=?, output_product_id=?, output_qty=?, note=? WHERE id=?",
        (name, output_product_id, output_qty, note or None, formula_id)
    )
    conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (formula_id,))
    for inp in inputs:
        conn.execute(
            "INSERT INTO conversion_formula_inputs(formula_id, product_id, quantity) VALUES (?,?,?)",
            (formula_id, inp['product_id'], inp['quantity'])
        )
    conn.commit()
    conn.close()


def delete_conversion_formula(formula_id):
    conn = get_connection()
    conn.execute("DELETE FROM conversion_formula_inputs WHERE formula_id=?", (formula_id,))
    conn.execute("DELETE FROM conversion_formulas WHERE id=?", (formula_id,))
    conn.commit()
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


def run_conversion(formula_id, multiplier, reference_no='', extra_note=''):
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

    output_qty       = formula['output_qty'] * multiplier
    output_unit_cost = total_input_cost / output_qty if output_qty > 0 else 0.0

    # ใช้ reference_no ที่ user ส่งมา หรือ generate ใหม่
    conv_ref = reference_no or f'CONV{formula_id}-{_dt.now().strftime("%Y%m%d%H%M%S")}'

    note_text = f'แปลง: {formula["name"]}'
    if extra_note:
        note_text += f' | {extra_note}'

    for inp in inputs:
        needed = inp['quantity'] * multiplier
        conn.execute(
            "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
            " VALUES (?,?,?,?,?,?)",
            (inp['product_id'], 'OUT', -needed, 'unit', conv_ref, note_text)
        )

    conn.execute(
        "INSERT INTO transactions(product_id, txn_type, quantity_change, unit_mode, reference_no, note)"
        " VALUES (?,?,?,?,?,?)",
        (formula['output_product_id'], 'IN', output_qty, 'unit', conv_ref, note_text)
    )

    # บันทึก conversion cost log (ใช้ตอน recalculate WACC output)
    conn.execute(
        "INSERT INTO conversion_cost_log"
        " (output_product_id, reference_no, event_date, output_qty, total_input_cost, unit_cost)"
        " VALUES (?,?,date('now'),?,?,?)",
        (formula['output_product_id'], conv_ref, output_qty, total_input_cost, output_unit_cost)
    )

    conn.commit()

    # Recalculate WACC for all involved products
    involved = [inp['product_id'] for inp in inputs] + [formula['output_product_id']]
    recalculate_waccs_for_products(involved)

    conn.close()
    return True, f'แปลงสำเร็จ: ได้ {output_qty:,} {formula["output_product_name"]}', {
        'output_qty': output_qty,
        'output_name': formula['output_product_name'],
    }


# ── Customer Master (BSN import) ───────────────────────────────────────────────

def import_customers_from_bsn(customers):
    conn = get_connection()
    inserted = updated = 0
    for c in customers:
        existing = conn.execute("SELECT code FROM customers WHERE code=?", (c['code'],)).fetchone()
        if existing:
            conn.execute("""
                UPDATE customers SET name=?, salesperson=?, zone=?, customer_type=?,
                    address=?, phone=?, tax_id=?, credit_days=?, contact=?,
                    imported_at=datetime('now','localtime')
                WHERE code=?
            """, (c['name'], c['salesperson'], c['zone'], c['customer_type'],
                  c['address'], c['phone'], c['tax_id'], c['credit_days'],
                  c['contact'], c['code']))
            updated += 1
        else:
            conn.execute("""
                INSERT INTO customers(code, name, salesperson, zone, customer_type,
                    address, phone, tax_id, credit_days, contact)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (c['code'], c['name'], c['salesperson'], c['zone'], c['customer_type'],
                  c['address'], c['phone'], c['tax_id'], c['credit_days'], c['contact']))
            inserted += 1
    conn.commit()
    conn.close()
    return inserted, updated


def get_customers_for_map(zone=None, customer_type=None, geocoded_only=False):
    conn = get_connection()
    conds = ['1=1']
    params = []
    if zone:
        conds.append('zone=?'); params.append(zone)
    if customer_type:
        conds.append('customer_type=?'); params.append(customer_type)
    if geocoded_only:
        conds.append('lat IS NOT NULL')
    where = ' AND '.join(conds)
    rows = conn.execute(
        f"SELECT * FROM customers WHERE {where} ORDER BY zone, code",
        params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_customer_geocode(code, lat, lng):
    conn = get_connection()
    conn.execute(
        "UPDATE customers SET lat=?, lng=?, geocoded_at=datetime('now','localtime') WHERE code=?",
        (lat, lng, code)
    )
    conn.commit()
    conn.close()


def get_customer_zones():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT zone FROM customers WHERE zone IS NOT NULL ORDER BY zone"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_customer_types():
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT customer_type FROM customers WHERE customer_type IS NOT NULL ORDER BY customer_type"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_geocode_progress():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    geocoded = conn.execute("SELECT COUNT(*) FROM customers WHERE lat IS NOT NULL").fetchone()[0]
    conn.close()
    return total, geocoded


# ── Supplier List & Summary ───────────────────────────────────────────────────

def get_suppliers(search=None, page=1, per_page=50):
    conn = get_connection()
    conds = ["supplier IS NOT NULL AND supplier != ''"]
    params = []
    if search:
        conds.append("(supplier LIKE ? OR supplier_code LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    where = "WHERE " + " AND ".join(conds)

    total = conn.execute(
        f"SELECT COUNT(DISTINCT supplier) FROM purchase_transactions {where}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(f"""
        SELECT supplier, supplier_code,
               COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               MAX(date_iso)          AS last_date
        FROM purchase_transactions
        {where}
        GROUP BY supplier, supplier_code
        ORDER BY total_net DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, offset]).fetchall()

    conn.close()
    return [dict(r) for r in rows], total


def get_supplier_summary(supplier, date_from=None, date_to=None):
    conn = get_connection()
    conds = ['supplier = ?']
    params = [supplier]
    if date_from:
        conds.append('date_iso >= ?'); params.append(date_from)
    if date_to:
        conds.append('date_iso <= ?'); params.append(date_to)
    where = ' AND '.join(conds)

    summary = conn.execute(f"""
        SELECT COUNT(DISTINCT doc_no) AS doc_count,
               COALESCE(SUM(net), 0)  AS total_net,
               COALESCE(SUM(qty), 0)  AS total_qty,
               MIN(date_iso)          AS first_date,
               MAX(date_iso)          AS last_date
        FROM purchase_transactions
        WHERE {where}
    """, params).fetchone()

    top_products = conn.execute(f"""
        SELECT COALESCE(p.product_name, pt.product_name_raw) AS name,
               COALESCE(p.sku, 0) AS sku,
               p.id AS product_id,
               pt.unit,
               SUM(pt.qty)  AS total_qty,
               SUM(pt.net)  AS total_net,
               COUNT(DISTINCT pt.doc_no) AS doc_count
        FROM purchase_transactions pt
        LEFT JOIN products p ON p.id = pt.product_id
        WHERE {where}
        GROUP BY pt.product_id, pt.product_name_raw
        ORDER BY total_net DESC
        LIMIT 20
    """, params).fetchall()

    monthly = conn.execute(f"""
        SELECT strftime('%Y-%m', date_iso) AS month,
               COUNT(DISTINCT doc_no) AS doc_count,
               SUM(net) AS total_net
        FROM purchase_transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month
    """, params).fetchall()

    docs = conn.execute(f"""
        SELECT date_iso, doc_no,
               COUNT(*) AS line_count,
               SUM(qty) AS total_qty,
               SUM(net) AS total_net
        FROM purchase_transactions
        WHERE {where}
        GROUP BY doc_no
        ORDER BY date_iso DESC, doc_no
        LIMIT 200
    """, params).fetchall()

    supplier_code = conn.execute(
        "SELECT supplier_code FROM purchase_transactions WHERE supplier=? LIMIT 1", [supplier]
    ).fetchone()

    conn.close()
    return {
        'supplier': supplier,
        'supplier_code': supplier_code['supplier_code'] if supplier_code else None,
        'date_from': date_from,
        'date_to': date_to,
        'summary': dict(summary),
        'top_products': [dict(r) for r in top_products],
        'monthly': [dict(r) for r in monthly],
        'docs': [dict(r) for r in docs],
    }


# ── WACC (Weighted Average Cost) ─────────────────────────────────────────────

_WACC_INITIAL_DATE = '2026-03-03'


def recalculate_product_wacc(product_id, conn=None):
    """คำนวณ WACC ใหม่ทั้งหมดสำหรับสินค้า แล้วบันทึกลง product_cost_ledger"""
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    product = conn.execute(
        "SELECT id, unit_type, cost_price FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if not product:
        if close_conn:
            conn.close()
        return 0.0

    cost_price = product['cost_price'] or 0.0
    unit_type  = product['unit_type'] or ''

    # Build purchase_transactions lookup: doc_no → ordered list
    pt_by_docno = defaultdict(list)
    for pt in conn.execute(
        "SELECT doc_no, net, qty FROM purchase_transactions WHERE product_id=? ORDER BY id",
        (product_id,)
    ).fetchall():
        pt_by_docno[pt['doc_no']].append({'net': pt['net'] or 0.0, 'qty': pt['qty'] or 0.0})
    pt_cursor = defaultdict(int)

    # Build conversion_cost_log lookup: reference_no → list
    conv_by_ref = defaultdict(list)
    for row in conn.execute(
        "SELECT reference_no, unit_cost FROM conversion_cost_log WHERE output_product_id=? ORDER BY id",
        (product_id,)
    ).fetchall():
        conv_by_ref[row['reference_no']].append(row['unit_cost'])
    conv_cursor = defaultdict(int)

    # Build set of reference_nos that have ประวัติขาย INs (explicitly "ไม่นับสต็อค")
    # Both the ประวัติขาย IN and its paired BSN ขาย OUT are skipped in WACC calculation
    prathai_refs = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT reference_no FROM transactions"
            " WHERE product_id=? AND note LIKE 'ประวัติขาย%' AND reference_no IS NOT NULL",
            (product_id,)
        ).fetchall()
    }

    # All transactions: INs before OUTs on same day (standard WACC convention)
    txns = conn.execute(
        "SELECT txn_type, quantity_change, reference_no, note, created_at"
        " FROM transactions WHERE product_id=?"
        " ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id",
        (product_id,)
    ).fetchall()

    conn.execute("DELETE FROM product_cost_ledger WHERE product_id=?", (product_id,))

    # Pre-compute non-purchase INs on exactly INITIAL_DATE (stock imports with no note)
    # so the INITIAL ledger entry can show the correct "ยอดยกมา" stock
    initial_date_stock_imports = sum(
        r['quantity_change'] for r in txns
        if r['created_at'][:10] == _WACC_INITIAL_DATE
        and r['txn_type'] == 'IN'
        and (r['note'] or '') not in ('BSN ซื้อ',)
        and not (r['note'] or '').startswith('ประวัติขาย')
        and not (r['note'] or '').startswith('แปลง:')
    )

    current_stock = 0.0
    current_wacc  = 0.0
    initial_done  = False
    entries = []

    for txn in txns:
        date_str  = txn['created_at'][:10]
        qty       = txn['quantity_change']
        ref       = txn['reference_no'] or ''
        note      = txn['note'] or ''

        # Skip ประวัติขาย pairs entirely — both the compensating IN and the paired OUT
        if note.startswith('ประวัติขาย') or (txn['txn_type'] == 'OUT' and ref in prathai_refs):
            continue

        # ── Trigger initial WACC at INITIAL_DATE ──────────────────────────────
        if not initial_done and date_str >= _WACC_INITIAL_DATE:
            initial_done = True
            if cost_price > 0:
                current_wacc = cost_price
                # Include same-day stock imports so the displayed stock reflects reality
                display_stock = current_stock + initial_date_stock_imports
                entries.append(dict(
                    event_type='INITIAL', event_date=_WACC_INITIAL_DATE,
                    qty_change=display_stock, unit_cost=cost_price,
                    stock_after=display_stock, wacc_after=cost_price,
                    reference_no=None,
                    note=f'ยอดยกมา {display_stock:g} {unit_type} @ {cost_price:.2f} บาท/{unit_type}'
                ))

        # ── Purchase (BSN ซื้อ) ───────────────────────────────────────────────
        if txn['txn_type'] == 'IN' and note == 'BSN ซื้อ' and ref in pt_by_docno and qty > 0:
            idx = pt_cursor[ref]
            pts = pt_by_docno[ref]
            if idx < len(pts):
                net = pts[idx]['net']
                pt_cursor[ref] += 1
                if net > 0:
                    unit_cost = net / qty
                    if current_stock < 0:
                        # Negative stock — freeze WACC
                        new_wacc = current_wacc
                    elif current_wacc == 0:
                        # First-time WACC — use purchase price
                        new_wacc = unit_cost
                    elif current_stock == 0:
                        # Zero stock — keep last known WACC
                        new_wacc = current_wacc
                    else:
                        new_wacc = (current_stock * current_wacc + qty * unit_cost) / (current_stock + qty)
                    current_stock += qty
                    current_wacc   = new_wacc
                    entries.append(dict(
                        event_type='PURCHASE', event_date=date_str,
                        qty_change=qty, unit_cost=unit_cost,
                        stock_after=current_stock, wacc_after=current_wacc,
                        reference_no=ref,
                        note=f'ซื้อ {qty:g} {unit_type} @ {unit_cost:.2f} บาท/{unit_type} (net {net:.2f} บาท)'
                    ))
                    continue  # stock already updated above

        # ── Conversion IN ────────────────────────────────────────────────────
        elif txn['txn_type'] == 'IN' and note.startswith('แปลง:') and qty > 0:
            idx = conv_cursor[ref]
            costs = conv_by_ref.get(ref, [])
            if idx < len(costs):
                unit_cost = costs[idx]
                conv_cursor[ref] += 1
                if current_stock < 0:
                    new_wacc = current_wacc
                elif current_wacc == 0:
                    new_wacc = unit_cost
                elif current_stock == 0:
                    # Zero stock — keep last known WACC
                    new_wacc = current_wacc
                else:
                    new_wacc = (current_stock * current_wacc + qty * unit_cost) / (current_stock + qty)
                current_stock += qty
                current_wacc   = new_wacc
                entries.append(dict(
                    event_type='CONVERSION_IN', event_date=date_str,
                    qty_change=qty, unit_cost=unit_cost,
                    stock_after=current_stock, wacc_after=current_wacc,
                    reference_no=ref,
                    note=f'แปลงสินค้า {qty:g} {unit_type} @ {unit_cost:.2f} บาท/{unit_type}'
                ))
                continue

        current_stock += qty

    # ── Handle products that never reached INITIAL_DATE ──────────────────────
    if not initial_done and cost_price > 0:
        current_wacc = cost_price
        entries.append(dict(
            event_type='INITIAL', event_date=_WACC_INITIAL_DATE,
            qty_change=current_stock, unit_cost=cost_price,
            stock_after=current_stock, wacc_after=cost_price,
            reference_no=None,
            note=f'ยอดยกมา {current_stock:g} {unit_type} @ {cost_price:.2f} บาท/{unit_type}'
        ))

    for e in entries:
        conn.execute(
            "INSERT INTO product_cost_ledger"
            " (product_id,event_type,event_date,qty_change,unit_cost,stock_after,wacc_after,reference_no,note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (product_id, e['event_type'], e['event_date'], e['qty_change'],
             e['unit_cost'], e['stock_after'], e['wacc_after'],
             e['reference_no'], e['note'])
        )

    if close_conn:
        conn.commit()
        conn.close()

    return current_wacc


def get_current_wacc(product_id, conn=None):
    """คืน WACC ล่าสุด หรือ cost_price ถ้ายังไม่มีประวัติ"""
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    row = conn.execute(
        "SELECT wacc_after FROM product_cost_ledger WHERE product_id=? ORDER BY event_date DESC, id DESC LIMIT 1",
        (product_id,)
    ).fetchone()

    if row is None:
        # Lazy-calculate on first access
        wacc = recalculate_product_wacc(product_id, conn)
        if close_conn:
            conn.commit()
            conn.close()
        return wacc

    if close_conn:
        conn.close()
    return row['wacc_after']


def get_cost_history(product_id):
    """คืน list ประวัติต้นทุน WACC พร้อม trigger lazy-calc ถ้ายังไม่มี"""
    conn = get_connection()

    exists = conn.execute(
        "SELECT 1 FROM product_cost_ledger WHERE product_id=? LIMIT 1", (product_id,)
    ).fetchone()
    if not exists:
        recalculate_product_wacc(product_id, conn)
        conn.commit()

    rows = conn.execute(
        "SELECT event_type, event_date, qty_change, unit_cost, stock_after, wacc_after, reference_no, note"
        " FROM product_cost_ledger WHERE product_id=? ORDER BY event_date, id",
        (product_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recalculate_waccs_for_products(product_ids):
    """Batch recalculate WACC สำหรับหลายสินค้าใน transaction เดียว"""
    if not product_ids:
        return
    conn = get_connection()
    for pid in set(product_ids):
        recalculate_product_wacc(pid, conn)
    conn.commit()
    conn.close()


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
        SELECT el.*, p.sku, p.product_name
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
        SELECT el.*, p.sku, p.product_name
        FROM ecommerce_listings el
        LEFT JOIN products p ON p.id = el.product_id
        WHERE el.is_ignored = 0 {cond}
        ORDER BY el.platform, el.product_id IS NULL DESC, el.item_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def apply_listing_mapping(records):
    """
    Apply internal_sku → product_id for ecommerce_listings.
    records: list of {listing_id, internal_sku}
    Returns (updated, not_found).
    """
    conn = get_connection()
    updated = not_found = 0
    for r in records:
        lid = r.get('listing_id')
        int_sku = r.get('internal_sku')
        if not lid or not int_sku:
            continue
        product = conn.execute(
            "SELECT id FROM products WHERE sku = ? AND is_active = 1", (int_sku,)
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
    Returns dict: {listing_id -> {suggested_sku, suggested_name, confidence}}
    """
    import numpy as np
    from rapidfuzz import fuzz
    from rapidfuzz.process import cdist

    conn = get_connection()
    product_list = list(conn.execute(
        "SELECT id, sku, product_name FROM products WHERE is_active = 1"
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
                results[lid] = {'suggested_sku': matched['sku'], 'suggested_name': matched['product_name'], 'confidence': 100}
            continue
        score = int(best_score[i])
        if score < 25:
            continue
        product = product_list[best_idx[i]]
        results[lid] = {'suggested_sku': product['sku'], 'suggested_name': product['product_name'], 'confidence': score}
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
    Returns the new product id. Single transaction.
    `edits` dict overrides any field on the staged suggestion."""
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

        # Resolve free-text overrides into FK-target rows where possible.
        # brand: if brand_other_name set and no brand_id → INSERT new brand row
        if not d.get('brand_id') and d.get('brand_other_name'):
            new_brand_name = d['brand_other_name'].strip()
            if new_brand_name:
                # Use display name as both code and name
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

        # color: if color_code_other set and no color_code → INSERT new color row
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

        # packaging: free-text override is stored if dropdown empty
        # (may fail CHECK trigger on products INSERT — admin must extend trigger first)
        if not d.get('packaging') and d.get('packaging_other'):
            d['packaging'] = d['packaging_other'].strip()

        # next sku
        next_sku = conn.execute(
            "SELECT COALESCE(MAX(sku),0)+1 FROM products"
        ).fetchone()[0]

        # Insert product with structured fields
        cur = conn.execute("""
            INSERT INTO products
              (sku, product_name, unit_type, hard_to_sell,
               cost_price, base_sell_price, low_stock_threshold,
               shopee_stock, lazada_stock,
               brand_id, color_code, packaging,
               series, model, size, condition, pack_variant,
               units_per_carton, units_per_box)
            VALUES
              (?, ?, ?, 0, ?, 0.0, 10, 0, 0,
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            next_sku,
            d.get('suggested_name') or d.get('bsn_name'),
            d.get('suggested_unit_type') or 'ตัว',
            d.get('suggested_cost') or 0.0,
            d.get('brand_id'),
            d.get('color_code') or None,
            d.get('packaging') or None,
            d.get('series') or None,
            d.get('model') or None,
            d.get('size') or None,
            d.get('condition') or None,
            d.get('pack_variant') or None,
            d.get('units_per_carton'),
            d.get('units_per_box'),
        ))
        new_pid = cur.lastrowid

        # ensure stock_levels row
        conn.execute(
            "INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)",
            (new_pid,)
        )

        # Upsert mapping (bsn_code → new product)
        conn.execute("""
            INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, is_ignored)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(bsn_code) DO UPDATE SET
                product_id = excluded.product_id,
                is_ignored = 0
        """, (sug['bsn_code'], sug['bsn_name'], new_pid))

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


# ── Catalog data ─────────────────────────────────────────────────────────────

def get_catalog_data(brand_filter=None, category_filter=None,
                     own_brand_only=False, in_stock_only=False):
    """Return catalog cards: list of dicts grouped by category, with families
    and singletons properly merged. Each card has all fields needed for
    catalog rendering (no further DB query needed in template).

    Filters:
      brand_filter:    brand_id list, or None for all
      category_filter: categories.code list, or None for all
      own_brand_only:  True → only Sendai/Golden Lion/A-SPEC
      in_stock_only:   True → only SKUs with quantity > 0

    Cards are grouped by category in returned shape:
      {
        category_name_th: [card1, card2, ...],
        ...
      }
    Each card:
      {
        'card_type':       'family' | 'singleton',
        'family_id':       int | None,
        'display_name':    str,
        'display_format':  str,
        'catalogue_label': str | None,
        'brand_name':      str,
        'category':        str,
        'is_own_brand':    bool,
        'image_path':      str | None,  # primary image
        'skus': [
          {'sku', 'sku_code', 'product_name', 'series', 'size', 'color_th',
           'color_code', 'packaging', 'condition', 'pack_variant',
           'cost_price', 'base_sell_price', 'stock'},
          ...
        ],
      }
    """
    conn = get_connection()

    where = ["p.is_active = 1"]
    params = []
    if own_brand_only:
        where.append("b.is_own_brand = 1")
    if in_stock_only:
        where.append("COALESCE(s.quantity, 0) > 0")
    if brand_filter:
        placeholders = ",".join("?" * len(brand_filter))
        where.append(f"p.brand_id IN ({placeholders})")
        params += list(brand_filter)
    if category_filter:
        placeholders = ",".join("?" * len(category_filter))
        where.append(f"c.code IN ({placeholders})")
        params += list(category_filter)

    where_sql = " AND ".join(where)
    rows = conn.execute(f"""
        SELECT p.id, p.sku, p.sku_code, p.product_name, p.family_id,
               p.series, p.model, p.size, p.color_code, p.packaging,
               p.condition, p.pack_variant, p.sub_category,
               p.base_sell_price, p.cost_price,
               COALESCE(s.quantity, 0) AS stock,
               b.id AS brand_id, b.name AS brand_name, b.is_own_brand,
               b.sort_order AS brand_sort,
               c.code AS category_code, c.name_th AS category_name,
               c.short_code AS cat_short, c.sort_order AS cat_sort,
               cf.name_th AS color_th,
               pf.family_code, pf.display_name AS family_display_name,
               pf.display_format, pf.catalogue_label
          FROM products p
          LEFT JOIN brands b ON b.id = p.brand_id
          LEFT JOIN categories c ON c.id = p.category_id
          LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
          LEFT JOIN stock_levels s ON s.product_id = p.id
          LEFT JOIN product_families pf ON pf.id = p.family_id
         WHERE {where_sql}
         ORDER BY COALESCE(c.sort_order, 999), c.name_th,
                  COALESCE(b.is_own_brand, 0) DESC, b.sort_order, b.name,
                  pf.display_name, p.sub_category, p.size, p.sku
    """, params).fetchall()

    # Pull primary image per family + per sku from product_images (DB)
    image_rows = conn.execute("""
        SELECT family_id, sku_id, image_path
          FROM product_images
         ORDER BY family_id, COALESCE(sort_order, 999)
    """).fetchall()
    family_images = {}
    sku_images = {}
    for r in image_rows:
        if r['family_id'] not in family_images:
            family_images[r['family_id']] = r['image_path']
        if r['sku_id'] and r['sku_id'] not in sku_images:
            sku_images[r['sku_id']] = r['image_path']

    conn.close()

    # Filesystem fallback — scan Design/Catalog/photos/products/ for images
    # placed by match_and_copy_photos.py. Match by category_code/family_code
    # path. Family-shared images (filename starts with family_code prefix)
    # get used as the family hero; sku-specific images (sku_code prefix)
    # override per SKU.
    import os
    PHOTOS_ROOT = os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', '..', 'Design', 'Catalog', 'photos', 'products'
    ))
    fs_family_images = {}    # _safe(family_code) → first image filename
    fs_sku_images = {}       # _safe(sku_code)    → first image filename

    # Mirror scripts/match_and_copy_photos.py::_safe — the photo filename
    # sanitizer is one-way (only '/' → '_'); '_' is a legal sku_code char,
    # so the map is non-injective and CANNOT be reversed. Key the dicts by
    # the sanitized form and apply the SAME forward transform at lookup.
    def _safe(s):
        return (s or "").replace("/", "_")
    if os.path.isdir(PHOTOS_ROOT):
        for cat_dir in os.listdir(PHOTOS_ROOT):
            cat_path = os.path.join(PHOTOS_ROOT, cat_dir)
            if not os.path.isdir(cat_path):
                continue
            for family_dir in os.listdir(cat_path):
                family_path = os.path.join(cat_path, family_dir)
                if not os.path.isdir(family_path):
                    continue
                for fname in sorted(os.listdir(family_path)):
                    if not fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        continue
                    rel_path = f"products/{cat_dir}/{family_dir}/{fname}"
                    base = os.path.splitext(fname)[0]
                    # Family-shared: filename starts with family_dir prefix
                    if base.startswith(family_dir + "_"):
                        if family_dir not in fs_family_images:
                            fs_family_images[family_dir] = rel_path
                    else:
                        # SKU-specific: extract sku_code (everything before _auto_NN)
                        m = re.match(r'^(.+?)_auto_\d+$', base)
                        if m:
                            # m.group(1) is already _safe(sku_code) (the
                            # filename stem); key by it directly — do NOT
                            # reverse-map '_'→'/'.
                            safe_key = m.group(1)
                            if safe_key not in fs_sku_images:
                                fs_sku_images[safe_key] = rel_path

    # Build cards: group by family_id; singletons each get their own card
    family_cards = {}      # family_id → card
    singleton_cards = []   # list of cards
    for r in rows:
        sku_data = {
            'sku': r['sku'],
            'sku_code': r['sku_code'],
            'product_name': r['product_name'],
            'series': r['series'],
            'model': r['model'],
            'size': r['size'],
            'color_th': r['color_th'],
            'color_code': r['color_code'],
            'packaging': r['packaging'],
            'condition': r['condition'],
            'pack_variant': r['pack_variant'],
            'cost_price': r['cost_price'],
            'base_sell_price': r['base_sell_price'],
            'stock': r['stock'],
            'image_path': sku_images.get(r['id']) or fs_sku_images.get(_safe(r['sku_code'])),
        }
        if r['family_id']:
            if r['family_id'] not in family_cards:
                # Resolve family image: DB → filesystem hero → first SKU image
                fam_img = (family_images.get(r['family_id'])
                           or fs_family_images.get(_safe(r['family_code'])))
                family_cards[r['family_id']] = {
                    'card_type': 'family',
                    'family_id': r['family_id'],
                    'family_code': r['family_code'],
                    'display_name': r['family_display_name'] or r['sub_category'],
                    'display_format': r['display_format'] or 'single',
                    'catalogue_label': r['catalogue_label'],
                    'brand_name': r['brand_name'] or '',
                    'is_own_brand': bool(r['is_own_brand']),
                    'category': r['category_name'] or '',
                    'sub_category': r['sub_category'],
                    'image_path': fam_img,
                    'skus': [],
                }
            family_cards[r['family_id']]['skus'].append(sku_data)
            # If family had no image but a sku does, use sku's as family fallback
            if not family_cards[r['family_id']]['image_path']:
                family_cards[r['family_id']]['image_path'] = sku_data['image_path']
        else:
            singleton_cards.append({
                'card_type': 'singleton',
                'family_id': None,
                'family_code': r['sku_code'],
                'display_name': r['product_name'],
                'display_format': 'single',
                'catalogue_label': None,
                'brand_name': r['brand_name'] or '',
                'is_own_brand': bool(r['is_own_brand']),
                'category': r['category_name'] or 'อื่น ๆ',
                'sub_category': r['sub_category'],
                'image_path': sku_data['image_path'],
                'skus': [sku_data],
            })

    # Group by category
    by_cat = {}
    for c in list(family_cards.values()) + singleton_cards:
        cat = c['category'] or 'อื่น ๆ'
        by_cat.setdefault(cat, []).append(c)

    return by_cat
