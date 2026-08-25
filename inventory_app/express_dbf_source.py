"""Server-side Express (DBF) reader + record adapters — Phase 1 slices A+B
(sales/purchase, plus payments_in/payments_out/credit_notes_ar/credit_notes_ap).

Self-contained: does NOT import projects/express-integration/express_dbf.py
(that read-only helper lives outside this repo and can't be imported on
Railway). The cp874 + LenientFieldParser + \\xa0-normalize gotchas it bakes
in are duplicated here (~15 lines) rather than shared — see
projects/express-integration/plan.md §3.

Two layers, split for testability:
  - open_table(): thin dbfread IO. Returns a list of cleaned dict rows (a
    list, not a generator, so callers can index/join without exhausting it).
  - build_sales_entries / build_purchase_entries / build_invoice_refs: PURE
    functions over already-read lists-of-dicts — no file IO — so the
    filter/join/trap logic is unit-testable with hand-written dict fixtures
    (see tests/test_express_dbf_source.py). No DBF files needed for tests.

Field mapping + the 3 field-selection traps are per
projects/express-integration/MAPPING.md (Phase 0, verified 2026-07-08 by a
full 3-way reconciliation against Sendy's existing sales_transactions /
purchase_transactions). Do not rediscover them:
  1. sales/purchase scope is ARTRN/APTRN.RECTYP IN ('3','1','5') = IV/RR
     (credit), HS/HP (cash), SR/GR (credit-note LINE items also stored in
     sales_/purchase_transactions by the text-report importer). '9' (RE/PS
     payments) and '7' (OE orders) are out of scope.
  2. SR (sales) / GR (purchase) lines use STCRD.TRNVAL for `net`, NOT
     NETVAL — NETVAL is VAT-stripped/post-discount, but Sendy's ledgers
     store the pre-discount TRNVAL for these credit-note lines.
  3. vat_type is doc-level (ARTRN/APTRN.FLGVAT) — STCRD.VATCOD is always
     blank, so every line of a doc gets the header's FLGVAT.

Slice B adds build_payments_in_records / build_payments_out_records /
build_credit_notes_ar_records / build_credit_notes_ap_records — same PURE,
dict-fixture-testable shape. Their traps (MAPPING.md §3-6, Phase 0):
  4. payments_in: ARTRN RECTYP='9' (RE) header money fields are always 0 —
     total is Σ ARRCPIT.RCVAMT (IV lines only); SR lines are unsigned in
     DBF but must sign-flip negative (Sendy's netting-link convention).
  5. payments_out: invoice_amount = APTRN.RCVAMT, NOT PAYAMT (PAYAMT
     diverges arbitrarily, sometimes exactly 2x the correct value).
  6. credit_notes_ap: total = Σ STCRD.TRNVAL, NOT NETVAL — same VAT-strip
     trap as #2, independently confirmed on the AP side (GR6700021).
Each builder feeds its existing downstream importer directly (a records
list, not a file path) — see import_router.py::commit_express_dbf.
"""
import collections
import hashlib
import os
from collections import defaultdict

from dbfread import DBF, FieldParser


class LenientFieldParser(FieldParser):
    """Tolerate Express's occasional malformed date/number bytes (return None
    instead of raising) — mirrors express_dbf.py's LenientFieldParser."""

    def parseD(self, field, data):
        try:
            return super().parseD(field, data)
        except (ValueError, TypeError):
            return None

    def parseN(self, field, data):
        try:
            return super().parseN(field, data)
        except (ValueError, TypeError):
            return None


def _clean(v):
    return v.replace("\xa0", " ").strip() if isinstance(v, str) else v


def open_table(dataset_dir, name):
    """Read one Express DBF table (e.g. 'STCRD', 'ARTRN') into a list of dict
    rows. Char fields have \\xa0 normalized to space and are stripped."""
    path = os.path.join(dataset_dir, f"{name.upper()}.DBF")
    tbl = DBF(
        path,
        encoding="cp874",
        ignore_missing_memofile=True,
        parserclass=LenientFieldParser,
    )
    return [{k: _clean(v) for k, v in rec.items()} for rec in tbl]


# RECTYP codes shared by sales (ARTRN) and purchase (APTRN): '3'=IV/RR,
# '1'=HS/HP cash docs, '5'=SR/GR credit-note lines. Phase 0's gate (Sendy-only
# doc count == 0) only passes with all three included — see MAPPING.md.
_SCOPE_RECTYP = ('3', '1', '5')
_CREDIT_NOTE_RECTYP = '5'  # SR (sales) / GR (purchase): net = TRNVAL, not NETVAL

# APTRN.DOCSTAT: 'N'/'M' are live documents, 'C' is cancelled. Verified on the
# AP side only: of the credit notes present in both BSN5657 and Sendy, DOCSTAT
# =='C' agrees with the text report's own is_void 33/33, including the one
# genuinely voided GR6700007. ⚠ The AR side (ARTRN, payments_in) shows 49 DBF
# 'C' against 2 Sendy cancelled rows — that question is still open and this
# constant must NOT be wired there on the strength of the AP evidence.
_CANCELLED_DOCSTAT = 'C'


def _num(row, field):
    v = row.get(field)
    return float(v) if v is not None else 0.0


def _int(row, field, default=0):
    v = row.get(field)
    return int(v) if v is not None else default


def _date_iso(d):
    """An optional DBF date as ISO, or None. Unlike _header_date_iso this does
    NOT fail loud: these are genuinely optional columns (a billing note that has
    not been sent has no BILOUT), so a blank is data, not corruption."""
    return d.isoformat() if d is not None else None


def _header_date_iso(hdr):
    d = hdr.get('DOCDAT')
    if d is None:
        # DOCDAT is a real datetime.date on every header row Phase 0 checked
        # (verified facts, MAPPING.md). A None here means LenientFieldParser
        # hit malformed bytes on a field that should never be malformed —
        # fail loud rather than silently emit a bad date_iso into the ledger.
        raise ValueError(f"DOCDAT missing/malformed for doc {hdr.get('DOCNUM')!r}")
    return d.isoformat()


def _in_window(hdr, cutoff):
    """True if cutoff is None (no filter — the old, unfiltered behavior every
    existing caller/test gets by default) or hdr's DOCDAT >= cutoff.

    A missing/malformed DOCDAT is treated as OUT of a windowed run rather
    than letting it reach _header_date_iso()'s raise later — scoping which
    docs to process is a cheaper, safer place to drop a bad row than mid-way
    through building its entries. cutoff=None (unfiltered) keeps the old
    fail-loud-on-bad-date behavior unchanged."""
    if cutoff is None:
        return True
    d = hdr.get('DOCDAT')
    return d is not None and d >= cutoff


def build_sales_entries(artrn_rows, stcrd_rows, armas_rows, cutoff=None):
    """Build sales entries — the SAME shape parse_weekly.parse_sales emits —
    from already-read Express DBF rows. Pure: no file IO, so a caller can
    build entries with plain dict fixtures for tests.

    cutoff (a datetime.date, or None): when given, only headers with
    DOCDAT >= cutoff are kept — the recency window (import_router's
    since_days) that keeps a daily full-history DBF upload fast. Filtering
    the HEADERS set (not the STCRD lines directly) is what keeps a doc's
    lines all-or-nothing consistent, and is also the only thing that makes
    the STCRD loop below skip almost all of its lines cheaply (a dict miss,
    no field parsing) instead of models.import_weekly() diffing every one
    against the DB — that per-row diffing is what made a full-history
    upload take 12+ minutes before this filter existed."""
    headers = {r['DOCNUM']: r for r in artrn_rows
               if r.get('RECTYP') in _SCOPE_RECTYP and _in_window(r, cutoff)}
    names = {r['CUSCOD']: r['CUSNAM'] for r in armas_rows}

    entries = []
    for line in stcrd_rows:
        hdr = headers.get(line.get('DOCNUM'))
        if hdr is None:
            continue
        is_credit_note = hdr.get('RECTYP') == _CREDIT_NOTE_RECTYP
        entries.append({
            'date_iso':         _header_date_iso(hdr),
            'doc_no':           f"{line['DOCNUM']}-{_int(line, 'SEQNUM', 1)}",
            'line_seq':         _int(line, 'SEQNUM', 1),
            'qty':              _num(line, 'TRNQTY'),
            'unit':             line.get('TQUCOD') or '',
            'unit_price':       _num(line, 'UNITPR'),
            'vat_type':         _int(hdr, 'FLGVAT', 0),
            'discount':         line.get('DISC') or '',
            'total':            _num(line, 'TRNVAL'),
            'net':              _num(line, 'TRNVAL') if is_credit_note else _num(line, 'NETVAL'),
            'product_name_raw': line.get('STKDES') or '',
            'product_code_raw': line.get('STKCOD') or '',
            'party':            names.get(hdr.get('CUSCOD')) or hdr.get('CUSCOD'),
            'party_code':       hdr.get('CUSCOD'),
        })
    return entries


