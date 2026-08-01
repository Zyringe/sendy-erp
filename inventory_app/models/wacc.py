"""WACC (Weighted Average Cost) — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Imports nothing from other domain submodules (products/customers/etc) —
only the shared `_shared` leaf helper (no circular-import risk) plus
stdlib/database, matching the pattern every other Phase-11 submodule uses.
"""
from collections import defaultdict

from database import get_connection

from ._shared import _set_price_change_source


_WACC_INITIAL_DATE = '2026-03-03'


class WaccIdentityError(Exception):
    """A ledger row's source-line provenance (mig 148) cannot be resolved.

    Raised BEFORE anything is deleted or written, so a failure leaves
    product_cost_ledger and products.cost_price exactly as they were.

    This must never degrade into "skip the row and carry on": the purchase
    branch not costing a row does NOT mean the row is ignored — control falls
    through to `current_stock += qty`, so the quantity is added UNCOSTED and
    the incomplete result is then written to products.cost_price. That is a
    silently wrong cost basis feeding margin, COGS, pricing and conversions.

    Carries structured context so the caller (PR3) can persist an actionable
    alert after it has rolled back and released its transaction. This class
    NEVER writes an alert itself — see models/wacc.py's callers.
    """

    def __init__(self, reason, *, product_id=None, reference_no=None,
                 source_bsn_code=None, source_line_seq=None, operation=None):
        self.reason = reason
        self.product_id = product_id
        self.reference_no = reference_no
        self.source_bsn_code = source_bsn_code
        self.source_line_seq = source_line_seq
        self.operation = operation
        super().__init__(
            f"{reason} (product_id={product_id}, reference_no={reference_no!r}, "
            f"source_bsn_code={source_bsn_code!r}, source_line_seq={source_line_seq!r})"
        )

    def context(self):
        """Incident identity + diagnostics, for the PR3 alert writer."""
        return {
            'reason': self.reason,
            'product_id': self.product_id,
            'reference_no': self.reference_no,
            'source_bsn_code': self.source_bsn_code,
            'source_line_seq': self.source_line_seq,
            'operation': self.operation,
        }


def preflight_source_identity(conn, product_id, operation=None):
    """Validate one product's source-line provenance. READ-ONLY.

    Raises WaccIdentityError if any linked ledger row has partial provenance,
    a duplicated purchase business key, or a link to a purchase line that no
    longer exists.

    Exposed separately from recalculate_product_wacc so a BATCH caller can
    validate EVERY affected product before mutating ANY of them. Per-product
    validation alone does not protect a loop: without this, product A can be
    rebuilt and committed before product B's failure is discovered, leaving an
    iteration-order-dependent subset of the batch recalculated.
    """
    pt_idents = set()
    dup_identities = set()
    for pt in conn.execute(
        "SELECT doc_no, bsn_code, line_seq FROM purchase_transactions"
        " WHERE product_id=? ORDER BY id", (product_id,)
    ).fetchall():
        if pt['bsn_code'] is None:
            continue
        key = (pt['doc_no'], pt['bsn_code'], pt['line_seq'])
        if key in pt_idents:
            dup_identities.add(key)
        pt_idents.add(key)

    for txn in conn.execute(
        "SELECT reference_no, source_bsn_code, source_line_seq"
        " FROM transactions WHERE product_id=?", (product_id,)
    ).fetchall():
        code, seq = txn['source_bsn_code'], txn['source_line_seq']
        if (code is None) != (seq is None):
            raise WaccIdentityError(
                'partial source-line provenance (exactly one column set)',
                product_id=product_id, reference_no=txn['reference_no'],
                source_bsn_code=code, source_line_seq=seq, operation=operation)
        if code is None:
            continue  # legacy row → positional path, unchanged
        key = (txn['reference_no'], code, seq)
        if key in dup_identities:
            raise WaccIdentityError(
                'duplicate purchase business key — cannot choose a cost',
                product_id=product_id, reference_no=txn['reference_no'],
                source_bsn_code=code, source_line_seq=seq, operation=operation)
        if key not in pt_idents:
            raise WaccIdentityError(
                'linked purchase line no longer exists',
                product_id=product_id, reference_no=txn['reference_no'],
                source_bsn_code=code, source_line_seq=seq, operation=operation)


