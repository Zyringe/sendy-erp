"""reconcile-scan — detect + confirm-apply for Sendy sales docs that
disappeared from Express entirely (deleted/cancelled at source).

See projects/express-integration/reconcile-scan-plan.md (rev 8, GO) for the
full contract this module implements. Short version:

- `scan_reconcile()` runs read-only detection AFTER the normal Express DBF
  import (import_router.commit_express_dbf calls it with the SAME
  eds.build_sales_entries() output already built for the import — no second
  parse — plus the raw ARTRN header rows). It classifies every Sendy sales
  doc in the recency window into one of six states and upserts
  express_reconcile_flags/_events. Only the 'deleted' class is apply-eligible;
  the rest are visibility-only.
- `apply_reconcile_flag()` is the confirm-apply for one 'deleted' flag: every
  refusal check runs server-side, before any write, inside ONE
  `BEGIN IMMEDIATE` transaction on one connection.

Conventions: raw SQL via `sqlite3` (see database.py), no ORM. `conn.row_factory`
must be `sqlite3.Row` for every connection this module touches — callers that
open their own (tests) must set it.
"""
import json
from collections import defaultdict

from database import get_connection
import bsn_units
from .bsn_sync import PLATFORM_STOCK_DEDUCT_CUSTOMERS, _get_base_qty

# Mirrors express_dbf_source._SCOPE_RECTYP — kept as a separate literal
# (not imported) so this module has no hard dependency on dbfread/DBF file
# IO; it only ever sees already-read dict rows.
_SCOPE_RECTYP = ('3', '1', '5')

_CLASSES = ('deleted', 'out_of_scope', 'parse_gap', 'date_moved', 'data_gap')

# The canonical CAS row (plan §2 Detection payload — exhaustive):
# bsn_code because CAS sorts/dedups on (doc_no, bsn_code); ref_invoice
# because a post-scan credit-note import can update it and that change
# must fail CAS, not slip through.
_CAS_FIELDS = ('id', 'doc_no', 'doc_base', 'bsn_code', 'customer', 'product_id',
               'qty', 'unit', 'unit_price', 'net', 'synced_to_stock', 'ref_invoice')
_CAS_NUMERIC = ('qty', 'unit_price', 'net')

_BSN_SALE_NOTES = ('BSN ขาย', 'BSN ขาย-คืน')

_PLATFORM_REFUSAL_MSG = (
    'บิลนี้เคยหักสต็อกแพลตฟอร์ม — ระบบย้อนให้อัตโนมัติไม่ได้ '
    'ต้องจัดการ platform stock เองก่อน แล้ว dismiss พร้อมโน้ต')


class _ConnCtx:
    """conn=None -> open+own a connection (commit on clean exit, rollback on
    exception, always close). conn=<given> -> use as-is; the CALLER owns the
    transaction (test-friendly — lets a test inspect state before commit)."""
    def __init__(self, conn):
        self._given = conn
        self._owned = None

    def __enter__(self):
        if self._given is not None:
            return self._given
        self._owned = get_connection()
        return self._owned

    def __exit__(self, exc_type, exc, tb):
        if self._owned is not None:
            if exc_type is None:
                self._owned.commit()
            else:
                self._owned.rollback()
            self._owned.close()
        return False


def _doc_base(doc_no):
    return doc_no.rsplit('-', 1)[0] if '-' in doc_no else doc_no


def _row_to_dict(r):
    return {k: r[k] for k in r.keys()}