def build_purchase_entries(aptrn_rows, stcrd_rows, apmas_rows, cutoff=None):
    """Build purchase entries — the SAME shape parse_weekly.parse_purchases
    emits — from already-read Express DBF rows. Pure: no file IO.

    cutoff: see build_sales_entries's docstring — same recency-window
    treatment on the APTRN header set."""
    headers = {r['DOCNUM']: r for r in aptrn_rows
               if r.get('RECTYP') in _SCOPE_RECTYP and _in_window(r, cutoff)}
    names = {r['SUPCOD']: r['SUPNAM'] for r in apmas_rows}

    entries = []
    for line in stcrd_rows:
        hdr = headers.get(line.get('DOCNUM'))
        if hdr is None:
            continue
        is_credit_note = hdr.get('RECTYP') == _CREDIT_NOTE_RECTYP
        entries.append({
            'date_iso':         _header_date_iso(hdr),
            'doc_no':           line['DOCNUM'],  # no line suffix, unlike sales
            'line_seq':         _int(line, 'SEQNUM', 1),
            'qty':              _num(line, 'TRNQTY'),
            'unit':             line.get('TQUCOD') or '',
            'unit_price':       _num(line, 'UNITPR'),
            'vat_type':         _int(hdr, 'FLGVAT', 0),
            'discount':         line.get('DISC') or '',
            'total':            _num(line, 'TRNVAL'),
            'net':              _num(line, 'TRNVAL') if is_credit_note else _num(line, 'NETVAL'),
            'product_name_raw': line.get('STKDES') or '',
            'product_code_raw': line.get('STKCOD') or '',
            'party':            names.get(hdr.get('SUPCOD')) or hdr.get('SUPCOD'),
            'party_code':       hdr.get('SUPCOD'),
        })
    return entries


def build_invoice_refs(artrn_rows, artrnrm_rows, cutoff=None):
    """Build express_invoice_refs rows: doc_base -> (youref, remark), scoped
    to the same sales doc set build_sales_entries uses (IV/HS/SR). Feeds the
    marketplace-IV matcher (buyer-name on YOUREF, #271/#272 — a separate
    project). Docs with neither field populated are skipped (nothing to
    store — most non-marketplace invoices have a blank YOUREF).

    cutoff: same recency window as build_sales_entries — scoped to the same
    doc set that got imported this run."""
    remarks = {}
    for r in artrnrm_rows:
        doc = r.get('DOCNUM')
        remark = (r.get('REMARK') or '').strip()
        if not doc or not remark:
            continue
        remarks[doc] = f"{remarks[doc]} {remark}" if doc in remarks else remark

    refs = []
    for r in artrn_rows:
        if r.get('RECTYP') not in _SCOPE_RECTYP:
            continue
        if not _in_window(r, cutoff):
            continue
        doc = r.get('DOCNUM')
        youref = (r.get('YOUREF') or '').strip() or None
        remark = remarks.get(doc)
        if not youref and not remark:
            continue
        refs.append({'doc_base': doc, 'youref': youref, 'remark': remark})
    return refs


# ── payments_in (RE header + ARRCPIT lines) ─────────────────────────────────

# ARRCPIT line RECTYP: '3'=IV settlement, '5'=SR netting link (unsigned in
# DBF; sign-flipped negative below to match Sendy's convention).
_PAYMENTS_IN_LINE_KIND = {'3': 'IV', '5': 'SR'}


def build_payments_in_records(artrn_rows, arrcpit_rows, armas_rows, cutoff=None, skipped=None):
    """Build payments_in records — the SAME shape models.parse_payment_csv
    emits (re_no, cancelled, date_iso, customer, salesperson, iv_list,
    total) — from Express DBF rows. Feeds models.import_payment_records()
    directly: the CANONICAL received_payments + paid_invoices path, NOT
    express_payments_in (which import_router.commit_file never wires up).

    RE header money fields are always 0 (MAPPING.md §3) — `total` is Σ
    ARRCPIT.RCVAMT for IV lines only, mirroring parse_payment_csv exactly.

    cutoff: recency window on the RE headers (see build_sales_entries).

    An ARRCPIT line whose RECTYP isn't {3=IV, 5=SR} is SKIPPED, not raised —
    real data has 1 such row out of 57,024 (RECTYP='4', a 'DR' doc; Put's
    call: don't crash, don't guess the money, surface it — the correct
    accounting treatment is a separate finance decision). Pass a list via
    `skipped` to collect {re_no, doc, rectyp, amount} dicts for the ones
    dropped; omit it to just skip silently.
    """
    headers = [r for r in artrn_rows if r.get('RECTYP') == '9' and _in_window(r, cutoff)]
    lines_by_rcp = defaultdict(list)
    for line in arrcpit_rows:
        lines_by_rcp[line.get('RCPNUM')].append(line)
    names = {r['CUSCOD']: r['CUSNAM'] for r in armas_rows}

    records = []
    for hdr in headers:
        re_no = hdr['DOCNUM']
        iv_list = []
        for line in lines_by_rcp.get(re_no, []):
            kind = _PAYMENTS_IN_LINE_KIND.get(line.get('RECTYP'))
            if kind is None:
                if skipped is not None:
                    skipped.append({
                        're_no': re_no,
                        'doc': line.get('DOCNUM'),
                        'rectyp': line.get('RECTYP'),
                        'amount': _num(line, 'RCVAMT'),
                    })
                continue
            amount = _num(line, 'RCVAMT')
            if kind == 'SR':
                amount = -abs(amount)
            iv_list.append({'iv_no': line.get('DOCNUM'), 'amount': amount, 'kind': kind})
        records.append({
            're_no': re_no,
            # DOCSTAT='C' vs Sendy's cancelled=1 semantics are an open,
            # non-blocking question (MAPPING.md §3: 49 DBF 'C' vs 2 Sendy
            # cancelled rows) — default False rather than guess.
            'cancelled': False,
            'date_iso': _header_date_iso(hdr),
            'customer': names.get(hdr.get('CUSCOD')) or hdr.get('CUSCOD'),
            'salesperson': hdr.get('SLMCOD') or '',
            'iv_list': iv_list,
            'total': sum(iv['amount'] for iv in iv_list if iv['kind'] == 'IV'),
        })
    return records


# ── payments_out (PS header + APRCPIT lines) ────────────────────────────────

def build_payments_out_records(aptrn_rows, aprcpit_rows, apmas_rows, cutoff=None):
    """Build payments_out records — the dataclasses.asdict() shape of
    parse_express_payments_out.APPayment — from Express DBF rows. Feeds
    import_express.run_import_records('payments_out', ...).

    cutoff: recency window on the PS headers (see build_sales_entries).

    TRAP (MAPPING.md §4): invoice_amount must be APTRN.RCVAMT, NOT PAYAMT
    (PAYAMT diverges arbitrarily on 6/24 matched docs, three of them
    exactly 2x the correct value).

    The settlement breakdown comes off the HEADER, not the APRCPCQ cheque
    table MAPPING.md §4 flagged as unverified: CSHPAY + CHQPAY + DISCAMT
    - INTPAY == RCVAMT on 1985/1985 of BSN5657's PS headers (verified
    2026-08-20), and CSHPAY/CHQPAY tie to the text report's own split on
    275/275 shared documents. 17 headers carry no split at all — 7 interest
    offsets whose RCVAMT is negative, 10 ฿0.00 documents — and 0.0 is the
    right answer for every one of them.

    deposit_applied and vat_amount stay 0.0 (no confirmed field), and
    receive_refs.receive_date_iso / invoice_ref are likewise left None
    rather than guessing a DBF field name that was never verified.
    """
    headers = [r for r in aptrn_rows if r.get('RECTYP') == '9' and _in_window(r, cutoff)]
    lines_by_rcp = defaultdict(list)
    for line in aprcpit_rows:
        lines_by_rcp[line.get('RCPNUM')].append(line)
    names = {r['SUPCOD']: r['SUPNAM'] for r in apmas_rows}

    records = []
    for hdr in headers:
        doc_no = hdr['DOCNUM']
        receive_refs = [
            {
                'receive_doc': line.get('DOCNUM'),
                'receive_date_iso': None,
                'invoice_ref': None,
                # a GR credit applied to the payment is stored UNSIGNED, exactly
                # like ARRCPIT's SR lines: Σ refs ties to RCVAMT on 1719/1985 PS
                # headers as stored, 1985/1985 once negated.
                'amount': (-_num(line, 'PAYAMT') if line.get('RECTYP') == _CREDIT_NOTE_RECTYP
                           else _num(line, 'PAYAMT')),
            }
            for line in lines_by_rcp.get(doc_no, [])
        ]
        records.append({
            'doc_no': doc_no,
            'date_iso': _header_date_iso(hdr),
            'supplier_name': names.get(hdr.get('SUPCOD')) or hdr.get('SUPCOD'),
            'is_void': hdr.get('DOCSTAT') == _CANCELLED_DOCSTAT,
            'deposit_applied': 0.0,
            'invoice_amount': _num(hdr, 'RCVAMT'),   # RCVAMT, not PAYAMT — trap
            'cash_amount': _num(hdr, 'CSHPAY'),
            'cheque_amount': _num(hdr, 'CHQPAY'),
            'interest_amount': _num(hdr, 'INTPAY'),
            'discount_amount': _num(hdr, 'DISCAMT'),
            'vat_amount': 0.0,
            'cheque_no': '',
            'cheque_date_iso': '',
            'bank': '',
            'cheque_status': '',
            'note': hdr.get('YOUREF') or '',
            'receive_refs': receive_refs,
        })
    return records


# ── credit_notes_ar (SR header, no lines — credit_note_amounts) ────────────

def build_credit_notes_ar_records(artrn_rows, armas_rows, cutoff=None):
    """Build credit_notes_ar records — feeds
    import_credit_notes.import_credit_note_amounts_records() directly: the
    HEADER-level credit_note_amounts table (authoritative per-SR credited
    amount, mig 062). Per MAPPING.md §5: ARTRN RECTYP='5' AND DOCNUM starts
    with 'SR'.

    This targets a DIFFERENT table than the SR LINE items
    build_sales_entries (slice A) already writes into sales_transactions —
    same source rows, deliberately different (both correct) numbers by
    design. Do not unify; see MAPPING.md's "SR/GR-in-ledger duality" note.

    cutoff: recency window on the SR headers (see build_sales_entries).
    """
    names = {r['CUSCOD']: r['CUSNAM'] for r in armas_rows}
    records = []
    for r in artrn_rows:
        if r.get('RECTYP') != _CREDIT_NOTE_RECTYP:
            continue
        doc = r.get('DOCNUM') or ''
        if not doc.startswith('SR'):
            continue
        if not _in_window(r, cutoff):
            continue
        records.append({
            'sr_doc_base': doc,
            'ref_invoice': r.get('SONUM') or None,
            'credited_amount': _num(r, 'TOTAL'),
            'sr_date_iso': _header_date_iso(r),
            'customer': names.get(r.get('CUSCOD')) or r.get('CUSCOD'),
            'source': 'express_dbf',
        })
    return records


