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


def _num(row, field):
    v = row.get(field)
    return float(v) if v is not None else 0.0


def _int(row, field, default=0):
    v = row.get(field)
    return int(v) if v is not None else default


def _header_date_iso(hdr):
    d = hdr.get('DOCDAT')
    if d is None:
        # DOCDAT is a real datetime.date on every header row Phase 0 checked
        # (verified facts, MAPPING.md). A None here means LenientFieldParser
        # hit malformed bytes on a field that should never be malformed —
        # fail loud rather than silently emit a bad date_iso into the ledger.
        raise ValueError(f"DOCDAT missing/malformed for doc {hdr.get('DOCNUM')!r}")
    return d.isoformat()


def build_sales_entries(artrn_rows, stcrd_rows, armas_rows):
    """Build sales entries — the SAME shape parse_weekly.parse_sales emits —
    from already-read Express DBF rows. Pure: no file IO, so a caller can
    build entries with plain dict fixtures for tests."""
    headers = {r['DOCNUM']: r for r in artrn_rows if r.get('RECTYP') in _SCOPE_RECTYP}
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


def build_purchase_entries(aptrn_rows, stcrd_rows, apmas_rows):
    """Build purchase entries — the SAME shape parse_weekly.parse_purchases
    emits — from already-read Express DBF rows. Pure: no file IO."""
    headers = {r['DOCNUM']: r for r in aptrn_rows if r.get('RECTYP') in _SCOPE_RECTYP}
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


def build_invoice_refs(artrn_rows, artrnrm_rows):
    """Build express_invoice_refs rows: doc_base -> (youref, remark), scoped
    to the same sales doc set build_sales_entries uses (IV/HS/SR). Feeds the
    marketplace-IV matcher (buyer-name on YOUREF, #271/#272 — a separate
    project). Docs with neither field populated are skipped (nothing to
    store — most non-marketplace invoices have a blank YOUREF)."""
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


def build_payments_in_records(artrn_rows, arrcpit_rows, armas_rows):
    """Build payments_in records — the SAME shape models.parse_payment_csv
    emits (re_no, cancelled, date_iso, customer, salesperson, iv_list,
    total) — from Express DBF rows. Feeds models.import_payment_records()
    directly: the CANONICAL received_payments + paid_invoices path, NOT
    express_payments_in (which import_router.commit_file never wires up).

    RE header money fields are always 0 (MAPPING.md §3) — `total` is Σ
    ARRCPIT.RCVAMT for IV lines only, mirroring parse_payment_csv exactly.
    """
    headers = [r for r in artrn_rows if r.get('RECTYP') == '9']
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
                # Fail loud — mirrors parse_payment_csv's fail-loud guard on
                # an unknown doc prefix. The regex/filter above limits scope
                # to {3,5}; if that's ever widened, this catches a new kind
                # before it silently mis-signs into paid_invoices.
                raise ValueError(
                    f"build_payments_in_records: unexpected ARRCPIT.RECTYP "
                    f"{line.get('RECTYP')!r} on RE {re_no!r} "
                    f"(supported: 3=IV, 5=SR)"
                )
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

def build_payments_out_records(aptrn_rows, aprcpit_rows, apmas_rows):
    """Build payments_out records — the dataclasses.asdict() shape of
    parse_express_payments_out.APPayment — from Express DBF rows. Feeds
    import_express.run_import_records('payments_out', ...).

    TRAP (MAPPING.md §4): invoice_amount must be APTRN.RCVAMT, NOT PAYAMT
    (PAYAMT diverges arbitrarily on 6/24 matched docs, three of them
    exactly 2x the correct value).

    Money breakdown beyond invoice_amount (cash/cheque/deposit/interest/
    discount/vat) is left at the schema default (0.0) — MAPPING.md flags
    the APRCPCQ-based cash-vs-cheque split "best-effort... not exercised
    in this spike", i.e. unverified. invoice_amount is the only money field
    Phase 0's reconciliation actually ties, and it's what dedup depends on.
    receive_refs.receive_date_iso / invoice_ref are similarly not sourced
    here (not in MAPPING.md's confirmed field map) — left None rather than
    guessing a DBF field name that was never verified.
    """
    headers = [r for r in aptrn_rows if r.get('RECTYP') == '9']
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
                'amount': _num(line, 'PAYAMT'),
            }
            for line in lines_by_rcp.get(doc_no, [])
        ]
        records.append({
            'doc_no': doc_no,
            'date_iso': _header_date_iso(hdr),
            'supplier_name': names.get(hdr.get('SUPCOD')) or hdr.get('SUPCOD'),
            'is_void': False,   # no void/DOCSTAT mapping confirmed for PS docs
            'deposit_applied': 0.0,
            'invoice_amount': _num(hdr, 'RCVAMT'),   # RCVAMT, not PAYAMT — trap
            'cash_amount': 0.0,
            'cheque_amount': 0.0,
            'interest_amount': 0.0,
            'discount_amount': 0.0,
            'vat_amount': 0.0,
            'cheque_no': '',
            'cheque_date_iso': '',
            'bank': '',
            'cheque_status': '',
            'note': '',
            'receive_refs': receive_refs,
        })
    return records


# ── credit_notes_ar (SR header, no lines — credit_note_amounts) ────────────

def build_credit_notes_ar_records(artrn_rows, armas_rows):
    """Build credit_notes_ar records — feeds
    import_credit_notes.import_credit_note_amounts_records() directly: the
    HEADER-level credit_note_amounts table (authoritative per-SR credited
    amount, mig 062). Per MAPPING.md §5: ARTRN RECTYP='5' AND DOCNUM starts
    with 'SR'.

    This targets a DIFFERENT table than the SR LINE items
    build_sales_entries (slice A) already writes into sales_transactions —
    same source rows, deliberately different (both correct) numbers by
    design. Do not unify; see MAPPING.md's "SR/GR-in-ledger duality" note.
    """
    names = {r['CUSCOD']: r['CUSNAM'] for r in armas_rows}
    records = []
    for r in artrn_rows:
        if r.get('RECTYP') != _CREDIT_NOTE_RECTYP:
            continue
        doc = r.get('DOCNUM') or ''
        if not doc.startswith('SR'):
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


def build_credit_notes_ap_records(aptrn_rows, stcrd_rows, apmas_rows):
    """Build credit_notes_ap records — the dataclasses.asdict() shape of
    parse_express_credit_notes.CreditNote(+CreditNoteLine) — from Express
    DBF rows. Feeds import_express.run_import_records('credit_notes', ...).

    Per MAPPING.md §6: APTRN RECTYP='5' AND DOCNUM starts with 'GR' is the
    header (its own money fields are always 0 — the real total is the
    STCRD line sum). TRAP: total = Σ STCRD.TRNVAL, NOT NETVAL (NETVAL is
    VAT-stripped/post-discount; Sendy's express_credit_notes.total_amount
    stores the pre-VAT-strip TRNVAL — same trap as SR-in-sales/GR-in-purchase).
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
            'is_void': False,
            'type_code': None,
            'note': '',
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
