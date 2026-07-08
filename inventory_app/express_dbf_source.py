"""Server-side Express (DBF) reader + record adapters — Phase 1 slice A
(sales + purchase only; payments/credit-notes are a separate slice, not
implemented here).

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
"""
import os

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