# ── credit_notes_ap (GR header + STCRD lines) ───────────────────────────────

def _ref_doc_base(rdocnum):
    """'RR6700025     6' -> 'RR6700025' (strip the embedded line-sequence
    suffix). Blank/missing -> None — MAPPING.md §6's open edge case: 3 of 33
    docs have a blank RDOCNUM with no other DBF field found; NULL is more
    honest than a placeholder."""
    tokens = (rdocnum or '').split()
    return tokens[0] if tokens else None


def build_credit_notes_ap_records(aptrn_rows, stcrd_rows, apmas_rows, cutoff=None):
    """Build credit_notes_ap records — the dataclasses.asdict() shape of
    parse_express_credit_notes.CreditNote(+CreditNoteLine) — from Express
    DBF rows. Feeds import_express.run_import_records('credit_notes', ...).

    Per MAPPING.md §6: APTRN RECTYP='5' AND DOCNUM starts with 'GR' is the
    header (its own money fields are always 0 — the real total is the
    STCRD line sum). TRAP: total = Σ STCRD.TRNVAL, NOT NETVAL (NETVAL is
    VAT-stripped/post-discount; Sendy's express_credit_notes.total_amount
    stores the pre-VAT-strip TRNVAL — same trap as SR-in-sales/GR-in-purchase).

    cutoff: recency window on the GR headers (see build_sales_entries).
    """
    names = {r['SUPCOD']: r['SUPNAM'] for r in apmas_rows}
    lines_by_doc = defaultdict(list)
    for line in stcrd_rows:
        lines_by_doc[line.get('DOCNUM')].append(line)

    records = []
    for hdr in aptrn_rows:
        if hdr.get('RECTYP') != _CREDIT_NOTE_RECTYP:
            continue
        doc = hdr.get('DOCNUM') or ''
        if not doc.startswith('GR'):
            continue
        if not _in_window(hdr, cutoff):
            continue
        lines = sorted(lines_by_doc.get(doc, []), key=lambda l: _int(l, 'SEQNUM', 1))
        records.append({
            'doc_no': doc,
            'date_iso': _header_date_iso(hdr),
            'supplier_name': names.get(hdr.get('SUPCOD')) or hdr.get('SUPCOD'),
            'ref_doc': _ref_doc_base(lines[0].get('RDOCNUM')) if lines else None,
            'v_flag': 0,
            'discount': 0.0,
            'vat': 0.0,
            'total': sum(_num(l, 'TRNVAL') for l in lines),   # TRNVAL, not NETVAL — trap
            'is_cleared': False,
            'is_void': hdr.get('DOCSTAT') == _CANCELLED_DOCSTAT,
            'type_code': None,
            'note': hdr.get('YOUREF') or '',
            'lines': [
                {
                    'line_no': _int(l, 'SEQNUM', 1),
                    'product_code': l.get('STKCOD') or '',
                    'product_name': l.get('STKDES') or '',
                    'qty': _num(l, 'TRNQTY'),
                    'unit': l.get('TQUCOD') or '',
                    'unit_price': _num(l, 'UNITPR'),
                    'discount': l.get('DISC') or '',
                    'line_total': _num(l, 'TRNVAL'),
                    'is_cleared': False,
                }
                for l in lines
            ],
        })
    return records


# ── AR / AP outstanding snapshots ────────────────────────────────────────────
#
# The daily zip's "ลูกหนี้คงค้าง / เจ้าหนี้คงค้าง" side. Mapping verified against
# the 2026-06-05 prod snapshot (95 comparable rows tied field-for-field) — see
# docs/plans/2026-08-17-daily-ar-ap-snapshot-from-dbf.md.
#
# Deliberately NO cutoff parameter, unlike every builder above: an outstanding
# balance is as-of-now regardless of the document's age, and the real snapshot
# carries unpaid docs dated 2009. Passing one is a caller bug, so it raises.

# Express prints a Thai label for the customer/supplier TYPE code; the DBF
# stores only the code. These maps are read back off the text-report snapshots
# Express itself produced, and cover 100% of the open rows in both books. An
# unmapped code falls through to the raw code rather than blanking the column —
# wrong-looking beats silently-empty, and no total depends on it.
_AR_CUSTOMER_TYPE_LABELS = {
    '00': 'ลูกค้าประจำ',
    '01': 'ลูกค้าประจำ (ซาปั้ว)',
    '02': 'ตัวแทนจำหน่าย(ยี่ปั้ว)',
    '05': 'ซื้อภายใน',
}
_AP_SUPPLIER_TYPE_LABELS = {
    '00': 'ผู้จำหน่ายประจำ',
    '03': 'ผู้ค้าส่ง',
}

# RE (sales) / PS (purchase) receipt rows. Their header money fields are 0
# (MAPPING trap #4), so ยอดบิล has to be rebuilt from paid + remaining, and
# Express flags them in the report with both a leading '!' and a trailing '***'.
_RECEIPT_RECTYP = '9'
# SR/GR credit notes sit positive in the DBF but REDUCE the balance. Same code as
# _CREDIT_NOTE_RECTYP above — aliased rather than redefined so the two can't drift.
_CREDIT_RECTYP = _CREDIT_NOTE_RECTYP


def _open_balance(row, paid_field='RCVAMT'):
    """(paid, outstanding_raw) rounded to satang, or None when the doc is
    settled. Rounding before the zero-test is load-bearing: REMAMT is a double
    and its float noise otherwise reports ~1,100 settled docs as outstanding.

    paid_field: which column actually holds "how much of this document is
    settled". It is RCVAMT everywhere EXCEPT purchase credit notes — see
    _ap_paid_field."""
    remaining = round(_num(row, 'REMAMT'), 2)
    if remaining == 0:
        return None
    return round(_num(row, paid_field), 2), remaining


def _ap_paid_field(row):
    """APTRN stores the settled amount in different columns by RECTYP, and using
    the wrong one produces a plausible number rather than an error.

    Observed on GR6900005 (BSN5657, 2026-07-31): a purchase credit note carries
    NETAMT == RCVAMT == REMAMT == 1040.25 with PAYAMT 0 — RCVAMT mirrors the credit
    instead of recording a payment, so `bill = paid + remaining` only balances
    against PAYAMT. On ordinary RR invoices the opposite holds: RCVAMT is the paid
    amount (the reading that tied 7/7 to the 2026-05-29 prod snapshot).

    Same family as MAPPING trap #5 on payments_out, where PAYAMT is the unreliable
    one on PS rows. Neither field is safe to use blind; pick by RECTYP.
    """
    return 'PAYAMT' if row.get('RECTYP') == _CREDIT_RECTYP else 'RCVAMT'


def _billed(row, paid, remaining, doc_no):
    """ยอดบิล. NETAMT everywhere except receipt rows, where it is 0 and the
    report prints paid + remaining instead.

    For every other RECTYP `NETAMT == RCVAMT + REMAMT` is an invariant that held
    on 100% of open rows in both books, so a file that breaks it is format
    drift — refuse it rather than publish a wrong ยอดบิล into AR."""
    if row.get('RECTYP') == _RECEIPT_RECTYP:
        return round(paid + remaining, 2)
    billed = round(_num(row, 'NETAMT'), 2)
    if abs(billed - (paid + remaining)) > 0.005:
        raise ValueError(
            f'{doc_no}: NETAMT {billed} != RCVAMT {paid} + REMAMT {remaining} '
            f'— Express format drift, refusing to publish a wrong bill amount')
    return billed


def _reject_duplicate(seen, doc_no, side):
    """ARTRN/APTRN can hold more than one header for the same DOCNUM —
    models/reconcile.py builds on exactly that ("every DOCNUM header as Express
    actually wrote it, including duplicates"). Neither snapshot table has a unique
    constraint, so two OPEN rows for one document would post that balance twice and
    nothing downstream could tell. Refuse instead: a reported failure keeps
    yesterday's snapshot (see _commit_snapshot), a double-count silently inflates
    what we chase."""
    if doc_no in seen:
        raise ValueError(
            f'{doc_no}: appears more than once among open {side} documents — '
            f'refusing rather than counting the balance twice')
    seen.add(doc_no)


