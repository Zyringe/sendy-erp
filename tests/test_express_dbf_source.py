"""express_dbf_source — Phase 1 slice A (sales + purchase DBF adapters).

Pure dict-fixture tests: no DBF files needed. Fixtures use the field names
and traps documented in projects/express-integration/MAPPING.md (Phase 0,
verified 2026-07-08) — do not rediscover them:
  - sales/purchase scope = RECTYP IN ('3','1','5') (IV/RR, HS/HP cash, SR/GR
    credit-note lines); '9' (RE/PS) and '7' (OE) are OUT of scope.
  - SR (sales) / GR (purchase) lines use STCRD.TRNVAL for net, NOT NETVAL.
  - vat_type is doc-level (ARTRN/APTRN.FLGVAT), broadcast to every line.
  - sales doc_no carries the line suffix ("IV6900929-1"); purchase doc_no
    does not (line identity is (doc_no, bsn_code, line_seq)).
"""
import datetime

import pytest

from express_dbf_source import build_invoice_refs, build_purchase_entries, build_sales_entries


def _artrn(docnum, rectyp, *, cuscod='C001', flgvat=0, docdat=None, youref=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'CUSCOD': cuscod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1), 'YOUREF': youref,
    }


def _aptrn(docnum, rectyp, *, supcod='S001', flgvat=0, docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'SUPCOD': supcod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1),
    }


def _stcrd(docnum, seqnum, *, stkcod='804', stkdes='name', qty=2.0, unit='ตัว',
           unitpr=45.0, disc='', trnval=90.0, netval=90.0):
    return {
        'DOCNUM': docnum, 'SEQNUM': seqnum, 'STKCOD': stkcod, 'STKDES': stkdes,
        'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': unitpr, 'DISC': disc,
        'TRNVAL': trnval, 'NETVAL': netval,
    }


# ── sales ─────────────────────────────────────────────────────────────────

def test_build_sales_entries_iv_line():
    """IV (RECTYP='3') line: net comes from NETVAL, doc_no carries the -N suffix."""
    artrn = [_artrn('IV6900929', '3', cuscod='C001')]
    stcrd = [_stcrd('IV6900929', 1, stkcod='804ข1108', qty=2.0, unitpr=45.0,
                     trnval=95.0, netval=62.0)]
    armas = [{'CUSCOD': 'C001', 'CUSNAM': 'ร้านทดสอบ'}]

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    e = entries[0]
    assert e['doc_no'] == 'IV6900929-1'
    assert e['product_code_raw'] == '804ข1108'
    assert e['qty'] == 2.0
    assert e['unit_price'] == 45.0
    assert e['total'] == 95.0
    assert e['net'] == 62.0, "IV net must be NETVAL, not TRNVAL"
    assert e['party'] == 'ร้านทดสอบ'
    assert e['party_code'] == 'C001'


def test_build_sales_entries_hs_cash_line_is_in_scope():
    """HS (RECTYP='1', cash sale) is IN scope, same net rule as IV."""
    artrn = [_artrn('HS6900001', '1')]
    stcrd = [_stcrd('HS6900001', 1, stkcod='045ล6155', qty=1.0, unit='ผง',
                     unitpr=300.0, trnval=300.0, netval=300.0)]
    armas = []

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    assert entries[0]['doc_no'] == 'HS6900001-1'
    assert entries[0]['net'] == 300.0


def test_build_sales_entries_sr_uses_trnval_not_netval():
    """TRAP: SR (RECTYP='5') net must be TRNVAL, not the VAT-stripped NETVAL."""
    artrn = [_artrn('SR6900009', '5')]
    stcrd = [_stcrd('SR6900009', 1, stkcod='935ก2000', qty=60.0, unit='อน',
                     unitpr=155.0, trnval=2340.00, netval=2293.20)]
    armas = []

    entries = build_sales_entries(artrn, stcrd, armas)

    assert len(entries) == 1
    e = entries[0]
    assert e['total'] == 2340.00
    assert e['net'] == 2340.00, "SR net must be TRNVAL (pre-discount), not NETVAL"
    assert e['net'] != 2293.20


