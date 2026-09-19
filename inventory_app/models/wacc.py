"""WACC (Weighted Average Cost) — extracted verbatim from models.py
(behavior-preserving split, Phase 11) — see models/__init__.py's module
docstring for the overall file-split rationale. No behavior changes.

Imports nothing from other domain submodules (products/customers/etc) —
only the shared `_shared` leaf helper (no circular-import risk) plus
stdlib/database, matching the pattern every other Phase-11 submodule uses.
"""
import json
from collections import defaultdict

import actor
from database import get_connection

from ._shared import _set_price_change_source
from .system_alerts import (record_wacc_identity_alert, record_wacc_cost_outlier_alert,
                            record_actor_missing_alert)

# #546 (Put's 2026-09-16 triage, option B): at walk stock 0 the incoming bill
# is taken as the new WACC (see the two `elif current_stock == 0` branches
# below). A bill whose unit cost sits outside this band of the carried cost
# is flagged — never blocked, never altered — via record_wacc_cost_outlier_alert.
_OUTLIER_RATIO_LOW = 1 / 3
_OUTLIER_RATIO_HIGH = 3.0


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

    Carries structured context so whoever OWNS the failed connection can
    persist an actionable alert after rolling back and releasing it. The
    exception class never writes one itself; see the ownership rule in
    models/system_alerts.py.
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


def _flag_if_cost_outlier(conn, *, product_id, reference_no, event_type,
                          prior_cost, incoming_cost):
    """Shared ratio check for both zero-stock branches (#546) — kept in one
    place so PURCHASE and CONVERSION_IN cannot silently diverge on the band.
    `prior_cost` is guaranteed non-zero by both call sites (the `current_wacc
    == 0` branch above each one already claims that case)."""
    if incoming_cost < prior_cost * _OUTLIER_RATIO_LOW or incoming_cost > prior_cost * _OUTLIER_RATIO_HIGH:
        record_wacc_cost_outlier_alert(
            conn, product_id=product_id, reference_no=reference_no,
            event_type=event_type, prior_cost=prior_cost,
            incoming_cost=incoming_cost)


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

    ⚠ RESIDUAL INVARIANT — NULL/NULL provenance is a deliberate escape hatch.
    A row with neither column set is treated as legacy and takes the old
    positional path, which is what keeps pre-mig-148 data working. mig 148
    linked every existing positive 'BSN ซื้อ' IN and _sync_bsn_to_stock links
    every new one, so nothing reaches that path today. But a hand-written
    script that INSERTs a 'BSN ซื้อ' IN without source identity would silently
    reintroduce the original position-pairing bug. So: never create a
    'BSN ซื้อ' IN outside _sync_bsn_to_stock without setting both columns. A
    CHECK constraint or trigger could close this properly later.
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


def recalculate_product_wacc(product_id, conn=None, operation=None):
    """คำนวณ WACC ใหม่ทั้งหมดสำหรับสินค้า แล้วบันทึกลง product_cost_ledger

    `operation` names the business action for the durable alert this function
    records when it OWNS the connection (e.g. 'ratio_replay'). A caller that
    lets this function own the connection must pass its operation here rather
    than record its own alert afterwards — two alerts for one incident
    otherwise, since `operation` is part of the dedupe key.

    When called WITHOUT a connection this owns one, and must therefore clean it
    up on failure as well as on success. The pre-flight below raises
    WaccIdentityError, and the commit/close used to sit only on the success
    path — so every `recalculate_product_wacc(pid)` call site leaked its
    connection when a cost-identity failure fired. Closing the CALLER's
    connection (as the ratio-replay path does) does not help: the connection
    that actually raised is the one opened here.

    When a connection is PASSED IN the caller owns rollback and close; this
    function only propagates.
    """
    if conn is not None:
        return _recalculate_product_wacc(product_id, conn, operation)

    conn = get_connection()
    _closed = False          # bound BEFORE the try: the success path returns
                             # from inside the try, so `finally` must always
                             # find this defined
    try:
        wacc = _recalculate_product_wacc(product_id, conn, operation)
        conn.commit()
        return wacc
    except actor.ActorMissing as e:
        # Raised before anything was written (#590): nothing to undo, but the
        # refusal must reach Put, not only whoever ran the script.
        conn.rollback()
        conn.close()
        _closed = True
        record_actor_missing_alert(e, extra={'product_id': product_id})
        raise
    except WaccIdentityError as e:
        # We own this connection, so we own the durable alert too: roll back
        # and close FIRST, then record on a fresh connection. This is the
        # backstop for every owning entry point — including the lazy readers
        # get_current_wacc()/get_cost_history(), which would otherwise raise a
        # 500 that nobody but the person clicking ever sees.
        conn.rollback()
        conn.close()
        _closed = True
        record_wacc_identity_alert(
            e, operation=operation or e.operation or 'wacc_recalculate')
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if not _closed:
            conn.close()


def _recalculate_product_wacc(product_id, conn, operation=None):
    """The real work. Never commits, never closes — connection lifecycle
    belongs to recalculate_product_wacc above, or to the caller that supplied
    the connection.

    #590: refuses before its first write when nobody is declared on `conn`
    (actor.ActorMissing), and runs inside a scope that adds WHAT ran
    (`wacc:<operation>`) to whoever is already acting, so every audit row the
    rebuild writes names both.
    """
    op = operation or 'recalculate'
    actor.require(conn, f'wacc:{op}')
    with actor.acting_as(detail=f'wacc:{op}'):
        return _rebuild_product_wacc(product_id, conn, op)