def build_ar_snapshot_records(artrn_rows, armas_rows):
    """One record per outstanding AR document, shaped like
    _import_ar_snapshot_records expects (minus the snapshot date, which the
    importer stamps). The text-report parser this shape was originally borrowed
    from is gone — this builder is now the only producer."""
    customers = {(r.get('CUSCOD') or '').strip(): r for r in armas_rows}
    records = []
    seen = set()
    for row in artrn_rows:
        balance = _open_balance(row)
        if balance is None:
            continue
        paid, remaining = balance
        doc_no = (row.get('DOCNUM') or '').strip()
        _reject_duplicate(seen, doc_no, 'AR')
        rectyp = row.get('RECTYP')
        is_receipt = rectyp == _RECEIPT_RECTYP
        code = (row.get('CUSCOD') or '').strip()
        master = customers.get(code)
        type_code = ((master.get('CUSTYP') or '').strip() if master else '')
        records.append({
            'customer_code': code,
            'customer_name': ((master.get('CUSNAM') or '').strip() if master else ''),
            'customer_type': _AR_CUSTOMER_TYPE_LABELS.get(type_code, type_code),
            'doc_date_iso': _header_date_iso(row),
            'doc_no': doc_no,
            'is_anomalous': is_receipt,
            'salesperson_code': (row.get('SLMCOD') or '').strip(),
            'bill_amount': _billed(row, paid, remaining, doc_no),
            'paid_amount': paid,
            # Credit notes reduce the receivable; the report prints them negative.
            'outstanding_amount': -remaining if rectyp == _CREDIT_RECTYP else remaining,
            'has_warning': is_receipt,
            # Doc-level attributes ARTRN has always carried and this adapter used
            # to drop. They live HERE rather than on express_invoice_refs because
            # that table is built inside the 60-day ledger window, which covered
            # only 132 of the 170 invoices /ar chases — the snapshot is windowless
            # by design, so it is the only place every open document is present.
            'due_date_iso': (row['DUEDAT'].isoformat()
                             if row.get('DUEDAT') is not None else None),
            'pay_terms': (int(row['PAYTRM'])
                          if row.get('PAYTRM') not in (None, '') else None),
            # Express writes '~' for "not billed"; stored verbatim it would make
            # every unbilled invoice look like it was already on a ใบวางบิล.
            'bill_no': (lambda b: b if b and b != '~' else None)(
                (row.get('BILNUM') or '').strip()),
        })
    return records


def build_ap_snapshot_records(aptrn_rows, apmas_rows):
    """One record per outstanding AP document, shaped like
    _import_ap_snapshot_records expects (minus the snapshot date). The
    text-report parser this shape came from is gone — this is the only producer
    now."""
    suppliers = {(r.get('SUPCOD') or '').strip(): r for r in apmas_rows}
    records = []
    seen = set()
    for row in aptrn_rows:
        balance = _open_balance(row, _ap_paid_field(row))
        if balance is None:
            continue
        paid, remaining = balance
        doc_no = (row.get('DOCNUM') or '').strip()
        _reject_duplicate(seen, doc_no, 'AP')
        code = (row.get('SUPCOD') or '').strip()
        master = suppliers.get(code)
        type_code = ((master.get('SUPTYP') or '').strip() if master else '')
        records.append({
            'supplier_type': _AP_SUPPLIER_TYPE_LABELS.get(type_code, type_code),
            'supplier_name': ((master.get('SUPNAM') or '').strip() if master else ''),
            'supplier_code': code,
            'doc_no': doc_no,
            # REFNUM, not YOUREF: YOUREF is blank on every open AP row observed,
            # while REFNUM tied 7/7 to the prod snapshot's supplier_invoice_no.
            'supplier_invoice_no': (row.get('REFNUM') or '').strip(),
            'doc_date_iso': _header_date_iso(row),
            'bill_amount': _billed(row, paid, remaining, doc_no),
            'paid_amount': paid,
            # A purchase credit note reduces what we owe — same sign convention
            # the AR side uses for SR, which is tied to the Express report.
            'outstanding_amount': (-remaining if row.get('RECTYP') == _CREDIT_RECTYP
                                   else remaining),
        })
    return records


# ── ใบวางบิล (ARBIL) ────────────────────────────────────────────────────────
#
# `/ar` cannot otherwise tell an invoice nobody has billed yet from one that has
# been formally billed and is sitting in the customer's payment run — two very
# different phone calls. Measured on the 2026-08-17 export: 17 of the 170
# invoices it would chase (฿107,845) were already on a ใบวางบิล.
#
# Deliberately takes NO cutoff. The bills that currently-open invoices point at
# are dated 2014-02-01 .. 2026-07-25 and a 60-day window would miss 11 of the
# 19. The whole table is 11,925 rows, so importing all of it costs nothing and
# is the only way a bill_no on an old invoice ever resolves to a name and date.

def build_billing_note_records(arbil_rows, armas_rows):
    """One record per ใบวางบิล, keyed on BILNUM (unique across all 11,925 rows
    in the book as of 2026-08-17, asserted below)."""
    customers = {(r.get('CUSCOD') or '').strip(): (r.get('CUSNAM') or '').strip()
                 for r in armas_rows}
    records = []
    seen = set()
    for row in arbil_rows:
        bill_no = (row.get('BILNUM') or '').strip()
        if not bill_no:
            continue
        if bill_no in seen:
            raise ValueError(
                f'{bill_no}: appears more than once in ARBIL — refusing rather '
                f'than letting the (entity, bill_no) upsert keep whichever row '
                f'happened to come last')
        seen.add(bill_no)
        code = (row.get('CUSCOD') or '').strip()
        records.append({
            'bill_no': bill_no,
            'bill_date_iso': _date_iso(row.get('BILDAT')),
            # BILOUT is the date the note actually went out to the customer;
            # APPDAT is when they acknowledged it. Both are commonly blank.
            'sent_date_iso': _date_iso(row.get('BILOUT')),
            'approved_date_iso': _date_iso(row.get('APPDAT')),
            'customer_code': code,
            'customer_name': customers.get(code, ''),
            # Free Thai text as Express stores it ('เครดิต 30 วัน'), not a
            # number — the same customer's terms are worded differently across
            # bills and parsing them would invent precision that is not there.
            'pay_cond': (row.get('PAYCOND') or '').strip(),
            'net_amount': round(_num(row, 'NETAMT'), 2),
            # 138 of 11,925 are DOCSTAT 'C'. Kept rather than dropped: an
            # invoice pointing at a bill_no with no row reads as a data bug.
            'is_cancelled': (row.get('DOCSTAT') or '').strip() == 'C',
            'remark': (row.get('REMARK') or '').strip(),
        })
    return records


# ── ทะเบียนเช็ค (BKTRN) ─────────────────────────────────────────────────────
#
# A customer who paid by post-dated cheque still reads as "owing" in Sendy until
# it clears, so /ar would chase someone who has already paid — 9 such cheques
# worth ฿69,814 on the 2026-08-17 export. It is also the only forward-looking
# cash figure in the book: money whose arrival date is already known.
#
# ⚠ NOTHING here is interpreted. CHQSTAT's six values cannot be decoded from the
# data (status 10 holds both long-cleared cheques and all 9 still in the future,
# so it does not mean "cleared"), and TRNDAT/CHQDAT/GETDAT/PAYINDAT differ on
# 5,451 rows with no consistent ordering. Everything is carried under its DBF
# name; `kind` is the only derived field, and QR/QP is unambiguous. Label the
# rest once someone who knows the book says what they mean.

_BANK_KIND = {'QR': 'received', 'QP': 'paid'}

# Express's "no reference" placeholder, same as in ARBIL.BILNUM and APTRN.REFNUM.
_TILDE = '~'


def _plain(row, field):
    """A trimmed char field, with Express's '~' placeholder read as empty."""
    v = (row.get(field) or '').strip()
    return '' if v == _TILDE else v


def build_bank_cheque_records(bktrn_rows):
    """One record per cheque-register row.

    No cutoff and no duplicate guard, both deliberate and both the opposite of
    build_billing_note_records: post-dated cheques run months ahead so a window
    would drop exactly the rows this exists for, and CHQNUM repeats on 38 of the
    12,805 rows — which is why the importer replaces per entity instead of
    upserting on a key that does not exist.
    """
    records = []
    for row in bktrn_rows:
        type_code = (row.get('BKTRNTYP') or '').strip()
        records.append({
            'kind': _BANK_KIND.get(type_code, 'other'),
            'type_code': type_code,
            'cheque_no': _plain(row, 'CHQNUM'),
            'trn_date_iso': _date_iso(row.get('TRNDAT')),
            'cheque_date_iso': _date_iso(row.get('CHQDAT')),
            'received_date_iso': _date_iso(row.get('GETDAT')),
            'paid_in_date_iso': _date_iso(row.get('PAYINDAT')),
            'bank_code': _plain(row, 'BNKCOD'),
            'branch': _plain(row, 'BRANCH'),
            'bank_account': _plain(row, 'BNKACC'),
            'party_code': _plain(row, 'CUSCOD'),
            'party_name': _plain(row, 'NAME'),
            'amount': round(_num(row, 'AMOUNT'), 2),
            'charge': round(_num(row, 'CHARGE'), 2),
            'vat_amount': round(_num(row, 'VATAMT'), 2),
            'net_amount': round(_num(row, 'NETAMT'), 2),
            'remaining_amount': round(_num(row, 'REMAMT'), 2),
            'status_code': _plain(row, 'CHQSTAT'),
            'remark': _plain(row, 'REMARK'),
            'ref_doc': _plain(row, 'REFDOC'),
            'ref_no': _plain(row, 'REFNUM'),
            'voucher': _plain(row, 'VOUCHER'),
        })
    return records


# ── ใบสั่งขาย (OESO + OESOIT) ───────────────────────────────────────────────
#
# Customer demand that has been ordered but not yet invoiced. The ledger only
# sees a sale once it becomes an IV, so an unfulfilled order is invisible today.
#
# ⚠ Know this before building any "open orders" view: 1,732 of the 9,333 orders
# carry a remaining quantity, but only 20 are dated 2026 (฿112,464). The other
# 1,712 run back to 2003 and are orders nobody ever closed out. Without a date
# filter such a view reports ฿13.98M of demand that does not exist.
#
# DOCSTAT (M 7,545 / N 1,731 / C 57) is carried verbatim, same reasoning as
# BKTRN.CHQSTAT: not decodable from the data, and a guessed label sticks.

