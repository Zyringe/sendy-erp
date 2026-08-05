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

# Public (no leading underscore): the /reconcile template renders this
# verbatim next to the disabled apply button, so both the UI button state
# AND the POST guard show/enforce the identical message (plan §2 step 3).
PLATFORM_REFUSAL_MSG = (
    'บิลนี้เคยหักสต็อกแพลตฟอร์ม — ระบบย้อนให้อัตโนมัติไม่ได้ '
    'ต้องจัดการ platform stock เองก่อน แล้ว dismiss พร้อมโน้ต')


def is_platform_doc(payload_rows):
    """True when ANY row's customer — normalized the SAME way the sync's
    deduction lookup does, `(customer or '').strip()` — is a platform-
    stock-deducting customer (PLATFORM_STOCK_DEDUCT_CUSTOMERS). The ONE
    shared helper driving BOTH the /reconcile page's button state and the
    apply POST guard (plan §2 step 3, Codex r6): a whitespace-bearing
    customer that deducted stock at sync time must grey the button, not
    just refuse server-side after the click."""
    return any((r.get('customer') or '').strip() in PLATFORM_STOCK_DEDUCT_CUSTOMERS
               for r in payload_rows)


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


def _clear_suppression_on_reappear(conn, doc_base, actor):
    """A doc reappearing in the file is new evidence — a LATER disappearance
    must be free to open a fresh flag, so suppression on EVERY dismissed+
    suppressed flag for this doc_base (any class — plan §2: "A different
    class was never suppressed and opens normally" implies suppression is
    genuinely per (doc_base, class), and reappearance is doc-level evidence
    that clears all of them) is cleared automatically, each write audited.
    The flag row itself stays 'dismissed' — only the column that gates
    re-detection changes (plan §2 epoch-end way 2)."""
    rows = conn.execute(
        "SELECT id, class FROM express_reconcile_flags "
        "WHERE doc_base=? AND state='dismissed' AND suppression_active=1",
        (doc_base,)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE express_reconcile_flags SET suppression_active=0 WHERE id=?",
            (r['id'],))
        _write_event(conn, r['id'], 'dismissed', 'dismissed', r['class'], r['class'],
                    actor, 'เอกสารกลับมาปรากฏใน Express — ยกเลิก suppression')
    return len(rows)


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
                _clear_suppression_on_reappear(c, doc_base, actor)
                continue
            outcome = _upsert_flag(c, doc_base, cls, evidence, actor)
            if outcome in ('opened', 'refreshed'):
                counts[cls] += 1

    return counts


# ── Read / lifecycle ─────────────────────────────────────────────────────────

