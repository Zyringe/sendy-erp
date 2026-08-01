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

# หน้าร้าน customer codes whose synced sales ALSO decrement platform_skus.stock
# (see _sync_bsn_to_stock below). ONE definition, imported — ecommerce_overview
# must know exactly which sales are already baked into platform_skus.stock so
# its sold_since adjustment does not deduct the same sale a second time.
# ⚠ หน้าร้านB (the closed second Shopee shop) is deliberately ABSENT: its sales
# book to Shopee but were never deducted here, so they must still be adjusted.
PLATFORM_STOCK_DEDUCT_CUSTOMERS = {
    'หน้าร้านL': 'lazada',
    'หน้าร้านS': 'shopee',
}

# Unit classes for the pack/loose guard (decisions/log.md 2026-07-26/27):
# a bill in a piece unit must never deduct a pack-stocked SKU fractionally,
# and a product with an active pack/unpack pair must never get a cross-unit
# ratio in either direction — split mapping is the fix there.
_PIECE_UNITS = frozenset(('ตัว', 'อัน', 'ชิ้น'))
_PACK_UNITS = frozenset(('แผง', 'แพ็ค'))

# Ledger note prefixes a BSN sync ever writes: the 'BSN ขาย/ซื้อ(-คืน)' rows
# plus the paired "ประวัติขาย (ไม่นับสต็อค)" IN that _sync_bsn_to_stock creates
# alongside every history_import OUT (net-0 pairing so a historical bill never
# moves real stock). Both must be wiped together before a resync — deleting
# only the 'BSN%' half would duplicate the pairing once the resync recreates
# it fresh. Lives here because _sync_bsn_to_stock is what writes them;
# models/mapping.py imports it for the same rebuild.
_BSN_LEDGER_NOTE_PATTERNS = ("BSN%", "ประวัติขาย%")