def build_sales_order_records(oeso_rows, oesoit_rows, armas_rows):
    """(headers, lines) for ใบสั่งขาย.

    SONUM is unique across all 9,333 rows and (SONUM, SEQNUM) across the 51,940
    lines, so both are refused on collision rather than letting a keyed write
    silently keep whichever row came last — the opposite of build_bank_cheque_
    records, where CHQNUM genuinely repeats and the table is replaced instead.

    No cutoff: orders stay open for years.
    """
    customers = {(r.get('CUSCOD') or '').strip(): (r.get('CUSNAM') or '').strip()
                 for r in armas_rows}
    heads = []
    seen = set()
    for row in oeso_rows:
        so_no = _plain(row, 'SONUM')
        if not so_no:
            continue
        if so_no in seen:
            raise ValueError(f'{so_no}: appears more than once in OESO')
        seen.add(so_no)
        code = _plain(row, 'CUSCOD')
        terms = row.get('PAYTRM')
        heads.append({
            'so_no': so_no,
            'so_date_iso': _date_iso(row.get('SODAT')),
            'customer_code': code,
            'customer_name': customers.get(code, ''),
            'salesperson_code': _plain(row, 'SLMCOD'),
            'your_ref': _plain(row, 'YOUREF'),
            'pay_terms': int(terms) if terms not in (None, '') else None,
            'delivery_date_iso': _date_iso(row.get('DLVDAT')),
            'completed_date_iso': _date_iso(row.get('CMPLDAT')),
            'total': round(_num(row, 'TOTAL'), 2),
            'discount_amount': round(_num(row, 'DISCAMT'), 2),
            'vat_amount': round(_num(row, 'VATAMT'), 2),
            'net_amount': round(_num(row, 'NETAMT'), 2),
            'status_code': _plain(row, 'DOCSTAT'),
        })

    lines = []
    line_seen = set()
    for row in oesoit_rows:
        so_no = _plain(row, 'SONUM')
        # A line whose header is absent could never be shown against an order.
        # Express should not produce one; dropping it keeps the (entity, so_no)
        # shape honest rather than storing an orphan nobody can reach.
        if so_no not in seen:
            continue
        seq = _int(row, 'SEQNUM', 1)
        if (so_no, seq) in line_seen:
            raise ValueError(f'{so_no} line {seq}: duplicated SEQNUM in OESOIT')
        line_seen.add((so_no, seq))
        lines.append({
            'so_no': so_no,
            'line_seq': seq,
            'product_code': _plain(row, 'STKCOD'),
            'product_name': _plain(row, 'STKDES'),
            'ordered_qty': round(_num(row, 'ORDQTY'), 4),
            'cancelled_qty': round(_num(row, 'CANCELQTY'), 4),
            'remaining_qty': round(_num(row, 'REMQTY'), 4),
            'unit': _plain(row, 'TQUCOD'),
            'unit_price': round(_num(row, 'UNITPR'), 4),
            'line_total': round(_num(row, 'TRNVAL'), 2),
        })
    return heads, lines


# ── บัญชีแยกประเภท (GLACC + GLJNL + GLJNLIT) ────────────────────────────────
#
# Sendy's /accounting computes profit from sales minus cost, which is an
# estimate. The GL is the book the accountant actually closes, so this makes an
# independent figure available to check it against.
#
# ⚠ WINDOWED, and for a measured reason. The whole GL is 109,458 vouchers +
# 359,003 lines = 62MB in SQLite. Prod's Railway volume is 434MB with 214MB free
# (measured 2026-08-18); the full book across BOTH books would take ~87MB of
# that before the app's gzip backups grow to match, and a full volume stops
# Sendy writing at all. Three calendar years is ~12% of the rows (~7MB) and
# covers the current and prior fiscal years, which is what checking against a
# closed book needs.
# UPGRADE PATH: raise _GL_SINCE_YEARS, or move the GL to its own book DB the way
# vat_book.db works, once the volume has room.
_GL_SINCE_YEARS = 3

# TRNTYP → side. PROVEN, not guessed, which is why this one is derived while
# CHQSTAT and DOCSTAT are carried raw: account 41-01-00-00 รายได้จากการขาย has
# 53,764 lines and every single one is TRNTYP '1'. Income is credited, so
# 1 = credit and 0 = debit; ลูกหนี้การค้า and เงินสด both agree. '0' and '1' are
# the only values present across all 359,003 lines.
_GL_SIDE = {'0': 'debit', '1': 'credit'}


def gl_cutoff(today=None):
    """1 January, _GL_SINCE_YEARS calendar years back. Separate from the
    ledger's rolling since_days window: a fiscal comparison wants whole years,
    not the last 60 days."""
    import datetime as _dt
    today = today or _dt.date.today()
    return _dt.date(today.year - (_GL_SINCE_YEARS - 1), 1, 1)


def build_gl_records(glacc_rows, gljnl_rows, gljnlit_rows, cutoff):
    """(accounts, vouchers, lines).

    cutoff is REQUIRED and applies to vouchers and their lines, never to the
    chart of accounts — 135 rows that both old and new lines point at, so
    windowing it would leave account numbers resolving to nothing.
    """
    accounts = [{
        'account_no': _plain(r, 'ACCNUM'),
        'account_name': _plain(r, 'ACCNAM'),
        'level': _int(r, 'LEVEL', 0),
        'parent_no': _plain(r, 'PARENT'),
        'account_type': _plain(r, 'ACCTYP'),
        'nature': _plain(r, 'NATURE'),
        'status': _plain(r, 'STATUS'),
    } for r in glacc_rows if _plain(r, 'ACCNUM')]

    vouchers = []
    kept = set()
    for r in gljnl_rows:
        voucher = _plain(r, 'VOUCHER')
        if not voucher:
            continue
        d = r.get('VOUDAT')
        if d is None or d < cutoff:
            continue
        if voucher in kept:
            raise ValueError(f'{voucher}: appears more than once in GLJNL')
        kept.add(voucher)
        vouchers.append({
            'voucher': voucher,
            'voucher_date_iso': _date_iso(d),
            'journal_type': _plain(r, 'JNLTYP'),
            'reference_no': _plain(r, 'REFNUM'),
            'description': _plain(r, 'DESCRP'),
            'source_journal': _plain(r, 'SRCJNL'),
            'status': _plain(r, 'DOCSTAT'),
        })

    lines = []
    for r in gljnlit_rows:
        voucher = _plain(r, 'VOUCHER')
        # A line whose voucher was windowed out has nothing to hang from.
        if voucher not in kept:
            continue
        code = _plain(r, 'TRNTYP')
        side = _GL_SIDE.get(code)
        if side is None:
            # '0' and '1' are the only values across all 359,003 lines. A third
            # means the proof above no longer holds, and labelling it anyway
            # would put a wrong side on a money row.
            raise ValueError(
                f'{voucher} line {r.get("SEQIT")}: unknown TRNTYP {code!r} — '
                f'debit/credit is only proven for 0 and 1')
        lines.append({
            'voucher': voucher,
            # SEQIT repeats within a voucher on 3,367 pairs, so it is a label
            # rather than a key — which is why the importer replaces lines
            # wholesale instead of keying them.
            'line_seq': _int(r, 'SEQIT', 0),
            'voucher_date_iso': _date_iso(r.get('VOUDAT')),
            'account_no': _plain(r, 'ACCNUM'),
            'description': _plain(r, 'DESCRP'),
            'entry_side': side,
            'type_code': code,
            'amount': round(_num(r, 'AMOUNT'), 2),
        })
    return accounts, vouchers, lines


# ── F9: what the recency window drops ───────────────────────────────────────
#
# The inverse of _in_window, as a report. Every TRANSACTIONAL builder above is
# scoped by `cutoff`, and each daily run's cutoff has moved FORWARD, so a
# document dated before it is not merely skipped once — it can never be picked
# up by any later run either. For ordinary history that is the intended trade
# (BSN5657's ARTRN starts 2003-02-04 and both outstanding snapshots are
# deliberately windowless, so no BALANCE is lost). For a document Express gains
# *now* under
# an old DOCDAT — a backdated invoice, a late-keyed receipt — it means the doc
# is invisible to Sendy permanently and nothing says so.
#
# Detection only. This changes no window and feeds no page; it exists so the
# evidence is on the table before anyone argues about since_days.

# The RECTYPs `cutoff` actually filters: _SCOPE_RECTYP for sales/purchases/
# invoice_refs/credit-notes, plus _RECEIPT_RECTYP for payments in and out.
# '7' (OE) never reaches a windowed builder, so an old one is not a loss.
_WINDOWED_RECTYP = frozenset(_SCOPE_RECTYP) | {_RECEIPT_RECTYP}

# DBF header field -> the key it is published under. Used to compare duplicate
# headers on EVERY field the report emits: a field left out of this map is one a
# duplicate could still change silently.
_HEADER_FIELD = {'RECTYP': 'rectyp', 'DOCDAT': 'doc_date_iso', 'DOCSTAT': 'docstat',
                 'NETAMT': 'netamt', 'RCVAMT': 'rcvamt', 'REMAMT': 'remamt'}