def list_open_reconcile_flags(conn=None):
    """Every open flag, enriched with its latest payload + linked-records
    panel — the /reconcile page renders straight off this, no separate
    detail route (plan §2: "New small page")."""
    with _ConnCtx(conn) as c:
        rows = c.execute(
            "SELECT id, doc_base, class, first_seen_at, last_seen_at, state, "
            "latest_payload_json FROM express_reconcile_flags WHERE state='open' "
            "ORDER BY class = 'deleted' DESC, last_seen_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d['latest_payload'] = json.loads(d.pop('latest_payload_json'))
            d['linked_records'] = _linked_records(c, d['doc_base'])
            d['platform_blocked'] = is_platform_doc(d['latest_payload']['rows'])
            out.append(d)
        return out


def list_resolved_reconcile_flags(conn=None, limit=200):
    """Applied/dismissed/reappeared flags — history view (?all=1). Summary
    fields only; the forensic payloads are on get_reconcile_flag() if a
    future page needs the full detail."""
    with _ConnCtx(conn) as c:
        rows = c.execute(
            "SELECT id, doc_base, class, state, first_seen_at, last_seen_at, "
            "resolved_by, resolved_at, resolution_note, suppression_active "
            "FROM express_reconcile_flags WHERE state != 'open' "
            "ORDER BY resolved_at DESC LIMIT ?", (limit,)
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


def _linked_records_summary(linked):
    """Short human-readable summary of §4's linked-records panel, recorded
    into the flag's resolution_note (and the apply event's note) so what was
    shown BEFORE apply is preserved AFTER the doc's own rows are gone
    (plan §4: 'shown BEFORE apply, recorded in the resolution note')."""
    parts = [f'{k}={len(v)}' for k, v in linked.items() if v]
    return f"อ้างอิง: {', '.join(parts)}" if parts else 'ไม่มีข้อมูลอ้างอิง'


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
        for field in ('id', 'product_id', 'customer', 'synced_to_stock', 'ref_invoice'):
            if (exp.get(field) or None) != (live.get(field) or None):
                return f'ข้อมูลเปลี่ยนไปตั้งแต่สแกนล่าสุด ({field} ของ {k}) — สแกนใหม่ก่อน apply'
    return None


def _ledger_check(conn, payload_rows):
    """Both directions (plan §2d), mirroring _sync_bsn_to_stock's writer
    rule EXACTLY (bsn_sync.py ~215-233) instead of trusting a row that
    merely shares reference_no+note: every synced line must have exactly
    ONE transactions row whose product_id, exact note, txn_type, AND signed
    quantity_change (sign unconditionally, magnitude whenever
    _get_base_qty can compute it) all match what the writer would have
    produced for THAT line; every unsynced line has none; and NO other
    transactions row references any of this doc's doc_nos at all (catches
    history_import compensator legs and anything else unexpected).

    A corrupted/stale row that happens to share reference_no+note but has
    the WRONG product_id, direction, or note-type (e.g. a plain 'BSN ขาย'
    row sitting where an SR line's 'BSN ขาย-คืน' belongs) must NEVER be
    silently trusted — deleting it would fire the mig-080 stock trigger
    against the wrong product or in the wrong direction.

    Returns (error_message, None) on refusal, or (None, matched_ids) on
    success — matched_ids is the EXACT set of transactions.id verified to
    belong to this doc's synced lines. The caller must delete ONLY those
    ids, never a broader reference_no+note sweep (see apply_reconcile_flag).

    Known, deliberate limitation: when 2+ sales_transactions rows share the
    SAME literal doc_no (a real Express data shape — e.g. two STCRD lines
    reusing one SEQNUM, seen live on IV6900582-1), _sync_bsn_to_stock wrote
    a transactions row per line, both stamped with that identical
    reference_no. This function has no way to tell WHICH row belongs to
    WHICH payload line (they key identically on reference_no+note), so the
    exact-match count fires for every payload row sharing that doc_no and
    the whole doc refuses with "มี ledger แปลกปลอม" — even though nothing
    is actually wrong. This is the safe direction (never a wrong delete):
    such docs stay flagged forever as `deleted`-but-apply-refused and need
    จัดการมือ. Not a bug to fix here; documented so it isn't mistaken for
    one."""
    doc_nos = sorted({r['doc_no'] for r in payload_rows})
    if not doc_nos:
        return None, set()
    ph = ','.join('?' * len(doc_nos))
    all_txns = conn.execute(
        f"SELECT id, reference_no, note, quantity_change, product_id, txn_type "
        f"FROM transactions WHERE reference_no IN ({ph})", doc_nos).fetchall()
    by_doc_no = defaultdict(list)
    for t in all_txns:
        by_doc_no[t['reference_no']].append(t)

    matched_ids = set()
    for r in payload_rows:
        doc_no = r['doc_no']
        candidates = by_doc_no.get(doc_no, [])
        # Mirrors bsn_sync.py's is_sales_return / row_txn_type / note
        # derivation exactly — the ONLY two shapes a real sales sync ever
        # writes: a normal line (OUT, negative, 'BSN ขาย') or an SR return
        # line (IN, positive, 'BSN ขาย-คืน').
        is_sr = doc_no.startswith('SR')
        expected_note = 'BSN ขาย-คืน' if is_sr else 'BSN ขาย'
        expected_txn_type = 'IN' if is_sr else 'OUT'
        exact_rows = [t for t in candidates if t['note'] == expected_note]

        if r.get('synced_to_stock'):
            if len(exact_rows) != 1:
                return f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ', None
            txn = exact_rows[0]
            if txn['product_id'] != r['product_id']:
                return f'ledger ไม่ตรงกับข้อมูลของ {doc_no} (สินค้าไม่ตรง) — จัดการมือ', None
            if txn['txn_type'] != expected_txn_type:
                return f'ledger ไม่ตรงกับข้อมูลของ {doc_no} (ทิศทางไม่ตรง) — จัดการมือ', None
            qty_sign_ok = (txn['quantity_change'] < 0) == (not is_sr)
            if not qty_sign_ok:
                return f'ledger ไม่ตรงกับข้อมูลของ {doc_no} (เครื่องหมายไม่ตรง) — จัดการมือ', None
            product = conn.execute(
                "SELECT unit_type FROM products WHERE id=?", (r['product_id'],)).fetchone()
            if product is not None:
                expected_base = _get_base_qty(
                    conn, r['product_id'], product['unit_type'] or '', r['unit'], r['qty'] or 0)
                if expected_base is not None:
                    expected_signed = expected_base if is_sr else -expected_base
                    if abs(txn['quantity_change'] - expected_signed) >= 1e-9:
                        return f'ledger ไม่ตรงกับข้อมูลของ {doc_no} (qty ไม่ตรง) — จัดการมือ', None
            matched_ids.add(txn['id'])
        else:
            if exact_rows:
                return f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ', None

    for doc_no, candidates in by_doc_no.items():
        for t in candidates:
            if t['id'] not in matched_ids:
                return (f'มี ledger แปลกปลอมสำหรับ {doc_no} — จัดการมือ '
                        '(อาจเป็น history_import compensator หรืออื่นๆ)'), None
    return None, matched_ids


def _delete_verified_txns(conn, txn_ids):
    """Delete EXACTLY the transactions rows _ledger_check verified belong to
    this doc's synced lines — by id, never by a broader reference_no+note
    sweep. A row that shares reference_no+note but has the wrong product_id/
    txn_type/sign was already refused by _ledger_check and is NOT in
    txn_ids, so it is never touched here either."""
    if not txn_ids:
        return
    ids = list(txn_ids)
    ph = ','.join('?' * len(ids))
    conn.execute(f"DELETE FROM transactions WHERE id IN ({ph})", ids)


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

        if is_platform_doc(payload_rows):
            c.rollback()
            return {'ok': False, 'error': PLATFORM_REFUSAL_MSG}

        live_rows = _payload_for_doc(c, row['doc_base'])
        cas_err = _cas_compare(payload_rows, live_rows)
        if cas_err:
            c.rollback()
            return {'ok': False, 'error': cas_err}

        ledger_err, verified_txn_ids = _ledger_check(c, payload_rows)
        if ledger_err:
            c.rollback()
            return {'ok': False, 'error': ledger_err}

        # All refusal checks passed — capture the linked-records panel AS
        # SHOWN before mutating anything (plan §4), then mutate.
        linked = _linked_records(c, row['doc_base'])
        note = f"ยืนยันลบตาม Express | {_linked_records_summary(linked)}"

        row_ids = [r['id'] for r in payload_rows]
        # Delete by the EXACT ids _ledger_check verified (product_id/note/
        # txn_type/sign/magnitude all checked) — never a broader reference_
        # no+note sweep that could catch a corrupted/stale row sharing those
        # two fields but pointing at the wrong product or direction.
        _delete_verified_txns(c, verified_txn_ids)
        _delete_sales_rows(c, row_ids)
        _clean_review_docs(c, row['doc_base'])
        c.execute(
            "UPDATE express_reconcile_flags SET state='applied', resolved_by=?, "
            "resolved_at=datetime('now','localtime'), resolution_note=? WHERE id=?",
            (resolved_by, note, flag_id))
        _write_event(c, flag_id, 'open', 'applied', 'deleted', 'deleted',
                    resolved_by, note)
        c.commit()
        return {'ok': True}
    except Exception:
        c.rollback()
        raise
    finally:
        if own:
            c.close()
