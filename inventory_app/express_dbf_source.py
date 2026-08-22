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
    parse_express_ar_snapshot.AROutstanding (minus the snapshot date, which the
    importer stamps)."""
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
    parse_express_ap_snapshot's APOutstanding (minus the snapshot date)."""
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
            if key in by_doc:
                # ARTRN/APTRN carry more than one header for some DOCNUMs
                # (see _reject_duplicate). One document, not two.
                by_doc[key]['header_count'] += 1
                continue
            docdat = row.get('DOCDAT')
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