def build_out_of_window_docs(artrn_rows, aptrn_rows, cutoff,
                             known_ar_docs=(), known_ap_docs=()):
    """One record per DOCUMENT that a run with this `cutoff` would drop.

    cutoff (datetime.date, REQUIRED): the same value import_router derives from
    since_days. None is refused rather than returning [] — a windowless run
    drops nothing, so an empty list there would read as a clean bill of health
    for a question that was never asked. Same stance as the snapshot builders,
    which refuse a cutoff they must not honour.

    known_ar_docs / known_ap_docs: DOCNUMs Sendy already holds on each side,
    from any earlier import (text report, manual full-history backfill, an
    earlier daily run). Out of window AND absent is the actionable finding;
    out of window but held is just history that arrived by another road.
    Deliberately TWO sets rather than one: DOCNUM is unique per book side, and
    the direction a collision fails in is the bad one — an AP doc number that
    happened to match an AR one would mark the AR document "already held" and
    delete a real finding from the report. (Measured 2026-08-17 on BSN5657: 0
    collisions, prefixes disjoint IV/RE/SR/HS vs RR/HP/PS/GR. That is today's
    data, not a constraint the format promises.)

    Money fields are carried RAW, and a caller must value RE/PS from their
    LINES, not from what is returned here. NETAMT is ยอดบิล on IV/RR/SR/HS —
    pinned there by _billed()'s NETAMT == RCVAMT + REMAMT invariant. On a
    receipt it is neither 0 nor the receipt total: measured on BSN5657
    2026-08-17, MAPPING trap #4's "RE header money fields are always 0" holds
    for RCVAMT (0/28,818) and nearly for TOTAL (2) and REMAMT (43), but NETAMT
    is non-zero on 28,501 of 28,818 and agrees with Sigma(ARRCPIT IV lines) on
    only 95.01%, off by as much as ฿13,300 on one document. So it is carried
    verbatim and labelled, never turned into a single derived 'amount'.

    Sorted by (source, doc_date_iso, doc_no) so re-running produces a
    byte-identical report to diff against the last one.
    """
    if cutoff is None:
        raise ValueError(
            'build_out_of_window_docs needs the run\'s cutoff — a windowless '
            'run drops nothing, and reporting [] for it would claim otherwise')

    by_doc = {}
    for source, rows, known in (('ARTRN', artrn_rows, set(known_ar_docs)),
                                ('APTRN', aptrn_rows, set(known_ap_docs))):
        for row in rows:
            if row.get('RECTYP') not in _WINDOWED_RECTYP:
                continue
            if _in_window(row, cutoff):
                continue
            doc_no = row.get('DOCNUM')
            # DOCNUM is unique per book side, not across them.
            key = (source, doc_no)
            docdat = row.get('DOCDAT')
            if key in by_doc:
                # ARTRN/APTRN carry more than one header for some DOCNUMs (see
                # _reject_duplicate). Identical ones are ONE document. Ones that
                # DISAGREE are not mergeable: keeping the first would make both
                # the classification and the value depend on DBF row order,
                # which is exactly the determinism this function promises.
                prev = by_doc[key]
                clash = [f for f, now in (
                    ('RECTYP', row.get('RECTYP')),
                    ('DOCDAT', docdat.isoformat() if docdat is not None else None),
                    ('DOCSTAT', row.get('DOCSTAT')),
                    ('NETAMT', round(_num(row, 'NETAMT'), 2)),
                    ('RCVAMT', round(_num(row, 'RCVAMT'), 2)),
                    ('REMAMT', round(_num(row, 'REMAMT'), 2)),
                ) if prev[_HEADER_FIELD[f]] != now]
                if clash:
                    raise ValueError(
                        f'{source} {doc_no}: duplicate headers disagree on '
                        f'{", ".join(clash)} — refusing rather than publishing '
                        f'whichever row the file happened to list first')
                prev['header_count'] += 1
                continue
            by_doc[key] = {
                'source': source,
                'doc_no': doc_no,
                'rectyp': row.get('RECTYP'),
                # None is data here, not corruption: _in_window drops a header
                # with no usable DOCDAT, so it is one of the losses.
                'doc_date_iso': docdat.isoformat() if docdat is not None else None,
                # RAW. On the AP side DOCSTAT='C' agreed with is_void 33/33,
                # but the AR side is an OPEN question (49 DBF 'C' vs 2 Sendy
                # cancelled — MAPPING.md §3, _CANCELLED_DOCSTAT's warning), so
                # this is carried, never translated into "cancelled".
                'docstat': row.get('DOCSTAT'),
                'netamt': round(_num(row, 'NETAMT'), 2),
                'rcvamt': round(_num(row, 'RCVAMT'), 2),
                'remamt': round(_num(row, 'REMAMT'), 2),
                'header_count': 1,
                'in_sendy': doc_no in known,
            }
    return sorted(by_doc.values(),
                  key=lambda r: (r['source'], r['doc_date_iso'] or '', r['doc_no']))


def build_receipt_values(artrn_rows, arrcpit_rows, armas_rows,
                         aptrn_rows, aprcpit_rows, apmas_rows):
    """{(source, DOCNUM): amount} for RE and PS, from the SAME builders the
    import uses — never re-derived here, because the two sides do NOT agree on
    where a receipt's amount lives and guessing picks a plausible wrong number.

    RE comes from its LINES (Sigma ARRCPIT IV). Its header cannot be trusted:
    MAPPING trap #4's "RE header money fields are always 0" holds for RCVAMT
    (0 of 28,818 non-zero on BSN5657 2026-08-17) but NOT for NETAMT, which is
    non-zero on 28,501 of them and reads exactly like a receipt total. It is
    not one — against the line sum it ties on only 95.01%, off by up to 13,300
    on a single document.

    PS comes from its HEADER's RCVAMT, which is MAPPING trap #5: PAYAMT
    diverges arbitrarily, sometimes by exactly 2x, so the line total is the
    unreliable one on that side.

    Keyed by (source, DOCNUM), never DOCNUM alone: DOCNUM is unique per book
    side, and a collision would let the side read second overwrite the other's
    amount — the same reason build_out_of_window_docs keeps two sets.
    """
    values = {}
    for rec in build_payments_in_records(artrn_rows, arrcpit_rows, armas_rows,
                                         cutoff=None, skipped=[]):
        values[('ARTRN', rec['re_no'])] = round(rec['total'], 2)
    for rec in build_payments_out_records(aptrn_rows, aprcpit_rows, apmas_rows,
                                          cutoff=None):
        values[('APTRN', rec['doc_no'])] = round(rec['invoice_amount'], 2)
    return values


def split_era_findings(rows, era_start):
    """(real, unvalued, empty) for the Sendy-era slice of build_out_of_window_docs.

    This is the report's finding / not-a-finding judgement, so it is a tested
    function rather than a line inside a script: calling a document "0 in every
    field, nothing to collect" retires it from the reader's attention, and that
    verdict has to be earned.

    A RECEIPT (RECTYP '9') can never earn it. Its money lives in its
    ARRCPIT/APRCPIT lines, so a 0 or a missing lookup means the lines are gone
    or unreadable — the exact data loss this audit exists to surface, not
    evidence the document is empty. Those go to `unvalued`, which the report
    must show as a warning.

    Rows dated before era_start are dropped: Sendy never imported that history
    and its absence is expected. A row with NO date is KEPT — it was already
    dropped once by the window (_in_window treats a missing DOCDAT as out), and
    dropping it again here would hide it twice.
    """
    era = [r for r in rows
           if r['doc_date_iso'] is None or r['doc_date_iso'] >= era_start]
    unvalued = [r for r in era if r['rectyp'] == _RECEIPT_RECTYP and not r['value']]
    rest = [r for r in era if not (r['rectyp'] == _RECEIPT_RECTYP and not r['value'])]
    real = [r for r in rest if r['value'] or r['remamt']]
    empty = [r for r in rest if not (r['value'] or r['remamt'])]
    return real, unvalued, empty


# ── document-drift detection (plan-express-drift-detector-2026-08-24) ────────
#
# WHAT THIS ANSWERS, AND WHAT IT DOES NOT
#     `build_out_of_window_docs` answers "did we MISS a document". Nothing
#     answered "is a document we already hold being changed under our feet".
#     Express is edited by people after the fact, and the weekly import only
#     re-reads a 60-day window, so an edit to an older document is permanent and
#     silent — IV6900631 was cancelled at source on 2026-08-22, five months after
#     its own date, and the stale line survived every check until a human deleted
#     it. On the purchase side that same shape moves stock and WACC.
#
# ⛔ IT CANNOT SEE A BUG IN THE BUILDERS. Both sides run through
#     build_sales_entries / build_purchase_entries, which is what makes the
#     comparison meaningful at all (see below) and also means a builder that
#     mis-reads a field mis-reads it identically on both sides. Catching that is
#     the import tests' job, not this one's.
#
# ⛔ IT CANNOT SEE product_name_raw OR party NAME CHANGES. Both are excluded from
#     the fingerprint on measured grounds (see DRIFT_LINE_FIELDS below).
#
# WHY THE COMPARISON GOES THROUGH THE IMPORT'S OWN BUILDERS
#     Sendy does not store what Express holds; it stores what the import MADE of
#     it. Measured 2026-08-25 on the real dataset: comparing raw DBF fields
#     reported 9,551 of 9,551 documents as drift. Each normalisation layer below
#     was found by running, never by reading:
#         raw                                        9,551
#         + through the builders + normalize_unit     2,986
#         + normalize the unit on the Sendy side too  2,944   (old rows hold 'หล')
#         + treat '\xa0' as a space                   1,627
#         + drop product_name_raw from the compare      167
ERA_START = '2024-01-01'

# The fields a line is compared on. `line_seq` is deliberately absent: Sendy's
# value is a 1-based counter per (doc_no, product code) (parse_weekly.py) while
# Express's SEQNUM is the physical line number, so they agree only on
# single-line documents. Measured on prod: including it takes the drift set from
# 116 documents to 1,005, of which 580 are line_seq alone. Sendy's value is not
# derived from Express's, so comparing them could not detect a source change
# even in principle, and it moves neither money nor stock.
DRIFT_LINE_FIELDS = ('code', 'qty', 'unit', 'unit_price', 'total', 'net', 'discount')

# Header fields, in the order _classify_fields reports them.
DRIFT_HEADER_FIELDS = ('date_iso', 'vat_type', 'party_code')


class DriftInputError(ValueError):
    """The inputs cannot support a verdict, so there is no verdict.

    Raised rather than returning an empty finding list, because "no findings"
    and "nothing was looked at" are indistinguishable to a caller and the second
    one has already been mistaken for the first in this project.
    """