def test_build_sales_entries_vat_type_broadcast_from_header():
    """vat_type is doc-level (FLGVAT) — every line of the doc gets the same value."""
    artrn = [_artrn('IV6900001', '3', flgvat=2)]
    stcrd = [_stcrd('IV6900001', 1), _stcrd('IV6900001', 2, stkcod='805')]

    entries = build_sales_entries(artrn, stcrd, [])

    assert len(entries) == 2
    assert all(e['vat_type'] == 2 for e in entries)


def test_build_sales_entries_excludes_out_of_scope_rectyp():
    """RECTYP='9' (RE, payment header) must not be pulled into sales entries."""
    artrn = [_artrn('RE6900001', '9')]
    stcrd = [_stcrd('RE6900001', 1)]

    entries = build_sales_entries(artrn, stcrd, [])

    assert entries == []


def test_build_sales_entries_falls_back_to_customer_code_when_name_missing():
    artrn = [_artrn('IV6900002', '3', cuscod='UNKNOWN')]
    stcrd = [_stcrd('IV6900002', 1)]

    entries = build_sales_entries(artrn, stcrd, [])

    assert entries[0]['party'] == 'UNKNOWN'


# ── purchase ─────────────────────────────────────────────────────────────

def test_build_purchase_entries_rr_line_doc_no_has_no_suffix():
    """RR (RECTYP='3') line: doc_no is the bare DOCNUM (no -N), net from NETVAL."""
    aptrn = [_aptrn('RR6900077', '3', supcod='S001')]
    stcrd = [_stcrd('RR6900077', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                     trnval=1920.00, netval=1920.00)]
    apmas = [{'SUPCOD': 'S001', 'SUPNAM': 'ซัพพลายเออร์ทดสอบ'}]

    entries = build_purchase_entries(aptrn, stcrd, apmas)

    assert len(entries) == 1
    e = entries[0]
    assert e['doc_no'] == 'RR6900077'
    assert e['line_seq'] == 1
    assert e['net'] == 1920.00
    assert e['party'] == 'ซัพพลายเออร์ทดสอบ'
    assert e['party_code'] == 'S001'


def test_build_purchase_entries_hp_cash_line_is_in_scope():
    aptrn = [_aptrn('HP6900001', '1')]
    stcrd = [_stcrd('HP6900001', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                     trnval=1920.00, netval=1920.00),
             _stcrd('HP6900001', 2, stkcod='035ป6108', qty=500.0, unitpr=5.80,
                     trnval=2900.00, netval=2900.00)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert len(entries) == 2
    assert {e['line_seq'] for e in entries} == {1, 2}
    assert {e['doc_no'] for e in entries} == {'HP6900001'}


def test_build_purchase_entries_gr_uses_trnval_not_netval():
    """TRAP: GR (RECTYP='5') net must be TRNVAL, not NETVAL (VAT-type-2 case)."""
    aptrn = [_aptrn('GR6700021', '5', flgvat=2)]
    stcrd = [_stcrd('GR6700021', 1, trnval=390.00, netval=364.49)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert len(entries) == 1
    assert entries[0]['net'] == 390.00, "GR net must be TRNVAL, not the VAT-stripped NETVAL"


def test_build_purchase_entries_excludes_oe_and_ps():
    """RECTYP='7' (OE, order) and RECTYP='9' (PS, payment) are out of scope."""
    aptrn = [_aptrn('OE6900001', '7'), _aptrn('PS6900001', '9')]
    stcrd = [_stcrd('OE6900001', 1), _stcrd('PS6900001', 1)]

    entries = build_purchase_entries(aptrn, stcrd, [])

    assert entries == []


# ── invoice refs (YOUREF / REMARK side table) ───────────────────────────

def test_build_invoice_refs_captures_youref_and_remark():
    artrn = [_artrn('IV6900929', '3', youref='วรันดา(L)')]
    artrnrm = [{'DOCNUM': 'IV6900929', 'REMARK': 'แถม'}]

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == [{'doc_base': 'IV6900929', 'youref': 'วรันดา(L)', 'remark': 'แถม'}]


def test_build_invoice_refs_skips_docs_with_neither_field():
    artrn = [_artrn('IV6900930', '3', youref=None)]
    artrnrm = []

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == []


def test_build_invoice_refs_excludes_out_of_scope_rectyp():
    artrn = [_artrn('RE6900001', '9', youref='someone')]
    artrnrm = []

    refs = build_invoice_refs(artrn, artrnrm)

    assert refs == []
