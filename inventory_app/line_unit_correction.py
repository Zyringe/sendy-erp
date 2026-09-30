"""แก้หน่วยบรรทัด (line unit correction), #692, ADR 0021.

An admin corrects the หน่วย of ONE sales line. Express keeps its own value, so
the daily zip must read that line as unchanged for as long as Express still
says what it said when the correction was made. Every rule about that lives
here: the importer, the drift scan, the guarded writers and (PR-2) the routes
only call in.

A correction is keyed on the line key (doc_no, bsn_code), never the row id: a
changed line is DELETE + INSERT with a new id (models/imports.py).

States: active -> cancelled (admin) | retired (importer). The table CHECKs hold
the shape (mig 198); a new correction on the same line is a new row.

`models.*` is imported inside the functions that need it: bsn_sync, mapping
and reconcile import this module for `blocking`, so a top-level import of them
here would be a cycle.
"""
from dataclasses import dataclass
from typing import Optional

import bsn_units
import database
import document_kind
import unit_conversion

KEEP = 'keep'
EXPRESS_AGREES = 'express_agrees'
EXPRESS_CHANGED = 'express_changed'
EXPRESS_REMOVED = 'express_removed'

MODES = ('hold', 'move')
MIN_REASON = 12
EPSILON = 1e-9

# Must not start with `BSN` or `ประวัติขาย`: the orphan sweep alerts on those,
# and pass 2 of the importer owns the exact `BSN ขาย` notes.
OFFSET_NOTE_PREFIX = 'แก้หน่วยบรรทัด '
ADJUST_UNKNOWN_TH = 'ระบบไม่รู้ว่ารายการนี้เป็นการตั้งยอดหรือไม่'

_SALES_LEDGER_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')

_RETIRE_CAUSE_TH = {
    EXPRESS_CHANGED: 'Express แก้บรรทัดนี้ ระบบจึงใช้ค่าของ Express',
    EXPRESS_REMOVED: 'Express ลบบรรทัดนี้แล้ว',
    EXPRESS_AGREES: 'Express แก้หน่วยตรงกับที่แก้ไว้แล้ว (รายการปรับยอดคงเหลือคงไว้ตามเดิม)',
}