class DriftResult:
    """findings + the population they were drawn from.

    `compared_doc_nos` is not a diagnostic: a caller that sees no findings needs
    to know whether that is because everything agreed or because the scoping
    silently matched nothing.
    """

    __slots__ = ('findings', 'compared_doc_nos', 'counters')

    def __init__(self, findings, compared_doc_nos, counters):
        self.findings = findings
        self.compared_doc_nos = compared_doc_nos
        self.counters = counters


def _c_txt(v):
    """None and '' are the same value; \\xa0 is a space. Measured: the \\xa0 rule
    alone accounts for 1,317 documents."""
    return ('' if v is None else str(v)).replace('\xa0', ' ').strip()


def _c_num(v):
    """int 25 and float 25.0 are the same value.

    Deliberately NOT wrapped in a try: a value that will not parse as a number
    would become 0.0 on both sides and compare EQUAL, which is a detector
    reporting "these agree" about data it could not read. Letting it raise makes
    the scan fail visibly instead — the caller already isolates it, so the
    import still succeeds and the page says the scan did not run.
    """
    return round(float(v or 0), 2)


def _c_unit(v, _norm):
    """normalize_unit on BOTH sides — rows written before the import applied it
    still hold Express's raw 2-char code ('หล' for 'โหล'). Idempotent."""
    return _c_txt(_norm(_c_txt(v)))


def _c_line(code, qty, unit, price, total, net, disc, _norm):
    return (_c_txt(code), _c_num(qty), _c_unit(unit, _norm), _c_num(price),
            _c_num(total), _c_num(net), _c_txt(disc))


def _drift_sendy_side(conn, era_start, _norm):
    """Every document Sendy holds from `era_start`, in canonical form."""
    out = {}
    for table, doc_col, party_col in (
            ('sales_transactions', 'doc_base', 'customer_code'),
            ('purchase_transactions', 'doc_no', 'supplier_code')):
        rows = conn.execute(
            f"SELECT {doc_col}, date_iso, vat_type, {party_col}, bsn_code, qty,"
            f" unit, unit_price, total, net, discount"
            f"  FROM {table} WHERE date_iso >= ?", (era_start,))
        for r in rows:
            doc = r[0]
            if doc is None:
                continue
            d = out.setdefault(doc, {'hdr': (r[1], str(r[2]), _c_txt(r[3])),
                                     'lines': []})
            d['lines'].append(_c_line(r[4], r[5], r[6], r[7], r[8], r[9], r[10],
                                      _norm))
    return out


def _drift_express_side(artrn_rows, aptrn_rows, stcrd_rows, armas_rows,
                        apmas_rows, held, conn, _norm):
    """The same documents as Express holds them, scoped to `held`.

    Scoping to the documents Sendy actually holds is not only an optimisation
    (Sendy holds ~9.9k of Express's ~67k, measured 5.3x); documents Express has
    and Sendy does not are `build_out_of_window_docs`'s job, and reporting them
    here would double-report them.
    """
    from models.mapping import _resolve_mapping
    from models.stock_filters import is_non_stock_code
    cache = {}

    def kept(code, unit):
        """Mirror of models/imports.py `if is_ignored and not non_stock_line`.

        ⚠ Mirrored deliberately, not paraphrased from memory: an earlier
        revision of the plan recorded this branch BACKWARDS (as skipping
        non-stock lines) and review caught it. Non-stock lines are KEPT.
        """
        key = (code, unit)
        if key not in cache:
            _pid, is_ignored, _mapped = _resolve_mapping(conn, code, unit)
            cache[key] = not (is_ignored and not is_non_stock_code(code))
        return cache[key]

    # Scope the HEADERS before building, not the entries afterwards. The
    # builders walk every STCRD line and construct a full dict for each one
    # whose DOCNUM is in the header set; with all 74,562 Express headers in
    # scope that is ~237k dicts built to throw ~227k of them away. Filtering
    # here leaves the same STCRD walk doing a dict miss per irrelevant line,
    # which is what the builders' own docstrings say makes them cheap.
    # Measured: 0.68s -> 0.14s on build_sales_entries alone.
    artrn_rows = [r for r in artrn_rows if r.get('DOCNUM') in held]
    aptrn_rows = [r for r in aptrn_rows if r.get('DOCNUM') in held]

    out = {}
    dropped = set()
    for entries in (build_sales_entries(artrn_rows, stcrd_rows, armas_rows),
                    build_purchase_entries(aptrn_rows, stcrd_rows, apmas_rows)):
        for e in entries:
            doc = e['doc_no']
            base = doc.rsplit('-', 1)[0] if '-' in doc else doc
            if base not in held:
                # Redundant after the header filter above and kept anyway: a
                # sales doc_no carries a printed "-N" suffix, so this is the one
                # place the base is derived, and it costs a set lookup.
                continue
            code = _c_txt(e['product_code_raw'])
            unit = _c_unit(e['unit'], _norm)
            if not kept(code, unit):
                dropped.add(base)
                continue
            d = out.setdefault(base, {'hdr': (e['date_iso'], str(e['vat_type']),
                                              _c_txt(e['party_code'])),
                                      'lines': []})
            d['lines'].append(_c_line(code, e['qty'], unit, e['unit_price'],
                                      e['total'], e['net'], e['discount'], _norm))
    return out, (dropped - set(out))


def _side_repr(side):
    return (side['hdr'], sorted(side['lines'], key=repr))


def document_fingerprint(express_side, sendy_side):
    """What a baseline entry is pinned to — BOTH sides, not one.

    ⚠ A Sendy-only fingerprint cannot notice the source changing again, which is
    the entire subject of this detector: the baseline would stay silent through
    exactly the event it is supposed to stop hiding. (The first baseline builder
    stored the Sendy-side hash; regenerate any baseline written before this
    function existed.)
    """
    payload = repr((_side_repr(express_side), _side_repr(sendy_side)))
    return hashlib.blake2b(payload.encode('utf-8'), digest_size=16).hexdigest()


def _classify_fields(express_side, sendy_side):
    """Name WHAT differs, so an alert says more than "this document differs"."""
    hdr = [n for n, a, b in zip(DRIFT_HEADER_FIELDS,
                                express_side['hdr'], sendy_side['hdr']) if a != b]
    ex = collections.Counter(express_side['lines'])
    sy = collections.Counter(sendy_side['lines'])
    if len(express_side['lines']) != len(sendy_side['lines']):
        return hdr, ['LINE_COUNT']
    changed = set()
    for a, b in zip(sorted((ex - sy).elements()), sorted((sy - ex).elements())):
        changed |= {n for n, x, y in zip(DRIFT_LINE_FIELDS, a, b) if x != y}
    return hdr, sorted(changed)