def preflight_batch(conn, product_ids, operation=None):
    """Validate a whole batch before any of it is mutated. READ-ONLY."""
    for pid in product_ids:
        preflight_source_identity(conn, pid, operation=operation)


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

    # Build purchase_transactions lookups.
    #   pt_by_docno  — legacy positional list, still used by rows whose
    #                  provenance is NULL/NULL (pre-mig-148 or non-BSN).
    #   pt_by_ident  — (doc_no, bsn_code, line_seq) → row, the mig-148 identity.
    #                  Direct resolution: no cursor, no ordering dependence.
    # Duplicate business keys are detected by preflight_source_identity below,
    # which runs before anything is mutated — so this map can assume its keys
    # are unambiguous by the time the walk reads them.
    pt_by_docno = defaultdict(list)
    pt_by_ident = {}
    for pt in conn.execute(
        "SELECT doc_no, bsn_code, line_seq, net, qty"
        " FROM purchase_transactions WHERE product_id=? ORDER BY id",
        (product_id,)
    ).fetchall():
        entry = {'net': pt['net'] or 0.0, 'qty': pt['qty'] or 0.0}
        pt_by_docno[pt['doc_no']].append(entry)
        if pt['bsn_code'] is not None:
            pt_by_ident[(pt['doc_no'], pt['bsn_code'], pt['line_seq'])] = entry
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
        "SELECT txn_type, quantity_change, reference_no, note, created_at,"
        " source_bsn_code, source_line_seq"
        " FROM transactions WHERE product_id=?"
        " ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id",
        (product_id,)
    ).fetchall()

    # ── PRE-FLIGHT: validate before we mutate anything ──────────────────────
    # Everything below this point deletes and rewrites the cost ledger, so any
    # unresolvable provenance has to be caught HERE. Raising after the DELETE
    # would leave the product with a partially rebuilt ledger; skipping the row
    # instead would fall through to `current_stock += qty` and write an
    # UNCOSTED quantity into products.cost_price.
    preflight_source_identity(conn, product_id)

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
        if txn['txn_type'] == 'IN' and note == 'BSN ซื้อ' and qty > 0:
            pt_row = None
            if txn['source_bsn_code'] is not None:
                # Linked (mig 148): resolve the exact line this IN came from.
                # Pre-flight has already proven this key resolves uniquely, so
                # a reissued transactions.id cannot change which net we take.
                pt_row = pt_by_ident.get(
                    (ref, txn['source_bsn_code'], txn['source_line_seq']))
            elif ref in pt_by_docno:
                # Legacy row with no provenance — the original positional
                # cursor, byte-for-byte unchanged.
                idx = pt_cursor[ref]
                pts = pt_by_docno[ref]
                if idx < len(pts):
                    pt_row = pts[idx]
                    pt_cursor[ref] += 1
            if pt_row is not None:
                net = pt_row['net']
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

    # try/finally: the lazy recalculate below can raise WaccIdentityError, and
    # this wrapper OWNS the connection it created while passing it in — so
    # recalculate_product_wacc will not close it. Without this the commit/close
    # sat only on the success path and a raise leaked the connection.
    try:
        row = conn.execute(
            "SELECT wacc_after FROM product_cost_ledger WHERE product_id=? ORDER BY event_date DESC, id DESC LIMIT 1",
            (product_id,)
        ).fetchone()

        if row is None:
            # Lazy-calculate on first access
            wacc = recalculate_product_wacc(product_id, conn)
            if close_conn:
                conn.commit()
            return wacc

        return row['wacc_after']
    except Exception:
        if close_conn:
            conn.rollback()
        raise
    finally:
        if close_conn:
            conn.close()


def get_cost_history(product_id):
    """คืน list ประวัติต้นทุน WACC พร้อม trigger lazy-calc ถ้ายังไม่มี"""
    conn = get_connection()

    # try/finally for the same reason as get_current_wacc: this wrapper owns
    # the connection but passes it in, so a WaccIdentityError from the lazy
    # recalculate would otherwise skip the close() at the end and leak it.
    try:
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
        return [dict(r) for r in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recalculate_waccs_for_products(product_ids, operation=None):
    """Batch recalculate WACC สำหรับหลายสินค้าใน transaction เดียว

    Pre-flights the WHOLE batch before mutating any of it, so a later product's
    unresolvable provenance cannot leave an earlier product's ledger rebuilt.
    On failure nothing is committed and the connection is closed — previously
    this function had no exception handling at all, so a mid-loop raise left
    pending changes on a leaked connection.
    """
    if not product_ids:
        return
    pids = sorted(set(product_ids))
    conn = get_connection()
    try:
        preflight_batch(conn, pids, operation=operation)
        for pid in pids:
            recalculate_product_wacc(pid, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