def _payload_for_doc(conn, doc_base):
    """Canonical CAS snapshot: current sales_transactions rows for doc_base,
    sorted by (doc_no, bsn_code) per plan §2."""
    rows = conn.execute(
        f"SELECT {', '.join(_CAS_FIELDS)} FROM sales_transactions "
        "WHERE doc_base = ? ORDER BY doc_no, bsn_code", (doc_base,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _write_event(conn, flag_id, from_state, to_state, from_class, to_class, actor, note):
    conn.execute(
        "INSERT INTO express_reconcile_events "
        "(flag_id, from_state, to_state, from_class, to_class, actor, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (flag_id, from_state, to_state, from_class, to_class, actor, note))


# ── Detection ────────────────────────────────────────────────────────────────

def _headers_conflict(headers_raw):
    """True when 2+ raw ARTRN rows share a DOCNUM but disagree on the fields
    that drive classification (RECTYP/DOCDAT) — the builder's dict-comprehension
    last-one-wins collapse must not silently pick a winner for us (plan §2)."""
    keyset = {(h.get('RECTYP'), h.get('DOCDAT')) for h in headers_raw}
    return len(keyset) > 1


def _classify_doc(headers_raw, present, cutoff):
    """Returns (class, evidence_dict) or (None, None) when the doc is
    genuinely present (not ours — the import already handles it)."""
    if len(headers_raw) >= 2 and _headers_conflict(headers_raw):
        return 'data_gap', {'reason': 'duplicate_conflicting_header',
                             'headers_seen': len(headers_raw)}
    if present:
        return None, None
    if not headers_raw:
        return 'deleted', {}
    hdr = headers_raw[-1]
    docdat = hdr.get('DOCDAT')
    if docdat is None:
        return 'parse_gap', {'rectyp_seen': hdr.get('RECTYP')}
    rectyp = hdr.get('RECTYP')
    if rectyp not in _SCOPE_RECTYP:
        return 'out_of_scope', {'rectyp_seen': rectyp}
    if cutoff is not None and docdat < cutoff:
        return 'date_moved', {'docdat': docdat.isoformat()}
    return 'data_gap', {'reason': 'no_stcrd_match'}


def _suppressed(conn, doc_base, cls):
    return conn.execute(
        "SELECT 1 FROM express_reconcile_flags "
        "WHERE doc_base=? AND class=? AND state='dismissed' AND suppression_active=1",
        (doc_base, cls)).fetchone() is not None


def _upsert_flag(conn, doc_base, cls, evidence, actor):
    payload_rows = _payload_for_doc(conn, doc_base)
    payload_json = json.dumps({'rows': payload_rows, 'evidence': evidence}, sort_keys=True)

    existing = conn.execute(
        "SELECT id, class FROM express_reconcile_flags WHERE doc_base=? AND state='open'",
        (doc_base,)).fetchone()
    if existing:
        old_class = existing['class']
        conn.execute(
            "UPDATE express_reconcile_flags "
            "SET latest_payload_json=?, last_seen_at=datetime('now','localtime'), class=? "
            "WHERE id=?",
            (payload_json, cls, existing['id']))
        if old_class != cls:
            _write_event(conn, existing['id'], 'open', 'open', old_class, cls,
                        actor, 'class เปลี่ยนตอน re-scan')
        return 'refreshed'

    if _suppressed(conn, doc_base, cls):
        return 'suppressed'

    cur = conn.execute(
        "INSERT INTO express_reconcile_flags "
        "(doc_base, class, first_payload_json, latest_payload_json, state) "
        "VALUES (?, ?, ?, ?, 'open')",
        (doc_base, cls, payload_json, payload_json))
    _write_event(conn, cur.lastrowid, None, 'open', None, cls, actor, 'ตรวจพบครั้งแรก')
    return 'opened'


def _close_as_reappeared(conn, doc_base, actor):
    existing = conn.execute(
        "SELECT id, class FROM express_reconcile_flags WHERE doc_base=? AND state='open'",
        (doc_base,)).fetchone()
    if not existing:
        return False
    conn.execute(
        "UPDATE express_reconcile_flags "
        "SET state='reappeared', resolved_by='system', "
        "resolved_at=datetime('now','localtime'), resolution_note=? WHERE id=?",
        ('เอกสารกลับมาปรากฏใน Express แล้ว', existing['id']))
    _write_event(conn, existing['id'], 'open', 'reappeared',
                existing['class'], existing['class'], 'system',
                'เอกสารกลับมาปรากฏใน Express แล้ว')
    return True


def scan_reconcile(sales_entries, artrn_rows, cutoff, actor='system', conn=None):
    """Whole-document-disappearance detection for one commit_express_dbf run.

    sales_entries: express_dbf_source.build_sales_entries() output, ALREADY
        built for the import (reused verbatim — this never re-parses).
    artrn_rows: the raw, UNFILTERED ARTRN header rows (express_dbf_source.
        open_table(dataset_dir, 'ARTRN') output) — every DOCNUM header as
        Express actually wrote it, including duplicates.
    cutoff: a datetime.date (docs with date_iso before it are outside the
        universe) or None (unbounded — mirrors express_dbf_source._in_window's
        None semantics for a manual full-history run).
    actor: recorded on every flags/events row this scan writes.

    Detection-only: NEVER mutates sales_transactions/transactions. Writes are
    confined to express_reconcile_flags/_events. Returns per-class open-flag
    counts plus 'reappeared' (docs whose open flag auto-closed this scan).
    """
    doc_bases_present = {_doc_base(e['doc_no']) for e in sales_entries}

    raw_by_docnum = defaultdict(list)
    for r in artrn_rows:
        raw_by_docnum[r.get('DOCNUM')].append(r)

    with _ConnCtx(conn) as c:
        if cutoff is not None:
            sendy_docs = [r[0] for r in c.execute(
                "SELECT DISTINCT doc_base FROM sales_transactions WHERE date_iso >= ?",
                (cutoff.isoformat(),)).fetchall()]
        else:
            sendy_docs = [r[0] for r in c.execute(
                "SELECT DISTINCT doc_base FROM sales_transactions").fetchall()]

        counts = {cls: 0 for cls in _CLASSES}
        counts['reappeared'] = 0

        for doc_base in sendy_docs:
            headers_raw = raw_by_docnum.get(doc_base, [])
            present = doc_base in doc_bases_present
            cls, evidence = _classify_doc(headers_raw, present, cutoff)
            if cls is None:
                if _close_as_reappeared(c, doc_base, actor):
                    counts['reappeared'] += 1
                continue
            outcome = _upsert_flag(c, doc_base, cls, evidence, actor)
            if outcome in ('opened', 'refreshed'):
                counts[cls] += 1

    return counts


# ── Read / lifecycle ─────────────────────────────────────────────────────────

def list_open_reconcile_flags(conn=None):
    with _ConnCtx(conn) as c:
        rows = c.execute(
            "SELECT id, doc_base, class, first_seen_at, last_seen_at, state "
            "FROM express_reconcile_flags WHERE state='open' "
            "ORDER BY class = 'deleted' DESC, last_seen_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


_LINKED_QUERIES = {
    'paid_invoices':        ("SELECT * FROM paid_invoices WHERE doc_no = ?", 'doc_base'),
    'commission_payouts':   ("SELECT * FROM commission_payouts WHERE invoice_no = ?", 'doc_base'),
    'credit_note_amounts':  ("SELECT * FROM credit_note_amounts WHERE ref_invoice = ?", 'doc_base'),
    'credit_note_imports':  ("SELECT * FROM credit_note_imports WHERE ref_invoice = ?", 'doc_base'),
    'marketplace_order_invoice':  ("SELECT * FROM marketplace_order_invoice WHERE doc_base = ?", 'doc_base'),
    'marketplace_amount_review':  ("SELECT * FROM marketplace_amount_review WHERE doc_base = ?", 'doc_base'),
    'express_invoice_refs': ("SELECT * FROM express_invoice_refs WHERE doc_base = ?", 'doc_base'),
}


def _linked_records(conn, doc_base):
    """Everything §4 says the panel must surface, keyed by table name. v1
    NEVER mutates any of these — surfaced for a human to judge, per plan §4."""
    out = {}
    for key, (sql, _param) in _LINKED_QUERIES.items():
        out[key] = [_row_to_dict(r) for r in conn.execute(sql, (doc_base,)).fetchall()]
    # SR rows (in sales_transactions) referencing this doc as their ref_invoice —
    # a different signal than credit_note_amounts/imports (those are the
    # authoritative CN master; this is the ledger-side SR line still pointing here).
    out['sr_ref_rows'] = [_row_to_dict(r) for r in conn.execute(
        "SELECT id, doc_no, doc_base, net, customer FROM sales_transactions "
        "WHERE ref_invoice = ?", (doc_base,)).fetchall()]
    return out


def get_reconcile_flag(flag_id, conn=None):
    with _ConnCtx(conn) as c:
        row = c.execute(
            "SELECT * FROM express_reconcile_flags WHERE id=?", (flag_id,)).fetchone()
        if row is None:
            return None
        out = _row_to_dict(row)
        out['first_payload'] = json.loads(out['first_payload_json'])
        out['latest_payload'] = json.loads(out['latest_payload_json'])
        out['linked_records'] = _linked_records(c, out['doc_base'])
        events = c.execute(
            "SELECT * FROM express_reconcile_events WHERE flag_id=? ORDER BY id",
            (flag_id,)).fetchall()
        out['events'] = [_row_to_dict(r) for r in events]
        return out


def dismiss_reconcile_flag(flag_id, resolved_by, note, conn=None):
    if not note or not note.strip():
        return {'ok': False, 'error': 'ต้องระบุเหตุผลก่อน dismiss'}
    with _ConnCtx(conn) as c:
        row = c.execute(
            "SELECT id, state, class FROM express_reconcile_flags WHERE id=?",
            (flag_id,)).fetchone()
        if row is None or row['state'] != 'open':
            return {'ok': False, 'error': 'flag ไม่ได้อยู่ในสถานะ open'}
        c.execute(
            "UPDATE express_reconcile_flags SET state='dismissed', suppression_active=1, "
            "resolved_by=?, resolved_at=datetime('now','localtime'), resolution_note=? "
            "WHERE id=?",
            (resolved_by, note.strip(), flag_id))
        _write_event(c, flag_id, 'open', 'dismissed', row['class'], row['class'],
                    resolved_by, note.strip())
        return {'ok': True}


def reopen_reconcile_flag(flag_id, resolved_by, conn=None):
    """'เปิดพิจารณาใหม่' — clears suppression so the NEXT scan may re-flag
    this (doc_base, class). The row itself stays 'dismissed' (plan §2:
    the epoch ends two ways, both audited; this is the explicit-button way)."""
    with _ConnCtx(conn) as c:
        row = c.execute(
            "SELECT id, state, class, suppression_active FROM express_reconcile_flags "
            "WHERE id=?", (flag_id,)).fetchone()
        if row is None or row['state'] != 'dismissed' or not row['suppression_active']:
            return {'ok': False, 'error': 'flag ไม่ได้อยู่ในสถานะ suppressed'}
        c.execute(
            "UPDATE express_reconcile_flags SET suppression_active=0 WHERE id=?",
            (flag_id,))
        _write_event(c, flag_id, 'dismissed', 'dismissed', row['class'], row['class'],
                    resolved_by, 'เปิดพิจารณาใหม่')
        return {'ok': True}


# ── Apply ────────────────────────────────────────────────────────────────────

def _cas_compare(expected_rows, live_rows):
    """Field-wise CAS, canonicalized per plan §2c. Returns an error message,
    or None when the live state still matches what the flag last saw."""
    def key(r):
        return (r['doc_no'], r['bsn_code'])

    exp_keys = [key(r) for r in expected_rows]
    if len(exp_keys) != len(set(exp_keys)):
        return ('ข้อมูลอ้างอิง (latest_payload) มี doc_no+bsn_code ซ้ำ '
                '— ปฏิเสธ apply (ระบุแถวไม่ได้ชัดเจน)')
    live_keys = [key(r) for r in live_rows]
    if len(live_keys) != len(set(live_keys)):
        return ('ข้อมูลปัจจุบันมี doc_no+bsn_code ซ้ำ '
                '— ปฏิเสธ apply (ระบุแถวไม่ได้ชัดเจน)')

    exp_by_key = {key(r): r for r in expected_rows}
    live_by_key = {key(r): r for r in live_rows}
    if set(exp_by_key) != set(live_by_key):
        return 'ข้อมูลเปลี่ยนไปตั้งแต่สแกนล่าสุด (แถวหายไปหรือเพิ่มมา) — สแกนใหม่ก่อน apply'

    for k, exp in exp_by_key.items():
        live = live_by_key[k]
        for field in _CAS_NUMERIC:
            if abs((exp.get(field) or 0) - (live.get(field) or 0)) >= 1e-9:
                return f'ข้อมูลเปลี่ยนไปตั้งแต่สแกนล่าสุด ({field} ของ {k}) — สแกนใหม่ก่อน apply'
        if bsn_units.normalize_unit(exp.get('unit') or '') != bsn_units.normalize_unit(live.get('unit') or ''):
            return f'ข้อมูลเปลี่ยนไปตั้งแต่สแกนล่าสุด (unit ของ {k}) — สแกนใหม่ก่อน apply'
        for field in ('product_id', 'customer', 'synced_to_stock', 'ref_invoice'):
            if (exp.get(field) or None) != (live.get(field) or None):
                return f'ข้อมูลเปลี่ยนไปตั้งแต่สแกนล่าสุด ({field} ของ {k}) — สแกนใหม่ก่อน apply'
    return None


def _ledger_check(conn, payload_rows):
    """Both directions (plan §2d): every synced line has exactly one
    'BSN ขาย'/'BSN ขาย-คืน' transactions row whose magnitude matches; every
    unsynced line has none; and NO other transactions row references any of
    this doc's doc_nos at all (catches history_import compensator legs and
    anything else unexpected)."""
    doc_nos = sorted({r['doc_no'] for r in payload_rows})
    if not doc_nos:
        return None
    ph = ','.join('?' * len(doc_nos))
    all_txns = conn.execute(
        f"SELECT id, reference_no, note, quantity_change FROM transactions "
        f"WHERE reference_no IN ({ph})", doc_nos).fetchall()
    by_doc_no = defaultdict(list)
    for t in all_txns:
        by_doc_no[t['reference_no']].append(t)

    matched_ids = set()
    for r in payload_rows:
        doc_no = r['doc_no']
        candidates = by_doc_no.get(doc_no, [])
        bsn_rows = [t for t in candidates if t['note'] in _BSN_SALE_NOTES]
        if r.get('synced_to_stock'):
            if len(bsn_rows) != 1:
                return f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ'
            txn = bsn_rows[0]
            product = conn.execute(
                "SELECT unit_type FROM products WHERE id=?", (r['product_id'],)).fetchone()
            if product is not None:
                expected_base = _get_base_qty(
                    conn, r['product_id'], product['unit_type'] or '', r['unit'], r['qty'] or 0)
                if expected_base is not None and abs(abs(txn['quantity_change']) - abs(expected_base)) >= 1e-9:
                    return f'ledger ไม่ตรงกับข้อมูลของ {doc_no} (qty ไม่ตรง) — จัดการมือ'
            matched_ids.add(txn['id'])
        else:
            if bsn_rows:
                return f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ'

    for doc_no, candidates in by_doc_no.items():
        for t in candidates:
            if t['id'] not in matched_ids:
                return (f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ '
                        '(อาจเป็น history_import compensator หรืออื่นๆ)')
    return None


def _delete_stock_sync_txns(conn, doc_nos):
    if not doc_nos:
        return
    ph = ','.join('?' * len(doc_nos))
    conn.execute(
        f"DELETE FROM transactions WHERE reference_no IN ({ph}) "
        f"AND note IN ('BSN ขาย','BSN ขาย-คืน')", doc_nos)


def _delete_sales_rows(conn, row_ids):
    if not row_ids:
        return
    ph = ','.join('?' * len(row_ids))
    conn.execute(f"DELETE FROM sales_transactions WHERE id IN ({ph})", row_ids)


def _clean_review_docs(conn, doc_base):
    conn.execute("DELETE FROM txn_review_flags WHERE doc_base=?", (doc_base,))
    conn.execute("DELETE FROM txn_review_docs WHERE doc_base=?", (doc_base,))


def apply_reconcile_flag(flag_id, resolved_by, conn=None):
    """Confirm-apply for one 'deleted' flag (plan §2 Apply). ONE
    `BEGIN IMMEDIATE` transaction on one connection; every refusal check
    runs BEFORE any write. conn=None (the normal route path) opens+owns its
    own connection so the BEGIN IMMEDIATE lock is acquired the instant this
    function starts — the concurrency contract this exists to prove."""
    own = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT id, doc_base, class, state, latest_payload_json "
            "FROM express_reconcile_flags WHERE id=?", (flag_id,)).fetchone()
        if row is None:
            c.rollback()
            return {'ok': False, 'error': 'ไม่พบ flag'}
        if row['state'] == 'applied':
            c.rollback()
            return {'ok': True, 'noop': True}
        if row['state'] != 'open':
            c.rollback()
            return {'ok': False, 'error': f"flag อยู่ในสถานะ {row['state']} — apply ไม่ได้"}
        if row['class'] != 'deleted':
            c.rollback()
            return {'ok': False, 'error': 'apply ได้เฉพาะ class "deleted" เท่านั้น'}

        payload = json.loads(row['latest_payload_json'])
        payload_rows = payload['rows']

        for r in payload_rows:
            customer = (r.get('customer') or '').strip()
            if customer in PLATFORM_STOCK_DEDUCT_CUSTOMERS:
                c.rollback()
                return {'ok': False, 'error': _PLATFORM_REFUSAL_MSG}

        live_rows = _payload_for_doc(c, row['doc_base'])
        cas_err = _cas_compare(payload_rows, live_rows)
        if cas_err:
            c.rollback()
            return {'ok': False, 'error': cas_err}

        ledger_err = _ledger_check(c, payload_rows)
        if ledger_err:
            c.rollback()
            return {'ok': False, 'error': ledger_err}

        # All refusal checks passed — mutate.
        doc_nos = [r['doc_no'] for r in payload_rows]
        row_ids = [r['id'] for r in payload_rows]
        _delete_stock_sync_txns(c, doc_nos)
        _delete_sales_rows(c, row_ids)
        _clean_review_docs(c, row['doc_base'])
        c.execute(
            "UPDATE express_reconcile_flags SET state='applied', resolved_by=?, "
            "resolved_at=datetime('now','localtime') WHERE id=?",
            (resolved_by, flag_id))
        _write_event(c, flag_id, 'open', 'applied', 'deleted', 'deleted',
                    resolved_by, 'ยืนยันลบตาม Express')
        c.commit()
        return {'ok': True}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