def detect_document_drift(artrn_rows, aptrn_rows, stcrd_rows, armas_rows,
                          apmas_rows, conn, *, baseline=None, export_at=None,
                          era_start=ERA_START):
    """Compare every document Sendy holds against the same document in Express.

    `export_at` is the moment the uploaded dataset was exported, and it must come
    from the caller — the blueprint computes it (`_zip_export_datetime`) and it
    is NOT always authoritative: it falls back to "today" when the zip carries no
    readable DBF timestamp. Passing None says "unknown", and this function will
    then refuse to claim any document was deleted at source, because deciding
    that on a fallback value is how a detector invents an incident.

    `baseline` maps doc_base -> {'fingerprint': ..., 'reason': ...} for
    disagreements already explained (old parser eras, ignore rules that changed
    since). An entry without a reason is refused rather than honoured: a silent
    exemption is closing your eyes, written as code. A baselined document is
    still COMPARED — it goes quiet only while its fingerprint is unchanged.

    Returns a DriftResult. Raises DriftInputError when the inputs cannot support
    a verdict.
    """
    import bsn_units
    if not stcrd_rows:
        raise DriftInputError('STCRD is empty — there is nothing to compare '
                              'against, and "no findings" would be a lie')
    if not artrn_rows and not aptrn_rows:
        raise DriftInputError('both ARTRN and APTRN are empty — the Express side '
                              'has no documents at all')
    if artrn_rows and not armas_rows:
        raise DriftInputError('ARTRN has rows but ARMAS is empty — a partial read')
    if aptrn_rows and not apmas_rows:
        raise DriftInputError('APTRN has rows but APMAS is empty — a partial read')

    baseline = baseline or {}
    for doc, entry in baseline.items():
        if not _c_txt((entry or {}).get('reason')):
            raise DriftInputError(
                f'baseline entry {doc!r} has no reason. An entry that silences a '
                'document without saying why is not a baseline, it is a blindfold')
        if not _c_txt((entry or {}).get('fingerprint')):
            raise DriftInputError(
                f'baseline entry {doc!r} has no fingerprint, so it would silence '
                f'{doc!r} no matter how far it drifts afterwards')

    # One map, read once, instead of per line: normalize_unit() is called on
    # every line of both sides (~150k times on the real dataset).
    unit_map = bsn_units.load_unit_map()

    def _norm(u):
        return unit_map.get(u, u) if u else u

    sendy = _drift_sendy_side(conn, era_start, _norm)
    held = set(sendy)
    express, dropped_by_ignore = _drift_express_side(
        artrn_rows, aptrn_rows, stcrd_rows, armas_rows, apmas_rows, held, conn,
        _norm)

    compared = held & set(express)
    findings = []
    # Built ONCE. The first version called this inside the per-document loop,
    # which rebuilt a 74,562-entry dict for every drifting document and took the
    # measured cost from 3.5s to 5.7s — the same "parsing inside the pair loop"
    # shape that put the import route into a gunicorn WORKER TIMEOUT (PR #376).
    status = _drift_docstat(artrn_rows, aptrn_rows)
    prints = {}

    # ── content ──────────────────────────────────────────────────────────────
    for doc in sorted(compared):
        xp, sd = express[doc], sendy[doc]
        if xp['hdr'] == sd['hdr'] and \
                collections.Counter(xp['lines']) == collections.Counter(sd['lines']):
            continue
        fp = prints[doc] = document_fingerprint(xp, sd)
        if baseline.get(doc, {}).get('fingerprint') == fp:
            continue
        hdr_fields, line_fields = _classify_fields(xp, sd)
        fields = (['hdr:' + f for f in hdr_fields]
                  + ['line:' + f for f in line_fields])
        findings.append({
            'doc_no': doc, 'kind': 'content', 'fields': fields,
            'fingerprint': fp,
            'docstat': _c_txt(status.get(doc)),
            'express_lines': len(xp['lines']), 'sendy_lines': len(sd['lines']),
            'message': (f'เอกสาร {doc} ที่เราถืออยู่ไม่ตรงกับ Express แล้ว — '
                        f'ต่างที่ {", ".join(fields) or "ไม่ระบุ"} '
                        f'(บรรทัด Express {len(xp["lines"])} · Sendy {len(sd["lines"])})'),
        })

    # ── status at source ─────────────────────────────────────────────────────
    # Reported RAW. `DOCSTAT='C'` on the AR side is an open question the code
    # that owns it says is open (`build_payments_in_records` sets cancelled=False
    # unconditionally and says so), so translating it to "ยกเลิก" here would be
    # this detector deciding a question nobody has answered.
    for doc in sorted(compared):
        raw = _c_txt(status.get(doc))
        # Measured on the real 2026-08-25 dataset: in-scope headers carry only
        # 'N' (74,102) and 'C' (460). 'M' and 'R' exist in ARTRN but never on a
        # RECTYP this comparison looks at. The test is `!= 'N'` rather than
        # `== 'C'` so a value nobody has seen surfaces instead of being dropped.
        if raw and raw != 'N':
            findings.append({
                'doc_no': doc, 'kind': 'source_status', 'fields': ['DOCSTAT'],
                'fingerprint': prints.get(doc) or document_fingerprint(
                    express[doc], sendy[doc]),
                'docstat': raw,
                'message': (f'เอกสาร {doc} ฝั่ง Express มี DOCSTAT = "{raw}" '
                            f'(ค่าดิบ ยังไม่มีใครสรุปว่าแปลว่าอะไร) '
                            f'แต่ Sendy ยังถือบรรทัดของเอกสารนี้อยู่'),
            })

    # ── gone at source ───────────────────────────────────────────────────────
    sendy_only = held - set(express)
    freshness = 'authoritative' if export_at is not None else 'indeterminate'
    only_newer, only_older = set(), set()
    if freshness == 'authoritative':
        # DATE, and STRICTLY older. An export taken at 08:32 does not prove that
        # a document dated the SAME DAY is missing at source — it may simply have
        # been keyed after the export ran. Comparing the raw datetime would call
        # every one of those "deleted at source", which is the false-alarm class
        # this whole freshness rule exists to prevent.
        cut = (export_at.date() if hasattr(export_at, 'date')
               else export_at).isoformat()
        for doc in sendy_only:
            (only_older if (sendy[doc]['hdr'][0] or '') < cut else only_newer).add(doc)
        for doc in sorted(only_older):
            findings.append({
                'doc_no': doc, 'kind': 'deleted_at_source', 'fields': ['DOCUMENT'],
                'fingerprint': None, 'docstat': '',
                'message': (f'เอกสาร {doc} ({sendy[doc]["hdr"][0]}) อยู่ใน Sendy '
                            f'แต่ไม่มีใน Express ที่ export เมื่อ {cut} — '
                            f'อาจถูกลบที่ต้นทาง'),
            })

    counters = {
        'freshness': freshness,
        'era_start': era_start,
        'export_at': (export_at.isoformat() if hasattr(export_at, 'isoformat')
                      else export_at),
        'sendy_eligible': len(held),
        'express_headers': len({r.get('DOCNUM') for r in artrn_rows
                                if r.get('RECTYP') in _SCOPE_RECTYP}
                               | {r.get('DOCNUM') for r in aptrn_rows
                                  if r.get('RECTYP') in _SCOPE_RECTYP}),
        'express_eligible': len(express),
        'express_dropped_all_lines_ignored': len(dropped_by_ignore),
        'compared': len(compared),
        'sendy_only': len(sendy_only),
        'sendy_only_newer_than_export': len(only_newer),
        'sendy_only_older_than_export': len(only_older),
        'baseline_entries': len(baseline),
        'findings': len(findings),
    }
    # ⚠ `compared + sendy_only == sendy_eligible` is set algebra and cannot
    # fail, so it is not written here — an assertion that cannot fail reads as a
    # check and is not one. The population is verified against an INDEPENDENT
    # signal instead: a second query that counts the documents the Sendy side
    # was supposed to produce. It fails when _drift_sendy_side silently drops
    # rows, and when one document number exists in BOTH ledgers — which would
    # merge two books' lines into one fingerprint and report drift for ever.
    # UNION ALL, not UNION, precisely so a collision shows up as a count.
    expected = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT doc_base FROM sales_transactions"
        "   WHERE date_iso >= ? AND doc_base IS NOT NULL"
        "  UNION ALL"
        "  SELECT DISTINCT doc_no FROM purchase_transactions"
        "   WHERE date_iso >= ? AND doc_no IS NOT NULL)",
        (era_start, era_start)).fetchone()[0]
    if expected != counters['sendy_eligible']:
        raise DriftInputError(
            f'the Sendy side holds {counters["sendy_eligible"]} documents but the '
            f'ledgers name {expected} — either rows were dropped while building it, '
            f'or one document number appears in both books. Either way a document '
            f'comparison built on it would be wrong, so there is no verdict.')
    counters['sendy_expected'] = expected

    return DriftResult(findings, set(compared), counters)


def _drift_docstat(artrn_rows, aptrn_rows):
    """doc_no -> raw DOCSTAT, both books. Built per call and small; the drift
    loop calls it a handful of times, not per line."""
    out = {}
    for rows in (artrn_rows, aptrn_rows):
        for r in rows:
            if r.get('DOCNUM') is not None:
                out[r['DOCNUM']] = r.get('DOCSTAT')
    return out


# The explained-disagreement baseline that ships with the app. Regenerated by
# projects/express-integration/build_drift_baseline.py, where every entry's
# reason is COMPUTED from the document's own numbers by a named predicate (or,
# for the one case no computation could reach, carries a dated human ruling).
# It lives in the repo rather than in the brain repo because prod has to read
# it: without it the very first upload would raise 110 alerts for disagreements
# that were all explained months ago, and a page that cries wolf on day one is
# a page nobody opens again.
DRIFT_BASELINE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data',
                                   'reference', 'express_drift_baseline.json')


def load_drift_baseline(path=None):
    """The shipped baseline, or None when it cannot be read.

    None means "no baseline", not "empty baseline": the caller reports that in
    its counters so a run with a missing file is never mistaken for a run where
    nothing was explained away.
    """
    import json
    try:
        with open(path or DRIFT_BASELINE_PATH, encoding='utf-8') as f:
            return json.load(f).get('baseline') or None
    except (OSError, ValueError):
        return None


def run_document_drift_scan(artrn_rows, aptrn_rows, stcrd_rows, armas_rows,
                            apmas_rows, db_path, *, export_at=None,
                            baseline_path=None):
    """Read-only observer over the ledgers the import has ALREADY committed.

    Shaped exactly like `models.scan_reconcile`'s use in import_router: it opens
    its own read-only connection, it never writes, and the caller wraps it so a
    bug here can never make a successful upload look failed. The alert writing is
    the CALLER's job and happens after every connection is closed, per the
    best-effort rule the other alert recorders follow.

    Returns a JSON-serialisable dict, never raises for data reasons.
    """
    import sqlite3 as _sq
    baseline = load_drift_baseline(baseline_path)
    conn = _sq.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = _sq.Row
    try:
        result = detect_document_drift(artrn_rows, aptrn_rows, stcrd_rows,
                                       armas_rows, apmas_rows, conn,
                                       baseline=baseline, export_at=export_at)
    finally:
        conn.close()
    counters = dict(result.counters)
    counters['baseline_loaded'] = baseline is not None
    return {'findings': result.findings, 'counters': counters,
            'scope': DRIFT_SCOPE_NOTE}


# Shown on the import results page EVERY time, including a clean run. A detector
# is only trustworthy if what it does not look at is as visible as what it does;
# printing the limits only when something is found teaches the reader that a
# quiet page means "nothing is wrong" instead of "nothing I look at is wrong".
DRIFT_SCOPE_NOTE = (
    'ตรวจเฉพาะเอกสารที่ Sendy ถืออยู่ ตั้งแต่ 2024-01-01 · เทียบหัวบิล '
    '(วันที่ · ชนิด VAT · รหัสคู่ค้า) และทุกบรรทัด (รหัสสินค้า · จำนวน · หน่วย · '
    'ราคา/หน่วย · ยอดรวม · ยอดสุทธิ · ส่วนลด) · '
    '⛔ ไม่เห็น: ชื่อสินค้า/ชื่อคู่ค้าที่เปลี่ยนที่ต้นทาง · ลำดับบรรทัด · '
    'ใบรับเงิน/จ่ายเงิน (RE/PS) และการฉายใบลดหนี้ · '
    'บั๊กในตัวแปลงข้อมูลเอง (สองฝั่งใช้ตัวเดียวกัน จึงผิดเหมือนกัน) · '
    'เอกสารที่ Express มีแต่ Sendy ไม่มี (เป็นงานของหน้า "ตรวจสอบบิลหายจาก Express")'
)