class Refused(ValueError):
    """The correction cannot be made, cancelled or previewed. `code` names the
    rule, the message is Thai and is shown to the admin as it is."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Effect:
    """What `apply` would do to one line, measured and not yet written."""
    row_id: int
    doc_no: str
    bsn_code: str
    doc_base: str
    product_id: int
    date_iso: str
    qty: float
    unit_price: Optional[float]
    net: Optional[float]
    express_unit_raw: str
    corrected_unit: str
    ledger_txn_id: int
    old_effect: float
    new_effect: float
    stock_now: float
    stock_after_hold: float
    stock_after_move: float
    proposed_mode: str
    adjust_seen: Optional[dict]
    exposure_before: dict
    exposure_after: dict


def _dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _norm(conn, unit):
    return bsn_units.normalize_unit(unit or '', conn=conn) or ''


def _stock(conn, product_id):
    row = conn.execute(
        "SELECT quantity FROM stock_levels WHERE product_id=?", (product_id,)).fetchone()
    return row[0] if row else 0


def _line_label(doc_no, bsn_code):
    return f'{doc_no} ({bsn_code})'


def allowed_units(conn, product_id):
    """Units this product's line may be corrected TO: the base unit plus every
    stored ratio spelling, each only when it is a fixed point of the unit map.
    The stock writer and COGS look the STORED spelling up exactly, so a
    spelling the map would rewrite cannot be stored."""
    row = conn.execute(
        "SELECT unit_type FROM products WHERE id=?", (product_id,)).fetchone()
    if row is None:
        return []
    candidates = [row[0]] + [
        unit for (_pid, unit), ratio
        in unit_conversion.exact_ratios(conn, [product_id]).items() if ratio > 0]
    out = []
    for unit in candidates:
        if unit and unit not in out and bsn_units.normalize_unit(unit, conn=conn) == unit:
            out.append(unit)
    return out


def _ledger_row(conn, doc_no, product_id):
    rows = conn.execute(
        "SELECT id, quantity_change FROM transactions"
        " WHERE reference_no=? AND product_id=? AND note IN (?, ?)",
        (doc_no, product_id, *_SALES_LEDGER_NOTES)).fetchall()
    if len(rows) != 1:
        raise Refused('ledger_rows',
                      f'บรรทัด {doc_no} มีรายการตัดสต็อก {len(rows)} แถว (ต้องมี 1 แถวพอดี)')
    return rows[0]


def _signed_base_qty(conn, product_id, unit_type, unit, qty, doc_no):
    """What the stock writer posts for this line in `unit`, signed (a sale is
    negative, an SR return positive), or None when the unit has no ratio."""
    from models.bsn_sync import _get_base_qty
    base = _get_base_qty(conn, product_id, unit_type or '', unit, qty or 0)
    if base is None:
        return None
    sign = 1 if document_kind.is_return(doc_no, 'sales') else -1
    return round(sign * base, 4)


def _purchase_exposure(conn, product_id, replace=None):
    """How the WACC walk would meet this product's purchases: the lowest
    running stock, and how many `BSN ซื้อ` lots are costed while stock is
    below zero (WACC frozen) or exactly zero (WACC replaced).

    Same order and same skips as models/wacc.py::_rebuild_product_wacc.
    `replace` = (transactions.id, quantity_change) is applied in memory.
    """
    rows = conn.execute(
        "SELECT id, txn_type, quantity_change, reference_no, note FROM transactions"
        " WHERE product_id=?"
        " ORDER BY created_at, CASE WHEN txn_type='IN' THEN 0 ELSE 1 END, id",
        (product_id,)).fetchall()
    history_refs = {r['reference_no'] for r in rows
                    if (r['note'] or '').startswith('ประวัติขาย') and r['reference_no']}
    running = 0.0
    lowest = 0.0
    below_zero = at_zero = 0
    for r in rows:
        note = r['note'] or ''
        if note.startswith('ประวัติขาย') or (
                r['txn_type'] == 'OUT' and (r['reference_no'] or '') in history_refs):
            continue
        qty = r['quantity_change']
        if replace is not None and r['id'] == replace[0]:
            qty = replace[1]
        if r['txn_type'] == 'IN' and note == 'BSN ซื้อ' and qty > 0:
            if running < 0:
                below_zero += 1
            elif running == 0:
                at_zero += 1
        running += qty
        lowest = min(lowest, running)
    return {'min_stock': round(lowest, 4),
            'purchases_below_zero': below_zero,
            'purchases_at_zero': at_zero}


def preview(conn, doc_no, bsn_code, corrected_unit):
    """Read-only. Every refusal, then the measured effect of both stock modes."""
    label = _line_label(doc_no, bsn_code)
    rows = conn.execute(
        "SELECT * FROM sales_transactions WHERE doc_no=? AND bsn_code=?",
        (doc_no, bsn_code)).fetchall()
    if not rows:
        raise Refused('not_found', f'ไม่พบบรรทัด {label}')
    if len(rows) > 1:
        raise Refused('duplicate_key',
                      f'บรรทัด {label} มี {len(rows)} แถว ระบุบรรทัดเดียวไม่ได้')
    row = rows[0]
    product_id = row['product_id']
    if product_id is None:
        raise Refused('unmapped', f'บรรทัด {label} ยังไม่ได้ผูกสินค้า')
    # Two lines of one product under one literal doc_no share a ledger key
    # (models/reconcile.py::_ledger_check has the live case).
    twins = conn.execute(
        "SELECT COUNT(*) FROM sales_transactions WHERE doc_no=? AND product_id=?",
        (doc_no, product_id)).fetchone()[0]
    if twins > 1:
        raise Refused('twin_doc_no',
                      f'เลขที่ {doc_no} มีสินค้านี้ {twins} แถว แยกรายการตัดสต็อกไม่ได้')
    if not row['synced_to_stock']:
        raise Refused('unsynced', f'บรรทัด {label} ยังไม่ได้ตัดสต็อก')
    if row['batch_id'] == 'history_import':
        raise Refused('history_import',
                      f'บรรทัด {label} เป็นประวัติขายที่ไม่นับสต็อก แก้หน่วยไม่ได้')
    if conn.execute(
            "SELECT 1 FROM platform_stock_deductions"
            " WHERE source_table='sales_transactions' AND source_id=?",
            (row['id'],)).fetchone():
        raise Refused('platform_deduction',
                      f'บรรทัด {label} มีรายการตัดสต็อกแพลตฟอร์มผูกอยู่ แก้หน่วยไม่ได้')
    if conn.execute(
            "SELECT 1 FROM sales_line_unit_corrections"
            " WHERE doc_no=? AND bsn_code=? AND status='active'",
            (doc_no, bsn_code)).fetchone():
        raise Refused('already_active',
                      f'บรรทัด {label} มีการแก้หน่วยค้างอยู่แล้ว ยกเลิกก่อนจึงแก้ใหม่ได้')
    if corrected_unit not in allowed_units(conn, product_id):
        raise Refused('unit_not_allowed',
                      f'หน่วย "{corrected_unit}" ไม่ใช่หน่วยหลักและไม่มีอัตราแปลงของสินค้านี้')
    if _norm(conn, corrected_unit) == _norm(conn, row['unit']):
        raise Refused('same_unit', f'บรรทัด {label} เป็นหน่วย "{corrected_unit}" อยู่แล้ว')

    unit_type = conn.execute(
        "SELECT unit_type FROM products WHERE id=?", (product_id,)).fetchone()[0]
    ledger = _ledger_row(conn, doc_no, product_id)
    old_effect = _signed_base_qty(
        conn, product_id, unit_type, row['unit'], row['qty'], doc_no)
    if old_effect is None or abs(ledger['quantity_change'] - old_effect) >= EPSILON:
        raise Refused('ledger_mismatch',
                      f'รายการตัดสต็อกของ {label} ไม่ตรงกับหน่วยที่เก็บไว้ ตรวจสอบก่อนแก้')
    new_effect = _signed_base_qty(
        conn, product_id, unit_type, corrected_unit, row['qty'], doc_no)
    if not new_effect:
        raise Refused('unit_not_allowed',
                      f'หน่วย "{corrected_unit}" แปลงจำนวนของ {label} ไม่ได้')

    stock_now = _stock(conn, product_id)
    adjust = conn.execute(
        "SELECT id, note, created_at FROM transactions"
        " WHERE product_id=? AND txn_type='ADJUST' AND created_at > ?"
        "   AND COALESCE(note, '') NOT LIKE ?"
        " ORDER BY created_at, id LIMIT 1",
        (product_id, row['date_iso'] + ' 00:00:00', OFFSET_NOTE_PREFIX + '%')).fetchone()
    return Effect(
        row_id=row['id'], doc_no=doc_no, bsn_code=bsn_code,
        doc_base=row['doc_base'] or doc_no.rsplit('-', 1)[0],
        product_id=product_id, date_iso=row['date_iso'],
        qty=row['qty'], unit_price=row['unit_price'], net=row['net'],
        express_unit_raw=row['unit'], corrected_unit=corrected_unit,
        ledger_txn_id=ledger['id'], old_effect=old_effect, new_effect=new_effect,
        stock_now=stock_now, stock_after_hold=stock_now,
        stock_after_move=round(stock_now - old_effect + new_effect, 4),
        proposed_mode='hold' if adjust else 'move',
        adjust_seen=dict(adjust) if adjust else None,
        exposure_before=_purchase_exposure(conn, product_id),
        exposure_after=_purchase_exposure(
            conn, product_id, replace=(ledger['id'], new_effect)),
    )


def _rescan_and_recost(conn, doc_base, product_id):
    import review_rules
    from models.wacc import preflight_batch, recalculate_product_wacc
    review_rules.scan_docs([doc_base], conn=conn)
    preflight_batch(conn, [product_id], operation='unit_correction')
    recalculate_product_wacc(product_id, conn, operation='unit_correction')


def _alert_wacc_failure(exc, doc_no):
    """The transaction is already rolled back, so `conn` holds no write lock
    and a fresh connection can record the alert."""
    from models.system_alerts import record_wacc_identity_alert
    record_wacc_identity_alert(exc, operation='unit_correction',
                               extra={'doc_no': doc_no})


def _require_reason(reason):
    if len((reason or '').strip()) < MIN_REASON:
        raise Refused('reason_too_short',
                      f'ต้องระบุเหตุผลอย่างน้อย {MIN_REASON} ตัวอักษร')


def apply(conn, doc_no, bsn_code, corrected_unit, stock_mode, reason, actor):
    """Correct one line's unit. ONE transaction on `conn`, which must not have
    one open. Returns the correction id."""
    from models import _shared
    from models.wacc import WaccIdentityError
    if stock_mode not in MODES:
        raise Refused('bad_mode', f'ไม่รู้จักวิธีปรับสต็อก "{stock_mode}"')
    _require_reason(reason)
    try:
        with database.immediate(conn):
            effect = preview(conn, doc_no, bsn_code, corrected_unit)
            pid = effect.product_id
            stock_before = _stock(conn, pid)
            _shared.declared_update(
                conn, 'sales_transactions', effect.row_id, {'unit': corrected_unit},
                actor=actor, source='manual', reason=reason)
            conn.execute("UPDATE transactions SET quantity_change=? WHERE id=?",
                         (effect.new_effect, effect.ledger_txn_id))
            offset_txn_id = None
            if stock_mode == 'hold':
                # Measured, not computed: the sign differs between a sale and
                # an SR return, and the trigger has already moved the stock.
                offset = round(stock_before - _stock(conn, pid), 4)
                if offset:
                    # The sale's own timestamp: the WACC walk orders by
                    # created_at, IN first, id (models/wacc.py), so the
                    # running stock at every purchase stays what it is today.
                    offset_txn_id = conn.execute(
                        "INSERT INTO transactions (product_id, txn_type,"
                        " quantity_change, unit_mode, reference_no, note, created_at)"
                        " VALUES (?, 'ADJUST', ?, 'unit', NULL, ?, ?)",
                        (pid, offset,
                         f'{OFFSET_NOTE_PREFIX}{doc_no}: {effect.express_unit_raw}'
                         f' → {corrected_unit} (คงยอดคงเหลือ)',
                         effect.date_iso + ' 00:00:00')).lastrowid
                if round(_stock(conn, pid), 4) != round(stock_before, 4):
                    raise RuntimeError(
                        f'unit correction {doc_no}: stock moved in hold mode '
                        f'({stock_before} -> {_stock(conn, pid)})')
            correction_id = conn.execute(
                "INSERT INTO sales_line_unit_corrections"
                " (doc_no, bsn_code, doc_base, product_id, express_unit_raw,"
                "  express_unit, qty, unit_price, net, corrected_unit, stock_mode,"
                "  offset_txn_id, reason, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_no, bsn_code, effect.doc_base, pid, effect.express_unit_raw,
                 _norm(conn, effect.express_unit_raw), effect.qty,
                 effect.unit_price, effect.net, corrected_unit, stock_mode,
                 offset_txn_id, reason.strip(), actor)).lastrowid
            _rescan_and_recost(conn, effect.doc_base, pid)
    except WaccIdentityError as exc:
        _alert_wacc_failure(exc, doc_no)
        raise
    return correction_id


def cancel(conn, correction_id, reason, actor):
    """Put the line back to Express's unit and take the offset out. ONE
    transaction on `conn`, the inverse of `apply`."""
    from models import _shared
    from models.wacc import WaccIdentityError
    _require_reason(reason)
    doc_no = None
    try:
        with database.immediate(conn):
            c = conn.execute(
                "SELECT * FROM sales_line_unit_corrections WHERE id=?",
                (correction_id,)).fetchone()
            if c is None or c['status'] != 'active':
                raise Refused('not_active', 'การแก้หน่วยนี้ไม่ได้ค้างอยู่ ยกเลิกไม่ได้')
            doc_no, pid = c['doc_no'], c['product_id']
            label = _line_label(doc_no, c['bsn_code'])
            rows = conn.execute(
                "SELECT * FROM sales_transactions WHERE doc_no=? AND bsn_code=?",
                (doc_no, c['bsn_code'])).fetchall()
            if len(rows) != 1:
                raise Refused('line_missing',
                              f'บรรทัด {label} มี {len(rows)} แถว (ต้องมี 1 แถวพอดี)')
            row = rows[0]
            if _norm(conn, row['unit']) != _norm(conn, c['corrected_unit']):
                raise Refused('line_changed',
                              f'บรรทัด {label} ไม่ได้เป็นหน่วย "{c["corrected_unit"]}" แล้ว')
            if row['product_id'] != pid:
                raise Refused('product_changed',
                              f'บรรทัด {label} ถูกย้ายไปสินค้าอื่นแล้ว')
            unit_type = conn.execute(
                "SELECT unit_type FROM products WHERE id=?", (pid,)).fetchone()[0]
            # Without a ratio the restored row could never post again and
            # would sit unsynced with no ledger row.
            restored = _signed_base_qty(
                conn, pid, unit_type, c['express_unit_raw'], row['qty'], doc_no)
            if restored is None:
                raise Refused('express_unit_no_ratio',
                              f'หน่วยเดิม "{c["express_unit_raw"]}" ไม่มีอัตราแปลงของสินค้านี้แล้ว')
            ledger = _ledger_row(conn, doc_no, pid)
            if c['offset_txn_id'] is not None:
                offset = conn.execute(
                    "SELECT product_id FROM transactions WHERE id=?",
                    (c['offset_txn_id'],)).fetchone()
                if offset is None or offset['product_id'] != pid:
                    raise Refused('offset_missing',
                                  f'ไม่พบรายการปรับยอดคงเหลือของ {label} บนสินค้านี้')

            stock_before = _stock(conn, pid)
            _shared.declared_update(
                conn, 'sales_transactions', row['id'], {'unit': c['express_unit_raw']},
                actor=actor, source='manual', reason=reason)
            conn.execute("UPDATE transactions SET quantity_change=? WHERE id=?",
                         (restored, ledger['id']))
            if c['offset_txn_id'] is not None:
                conn.execute("DELETE FROM transactions WHERE id=?", (c['offset_txn_id'],))
            if c['stock_mode'] == 'hold' and \
                    round(_stock(conn, pid), 4) != round(stock_before, 4):
                raise RuntimeError(
                    f'unit correction {doc_no}: cancelling a hold moved stock '
                    f'({stock_before} -> {_stock(conn, pid)})')
            conn.execute(
                "UPDATE sales_line_unit_corrections SET status='cancelled',"
                " end_cause='cancelled', ended_at=datetime('now','localtime'),"
                " ended_by=?, end_reason=? WHERE id=? AND status='active'",
                (actor, reason.strip(), correction_id))
            _rescan_and_recost(conn, c['doc_base'], pid)
    except WaccIdentityError as exc:
        _alert_wacc_failure(exc, doc_no)
        raise


def active_by_line_key(conn):
    """{(doc_no, bsn_code): correction} for every active correction, each with
    its two units normalised through TODAY's unit map (`express_unit_norm`,
    `corrected_unit_norm`), so a later respelling in the map is not read as an
    Express change."""
    out = {}
    for c in _dicts(conn.execute(
            "SELECT * FROM sales_line_unit_corrections WHERE status='active'")):
        c['express_unit_norm'] = _norm(conn, c['express_unit'])
        c['corrected_unit_norm'] = _norm(conn, c['corrected_unit'])
        out[(c['doc_no'], c['bsn_code'])] = c
    return out


def _num_same(a, b):
    return abs((a or 0) - (b or 0)) < EPSILON


def decide(correction, entry, product_id):
    """What an incoming Express line means for its active correction. Pure.

    `entry['unit']` is the importer's already-normalised Express unit and
    `product_id` the product the mapping resolved on that unit.
    """
    unit = entry['unit'] or ''
    same_line = (
        _num_same(entry['qty'], correction['qty'])
        and _num_same(entry['unit_price'], correction['unit_price'])
        and _num_same(entry['net'], correction['net'])
        and (product_id or 0) == correction['product_id'])
    if same_line and unit == correction['express_unit_norm']:
        return KEEP
    if same_line and unit == correction['corrected_unit_norm']:
        return EXPRESS_AGREES
    return EXPRESS_CHANGED


def retire(conn, correction, cause, actor):
    """The importer ends a correction because Express moved. On the caller's
    connection and inside its transaction: no re-sync, no scan, no WACC here.
    Returns the products whose ledger the importer must rebuild.

    `express_agrees` keeps the offset: Express now says what Sendy says, so
    nothing physical changed and the stock must not move.
    """
    from models.system_alerts import create_system_alert, KIND_UNIT_CORRECTION_RETIRED
    pids = {correction['product_id']}
    if cause != EXPRESS_AGREES and correction['offset_txn_id'] is not None:
        offset = conn.execute(
            "SELECT product_id FROM transactions WHERE id=?",
            (correction['offset_txn_id'],)).fetchone()
        if offset is not None:
            pids.add(offset[0])
            conn.execute("DELETE FROM transactions WHERE id=?",
                         (correction['offset_txn_id'],))
    conn.execute(
        "UPDATE sales_line_unit_corrections SET status='retired', end_cause=?,"
        " ended_at=datetime('now','localtime'), ended_by=?"
        " WHERE id=? AND status='active'",
        (cause, actor, correction['id']))
    create_system_alert(
        KIND_UNIT_CORRECTION_RETIRED,
        f'การแก้หน่วยบรรทัด {correction["doc_no"]} '
        f'({correction["express_unit"]} → {correction["corrected_unit"]}) '
        f'สิ้นสุดแล้ว: {_RETIRE_CAUSE_TH[cause]}',
        dedupe_key=str(correction['id']), severity='warning',
        context={'correction_id': correction['id'], 'doc_no': correction['doc_no'],
                 'bsn_code': correction['bsn_code'],
                 'product_id': correction['product_id'], 'cause': cause},
        conn=conn)
    return pids


def blocking(conn, *, product_id=None, doc_base=None, bsn_code=None):
    """Active corrections a writer would strand: those on any of `product_id`
    (one id or several), on `doc_base`, or on `bsn_code`. A writer that gets a
    non-empty list refuses with `refusal(...)` before its first write."""
    clauses, params = [], []
    if product_id is not None:
        pids = [product_id] if isinstance(product_id, int) else list(product_id)
        if pids:
            clauses.append(f"product_id IN ({','.join('?' * len(pids))})")
            params += pids
    if doc_base is not None:
        clauses.append("doc_base = ?")
        params.append(doc_base)
    if bsn_code is not None:
        clauses.append("bsn_code = ?")
        params.append(bsn_code)
    if not clauses:
        raise ValueError('blocking() needs a product, a document or a code')
    return _dicts(conn.execute(
        "SELECT * FROM sales_line_unit_corrections WHERE status='active'"
        f" AND ({' OR '.join(clauses)}) ORDER BY id", params))


def refusal(corrections):
    docs = ', '.join(sorted({c['doc_no'] for c in corrections}))
    return f'ยกเลิกการแก้หน่วยบรรทัดก่อน ({docs})'


def badges_for_doc(conn, doc_base):
    """{(doc_no, bsn_code): the line's latest correction, any status}."""
    out = {}
    for c in _dicts(conn.execute(
            "SELECT * FROM sales_line_unit_corrections WHERE doc_base=?"
            " ORDER BY created_at, id", (doc_base,))):
        out[(c['doc_no'], c['bsn_code'])] = c
    return out
