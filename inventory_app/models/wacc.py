"""WACC (Weighted Average Cost) — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Pure sink: imports nothing from the models package.
"""
from database import get_connection


_WACC_INITIAL_DATE = '2026-03-03'


def recalculate_product_wacc(product_id, conn=None):
    """คำนวณ WACC ใหม่ทั้งหมดสำหรับสินค้า แล้วบันทึกลง product_cost_ledger"""
    close_conn = conn is None
    if conn is None:
        conn = get_connection()

    product = conn.execute(
        "SELECT id, unit_type, cost_price, opening_cost FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if not product:
        if close_conn:
            conn.close()
        return 0.0

    # Seed the ledger's INITIAL ("ยอดยกมา") entry from opening_cost, the immutable cost
    # basis — NOT from cost_price, which this function writes as the live WACC output.
    # Seeding from the output would re-blend past purchases on every recompute (mig 111).
    cost_price = product['opening_cost'] or 0.0
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

    # cost_price is the LIVE WACC output that margin / COGS / quote readers consume.
    # Writing it here makes a new purchase auto-update cost. Only write a real (>0)
    # WACC so a costless product (no purchases yet) keeps its manually-set cost_price
    # instead of being wiped to 0. opening_cost (the seed) is never touched here, which
    # keeps repeated recomputes idempotent.
    if current_wacc and current_wacc > 0:
        # stamp the price-history row so it reads as an automatic WACC sync
        _set_price_change_source(conn, 'wac-sync')
        conn.execute(
            "UPDATE products SET cost_price=? WHERE id=?", (current_wacc, product_id)
        )
        _set_price_change_source(conn, None)

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