def _rebuild_product_wacc(product_id, conn, op):
    product = conn.execute(
        "SELECT id, unit_type, cost_price, opening_cost FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if not product:
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

    ledger_rows_before = conn.execute(
        "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id=?", (product_id,)
    ).fetchone()[0]
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
                        # Zero stock (#546) — the incoming batch IS the whole
                        # stock, so its price IS the weighted average. Flag
                        # (never block) a unit cost far from the carried one.
                        _flag_if_cost_outlier(
                            conn, product_id=product_id, reference_no=ref,
                            event_type='PURCHASE', prior_cost=current_wacc,
                            incoming_cost=unit_cost)
                        new_wacc = unit_cost
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
                # PR #551 review (SHOULD-FIX 1): mirror the PURCHASE branch's
                # `if net > 0:` guard above — a non-positive unit cost must
                # never enter the costing block, or a 0-cost conversion
                # landing at zero stock would drive WACC to 0 (the write
                # guard at the bottom of this function then leaves
                # products.cost_price stale, so the ledger and cost_price
                # silently disagree). Falls through to the plain
                # `current_stock += qty` below, uncosted — same shape as an
                # uncosted (net<=0) purchase.
                if unit_cost > 0:
                    if current_stock < 0:
                        new_wacc = current_wacc
                    elif current_wacc == 0:
                        new_wacc = unit_cost
                    elif current_stock == 0:
                        # Zero stock (#546) — same reasoning as the purchase
                        # branch above: take the conversion's own unit cost, and
                        # flag (never block) if it is far from the carried one.
                        _flag_if_cost_outlier(
                            conn, product_id=product_id, reference_no=ref,
                            event_type='CONVERSION_IN', prior_cost=current_wacc,
                            incoming_cost=unit_cost)
                        new_wacc = unit_cost
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

    # One recalc-event row per rebuild (#590): the ledger has no actor column
    # because every rebuild re-inserts it, so THIS row is what says which
    # operation ran and who set it off. Same transaction as the rebuild.
    new_cost = current_wacc if current_wacc and current_wacc > 0 else product['cost_price']
    conn.execute(
        "INSERT INTO audit_log (table_name, row_id, action, changed_fields,"
        " user, change_source, change_reason)"
        " VALUES ('product_cost_ledger', ?, 'UPDATE', ?,"
        " sendy_actor('who'), sendy_actor('source'), sendy_actor('reason'))",
        (product_id, json.dumps({
            'operation': op,
            'cost_price': [product['cost_price'], new_cost],
            'ledger_rows': [ledger_rows_before, len(entries)],
        }))
    )

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
    _closed = False          # bound before the try — success returns from
                             # inside it, so `finally` must find this defined
    try:
        row = conn.execute(
            "SELECT wacc_after FROM product_cost_ledger WHERE product_id=? ORDER BY event_date DESC, id DESC LIMIT 1",
            (product_id,)
        ).fetchone()

        if row is None:
            # Lazy-calculate on first access
            wacc = recalculate_product_wacc(product_id, conn, operation='lazy_read')
            if close_conn:
                conn.commit()
            return wacc

        return row['wacc_after']
    except actor.ActorMissing as e:
        # Same ownership rule as below: only the owner of the connection alerts.
        if close_conn:
            conn.rollback()
            conn.close()
            _closed = True
            record_actor_missing_alert(e, extra={'product_id': product_id})
        raise
    except WaccIdentityError as e:
        # Only alert when we OWN the connection. With a caller-supplied one we
        # cannot roll back or close, so the alert is the owner's to record —
        # otherwise a nested caller (run_conversion's input-cost loop) would
        # raise a duplicate for the same incident.
        if close_conn:
            conn.rollback()
            conn.close()
            _closed = True
            record_wacc_identity_alert(e, operation='wacc_lazy_read')
        raise
    except Exception:
        if close_conn:
            conn.rollback()
        raise
    finally:
        if close_conn and not _closed:
            conn.close()


def get_cost_history(product_id):
    """คืน list ประวัติต้นทุน WACC พร้อม trigger lazy-calc ถ้ายังไม่มี"""
    conn = get_connection()

    # try/finally for the same reason as get_current_wacc: this wrapper owns
    # the connection but passes it in, so a WaccIdentityError from the lazy
    # recalculate would otherwise skip the close() at the end and leak it.
    _closed = False
    try:
        exists = conn.execute(
            "SELECT 1 FROM product_cost_ledger WHERE product_id=? LIMIT 1", (product_id,)
        ).fetchone()
        if not exists:
            recalculate_product_wacc(product_id, conn, operation='lazy_read')
            conn.commit()

        rows = conn.execute(
            "SELECT event_type, event_date, qty_change, unit_cost, stock_after, wacc_after, reference_no, note"
            " FROM product_cost_ledger WHERE product_id=? ORDER BY event_date, id",
            (product_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    except actor.ActorMissing as e:
        conn.rollback()
        conn.close()
        _closed = True
        record_actor_missing_alert(e, extra={'product_id': product_id})
        raise
    except WaccIdentityError as e:
        # This wrapper always owns its connection, so it owns the alert too.
        # Without this the /products/<id>/cost-history page just 500s and the
        # failure is seen only by whoever happened to click it.
        conn.rollback()
        conn.close()
        _closed = True
        record_wacc_identity_alert(e, operation='wacc_lazy_read')
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if not _closed:
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
            recalculate_product_wacc(pid, conn, operation=operation)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