def cross_unit_hazard(conn, product_id, bsn_unit):
    """None when (product, bsn_unit) may take a unit_conversions ratio.
    {'kind': 'pair', 'partner_id', 'partner_name', 'partner_unit'} when the
    product has an active [แพ็ค]/[แกะ] pair whose partner's unit_type equals
    the (normalized) bsn_unit — ANY ratio is forbidden, map the unit to the
    partner SKU instead.
    {'kind': 'pack_piece', 'product_unit'} when bsn_unit is a piece unit and
    the product's unit_type is a pack unit with NO such pair — only ratio 1
    (unit alias) is allowed; a real loose sale needs a loose SKU + split
    mapping."""
    norm = bsn_units.normalize_unit(bsn_unit) or ''
    product = conn.execute(
        "SELECT product_name, unit_type FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None or norm == (product['unit_type'] or '').strip():
        return None

    # Pair check, both directions: product as the OUTPUT of a pack/unpack
    # half (partner = its single input) and product as the (single) INPUT
    # of a pack/unpack half (partner = that half's output).
    partner_ids = set()
    for f in conn.execute("""
        SELECT id FROM conversion_formulas
         WHERE is_active = 1 AND output_product_id = ?
           AND (name LIKE '[แพ็ค]%' OR name LIKE '[แกะ]%')
    """, (product_id,)).fetchall():
        ins = conn.execute(
            "SELECT product_id FROM conversion_formula_inputs WHERE formula_id = ?",
            (f['id'],)).fetchall()
        if len(ins) == 1:
            partner_ids.add(ins[0]['product_id'])
    for f in conn.execute("""
        SELECT cf.id, cf.output_product_id
          FROM conversion_formulas cf
          JOIN conversion_formula_inputs cfi ON cfi.formula_id = cf.id
         WHERE cf.is_active = 1 AND cfi.product_id = ?
           AND (cf.name LIKE '[แพ็ค]%' OR cf.name LIKE '[แกะ]%')
    """, (product_id,)).fetchall():
        ins = conn.execute(
            "SELECT product_id FROM conversion_formula_inputs WHERE formula_id = ?",
            (f['id'],)).fetchall()
        if len(ins) == 1:
            partner_ids.add(f['output_product_id'])

    for pid in sorted(partner_ids):
        partner = conn.execute(
            "SELECT product_name, unit_type FROM products WHERE id = ?", (pid,)
        ).fetchone()
        if partner and (partner['unit_type'] or '').strip() == norm:
            return {'kind': 'pair', 'partner_id': pid,
                    'partner_name': partner['product_name'],
                    'partner_unit': partner['unit_type']}

    if norm in _PIECE_UNITS and (product['unit_type'] or '').strip() in _PACK_UNITS:
        return {'kind': 'pack_piece', 'product_unit': product['unit_type']}

    return None


def _product_name(conn, product_id):
    row = conn.execute(
        "SELECT product_name FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    return row['product_name'] if row else None


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

    # ORDER BY id is load-bearing on a REPLAY (update_unit_conversion_ratio /
    # repoint_bsn_code delete a product's ledger and re-post it): recalculate_
    # product_wacc pairs the Nth 'BSN ซื้อ' IN under a doc_no with the Nth
    # purchase_transactions row of that doc, by position, so the posting order
    # has to match the source id order rather than whatever a bare table scan
    # happens to hand back.
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE product_id IS NOT NULL AND synced_to_stock = 0"
        f" ORDER BY id"
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
                platform = PLATFORM_STOCK_DEDUCT_CUSTOMERS.get(customer)

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
    # Flag rows whose bsn_unit is still an UNKNOWN acronym (import already
    # normalises known ones) so the UI can ask Put for the full unit name.
    out = []
    for r in rows:
        d = dict(r)
        d['is_acronym'] = not bsn_units.is_known(d['bsn_unit'])
        d['hazard'] = cross_unit_hazard(conn, d['product_id'], d['bsn_unit'])
        out.append(d)
    conn.close()
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
    saved = 0
    blocked = []
    for item in items:
        hazard = cross_unit_hazard(conn, item['product_id'], item['bsn_unit'])
        if hazard is not None and (hazard['kind'] == 'pair' or float(item['ratio']) != 1):
            blocked.append(dict(hazard, product_id=item['product_id'],
                                 bsn_unit=item['bsn_unit'],
                                 product_name=_product_name(conn, item['product_id'])))
            continue
        conn.execute("""
            INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
            VALUES (?, ?, ?)
            ON CONFLICT(product_id, bsn_unit) DO UPDATE SET ratio = excluded.ratio
        """, (item['product_id'], item['bsn_unit'], item['ratio']))
        saved += 1
    # After saving, re-run sync for both tables
    if saved:
        _sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
        _sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')
    conn.commit()
    conn.close()
    return {'saved': saved, 'blocked': blocked}


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


def _synced_source_rows(conn, product_id):
    """How many BSN source rows for `product_id` currently hold a ledger
    movement (synced_to_stock=1), across both tables."""
    return sum(
        conn.execute(
            f"SELECT COUNT(*) FROM {table}"
            f" WHERE product_id=? AND synced_to_stock=1", (product_id,)
        ).fetchone()[0]
        for table in ('sales_transactions', 'purchase_transactions'))


def update_unit_conversion_ratio(product_id, bsn_unit, new_ratio):
    """อัปเดต ratio ที่มีอยู่แล้ว แล้ว re-sync BSN transactions ที่เกี่ยวข้อง

    Returns {'ok': True}, {'blocked': <hazard>} when the pack/loose guard
    refuses the ratio, or {'error': <msg>} when the rebuild could not replay
    every movement it removed (nothing is committed in that case)."""
    conn = get_connection()

    hazard = cross_unit_hazard(conn, product_id, bsn_unit)
    if hazard is not None and (hazard['kind'] == 'pair' or float(new_ratio) != 1):
        blocked = dict(hazard, product_id=product_id, bsn_unit=bsn_unit,
                       product_name=_product_name(conn, product_id))
        conn.close()
        return {'blocked': blocked}

    # Update ratio
    conn.execute("""
        UPDATE unit_conversions SET ratio=? WHERE product_id=? AND bsn_unit=?
    """, (new_ratio, product_id, bsn_unit))

    # Rebuild this product's WHOLE BSN ledger — the same Reconciliation
    # Procedure mapping.repoint_bsn_code runs (reset synced → delete every
    # ledger row a BSN sync wrote → re-sync → recompute WACC).
    #
    # Per PRODUCT, not per (unit, doc): the old code deleted ledger rows by
    # (product_id, doc_no) but reset synced_to_stock for `bsn_unit` alone, so a
    # doc carrying this product in TWO units lost the other unit's movement for
    # good — and the stock_levels re-derivation that used to sit at the end hid
    # the loss by recomputing stock from the truncated ledger, leaving nothing
    # for a drift check to find. Deleting and replaying the SAME set keeps the
    # two halves symmetric by construction.
    synced_before = _synced_source_rows(conn, product_id)
    for table in ('sales_transactions', 'purchase_transactions'):
        conn.execute(f"UPDATE {table} SET synced_to_stock=0 WHERE product_id=?",
                     (product_id,))
    conn.execute(
        "DELETE FROM transactions WHERE product_id=? AND ({})".format(
            " OR ".join("note LIKE ?" for _ in _BSN_LEDGER_NOTE_PATTERNS)),
        (product_id, *_BSN_LEDGER_NOTE_PATTERNS))

    _sync_bsn_to_stock(conn, 'sales_transactions', 'sales')
    _sync_bsn_to_stock(conn, 'purchase_transactions', 'purchase')

    # Every row that was synced must have re-synced. A shortfall means some
    # row's ratio is gone (the ad-hoc scripts under scripts/ can delete
    # unit_conversions rows) and its movement would silently vanish — bail
    # WITHOUT committing rather than lose it.
    if _synced_source_rows(conn, product_id) < synced_before:
        conn.close()          # nothing committed → sqlite rolls the lot back
        return {'error': 'อัปเดตไม่สำเร็จ: มีแถว BSN ของสินค้านี้ที่หา ratio '
                         'ไม่เจอ (หน่วยถูกลบไป) ระบบยกเลิกการแก้ไขทั้งหมด '
                         'เพื่อไม่ให้ยอดสต็อกหาย'}

    # stock_levels needs no manual rebuild here: the mig-080 triggers maintain
    # it through the delete + re-sync above (same reason repoint_bsn_code does
    # no stock surgery of its own).
    conn.commit()

    # WACC: recalculate after ratio change
    recalculate_product_wacc(product_id)

    conn.close()
    return {'ok': True}


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
        return False
    conn = get_connection()
    hazard = cross_unit_hazard(conn, product_id, bsn_unit)
    if hazard is not None and (hazard['kind'] == 'pair' or float(ratio) != 1):
        conn.close()
        return False
    conn.execute("""
        INSERT INTO unit_conversions (product_id, bsn_unit, ratio)
        VALUES (?, ?, ?)
        ON CONFLICT(product_id, bsn_unit) DO UPDATE SET
            ratio = excluded.ratio
    """, (product_id, bsn_unit, float(ratio)))
    conn.commit()
    conn.close()
    return True
