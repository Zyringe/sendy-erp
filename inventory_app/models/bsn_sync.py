"""BSN sync + unit-conversion helpers — extracted verbatim from models.py
(behavior-preserving split, Phase 12) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Imports `recalculate_product_wacc` from `.wacc` (not on the brief's original
edge list, but forced by the verbatim body: `update_unit_conversion_ratio`
calls it directly after a ratio change) — acyclic, flagged in the Phase 12
report.
"""
from database import get_connection
import bsn_units

from .wacc import recalculate_product_wacc


def to_base_units(quantity: int, mode: str, product) -> int:
    if mode == 'carton':
        return quantity * (product['units_per_carton'] or 1)
    if mode == 'box':
        return quantity * (product['units_per_box'] or 1)
    return quantity


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
        # Round to 4 dp: qty × a fractional ratio (e.g. 6 × 0.1) yields IEEE-754
        # noise (0.6000000000000001) that the stock triggers then accumulate.
        # Finest real movement is 0.1, so 4 dp is lossless. Mirrors mig 092's
        # trigger-level ROUND (belt-and-suspenders — keeps the ledger rows clean
        # too, so direct SUM(quantity_change) audits don't drift either).
        return round(qty * row['ratio'], 4)
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
            # Purchase returns (GR = ใบลดหนี้ / goods returned to supplier)
            # REDUCE stock — they must post OUT, not IN, and must NOT be
            # averaged into WACC as a purchase lot. Express prints GR qty as a
            # positive number but the GR doc-type is always a credit/return
            # (the purchase-history parser's validate() subtracts every GR row
            # from the grand total). We detect it by the GR doc-no prefix —
            # the only signal preserved on the stored row — and tag the txn
            # 'BSN ซื้อ-คืน' so recalculate_product_wacc's purchase branch
            # (note == 'BSN ซื้อ') skips it; the generic OUT path then lowers
            # stock at the current average cost, the correct treatment.
            # Sales returns (SR = ใบลดหนี้ / customer returned goods) are the
            # mirror image: goods come BACK, so they must post IN (raise stock),
            # not OUT. Express prints SR qty as a positive number; the SR
            # doc-type is always a customer return. Detect by the SR doc-no
            # prefix (the only signal on the stored sales row — sales_transactions
            # has no return_flag column) and tag 'BSN ขาย-คืน' so the WACC
            # purchase branch (note == 'BSN ซื้อ') skips it; the generic IN path
            # raises stock at the current average cost.
            is_purchase_return = (
                file_type == 'purchase' and (row['doc_no'] or '').startswith('GR')
            )
            is_sales_return = (
                file_type == 'sales' and (row['doc_no'] or '').startswith('SR')
            )
            if is_purchase_return:
                row_txn_type = 'OUT'
                change = -base_qty
                note = 'BSN ซื้อ-คืน'
            elif is_sales_return:
                row_txn_type = 'IN'
                change = base_qty
                note = 'BSN ขาย-คืน'
            else:
                row_txn_type = txn_type
                change = base_qty if txn_type == 'IN' else -base_qty
                label = 'ซื้อ' if file_type == 'purchase' else 'ขาย'
                note = f'BSN {label}'
            conn.execute("""
                INSERT INTO transactions
                    (product_id, txn_type, quantity_change, unit_mode,
                     reference_no, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                row['product_id'], row_txn_type, change, 'unit',
                row['doc_no'],
                note,
                row['date_iso'] + ' 00:00:00',
            ))

            # Deduct online stock for Shopee/Lazada store customers — only for
            # genuine sales OUT, never for a sales return (a return would have to
            # ADD platform stock back, not deduct; we leave platform stock to the
            # marketplace sync rather than guess on returns).
            if txn_type == 'OUT' and not is_sales_return:
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
                            # Remaining base qty is under half a platform unit —
                            # nothing meaningful left to deduct from this or any
                            # later (smaller-stock) SKU in the ORDER BY stock DESC
                            # list, so stop instead of forcing a whole unit off.
                            break
                        conn.execute("""
                            UPDATE platform_skus
                            SET stock = MAX(0, stock - ?)
                            WHERE id = ?
                        """, (platform_deduct, sku['id']))
                        remaining -= platform_deduct * qps

            # history_import: สร้าง txn ตรงข้ามคู่กันเพื่อไม่ให้กระทบสต็อคปัจจุบัน
            # ต้อง reverse แถวจริง (row_txn_type/change) ไม่ใช่สมมติว่าเป็นขายเสมอ —
            # แถว SR ในไฟล์ history โพสต์ IN เป็น primary leg แล้ว (ดูด้านบน),
            # ถ้า compensator ยัง +IN ซ้ำจะกลายเป็น +2×qty แทนที่จะหักล้างเป็น 0
            if row['batch_id'] == 'history_import' and txn_type == 'OUT':
                reverse_txn_type = 'OUT' if row_txn_type == 'IN' else 'IN'
                conn.execute("""
                    INSERT INTO transactions
                        (product_id, txn_type, quantity_change, unit_mode,
                         reference_no, note, created_at)
                    VALUES (?, ?, ?, 'unit', ?, ?, ?)
                """, (
                    row['product_id'], reverse_txn_type, -change,
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
        sql += " AND (p.product_name LIKE ? OR CAST(p.id AS TEXT) LIKE ?)"
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


def dismiss_pending_unit_conversion(product_id: int, bsn_unit: str) -> int:
    """Delete all synced_to_stock=0 rows for (product_id, bsn_unit) from both
    ledger tables. Used when the team entered a wrong unit and the rows are
    stale — they have never touched stock so deletion is safe."""
    conn = get_connection()
    deleted = 0
    for table in ('sales_transactions', 'purchase_transactions'):
        cur = conn.execute(
            f"DELETE FROM {table} WHERE product_id=? AND unit=? AND synced_to_stock=0",
            (product_id, bsn_unit),
        )
        deleted += cur.rowcount
    conn.commit()
    conn.close()
    return deleted


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
        where = "WHERE p.product_name LIKE ? OR CAST(p.id AS TEXT) LIKE ?"
        params += [f"%{search}%", f"%{search}%"]

    sql = f"""
        SELECT uc.id, uc.product_id, uc.bsn_unit, uc.ratio,
               p.product_name, p.unit_type,
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
