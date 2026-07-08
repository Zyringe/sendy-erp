"""express_dbf_source — Phase 1 slices A+B (sales/purchase + payments/
credit-notes DBF adapters).

Pure dict-fixture tests: no DBF files needed. Fixtures use the field names
and traps documented in projects/express-integration/MAPPING.md (Phase 0,
verified 2026-07-08) — do not rediscover them:
  - sales/purchase scope = RECTYP IN ('3','1','5') (IV/RR, HS/HP cash, SR/GR
    credit-note lines); '9' (RE/PS) and '7' (OE) are OUT of scope.
  - SR (sales) / GR (purchase) lines use STCRD.TRNVAL for net, NOT NETVAL.
  - vat_type is doc-level (ARTRN/APTRN.FLGVAT), broadcast to every line.
  - sales doc_no carries the line suffix ("IV6900929-1"); purchase doc_no
    does not (line identity is (doc_no, bsn_code, line_seq)).
  - payments_in: ARTRN RECTYP='9' (RE) header + ARRCPIT lines (RECTYP
    '3'=IV/'5'=SR, RCVAMT); SR lines sign-flip negative.
  - payments_out: APTRN RECTYP='9' (PS); invoice_amount = APTRN.RCVAMT, NOT
    PAYAMT (PAYAMT diverges arbitrarily, sometimes exactly 2x).
  - credit_notes_ar: ARTRN RECTYP='5' DOCNUM LIKE 'SR%' (header-only) →
    credit_note_amounts, using ARTRN.TOTAL — NOT the same target as the SR
    line items build_sales_entries already writes into sales_transactions.
  - credit_notes_ap: APTRN RECTYP='5' DOCNUM LIKE 'GR%' + STCRD lines;
    total = Σ STCRD.TRNVAL, NOT NETVAL (same VAT-strip trap as SR-in-sales).
"""
import datetime
import os
import sqlite3

from express_dbf_source import (
    build_credit_notes_ap_records,
    build_credit_notes_ar_records,
    build_invoice_refs,
    build_payments_in_records,
    build_payments_out_records,
    build_purchase_entries,
    build_sales_entries,
)

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MIG_124 = os.path.join(_REPO, "data", "migrations", "124_restore_mapping_bsn_unit.sql")
_MIG_132 = os.path.join(_REPO, "data", "migrations", "132_express_invoice_refs.sql")


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
           unitpr=45.0, disc='', trnval=90.0, netval=90.0, rdocnum=''):
    return {
        'DOCNUM': docnum, 'SEQNUM': seqnum, 'STKCOD': stkcod, 'STKDES': stkdes,
        'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': unitpr, 'DISC': disc,
        'TRNVAL': trnval, 'NETVAL': netval, 'RDOCNUM': rdocnum,
    }


# ── payments_in / payments_out / credit_notes headers (slice B) ────────────

def _artrn_re(docnum, *, cuscod='C001', slmcod='06', docdat=None):
    """ARTRN RE header row (RECTYP='9', payments_in)."""
    return {
        'DOCNUM': docnum, 'RECTYP': '9', 'CUSCOD': cuscod, 'SLMCOD': slmcod,
        'DOCDAT': docdat or datetime.date(2026, 5, 1),
    }


def _arrcpit(rcpnum, docnum, rectyp, rcvamt):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'RECTYP': rectyp, 'RCVAMT': rcvamt}


def _aptrn_ps(docnum, *, supcod='S001', rcvamt=0.0, docdat=None):
    """APTRN PS header row (RECTYP='9', payments_out). RCVAMT is the correct
    field per the trap — NOT PAYAMT."""
    return {
        'DOCNUM': docnum, 'RECTYP': '9', 'SUPCOD': supcod, 'RCVAMT': rcvamt,
        'DOCDAT': docdat or datetime.date(2026, 5, 1),
    }


def _aprcpit(rcpnum, docnum, payamt):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'PAYAMT': payamt}


def _artrn_sr(docnum, *, cuscod='C001', sonum=None, total=0.0, docdat=None):
    """ARTRN SR header row (RECTYP='5', DOCNUM LIKE 'SR%' — credit_notes_ar)."""
    return {
        'DOCNUM': docnum, 'RECTYP': '5', 'CUSCOD': cuscod, 'SONUM': sonum,
        'TOTAL': total, 'DOCDAT': docdat or datetime.date(2026, 2, 1),
    }


def _aptrn_gr(docnum, *, supcod='S001', docdat=None):
    """APTRN GR header row (RECTYP='5', DOCNUM LIKE 'GR%' — credit_notes_ap).
    Own money fields are always 0 on GR rows; the real total is the STCRD
    line sum."""
    return {
        'DOCNUM': docnum, 'RECTYP': '5', 'SUPCOD': supcod,
        'DOCDAT': docdat or datetime.date(2024, 6, 1),
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


# ── recency window (cutoff) — Phase 2 follow-up, Put chose since_days=60 ───

def test_build_sales_entries_cutoff_excludes_older_doc():
    """A doc dated before cutoff is dropped entirely — header AND its STCRD
    lines (the header-set filter is what keeps the two consistent)."""
    cutoff = datetime.date(2026, 6, 1)
    artrn = [
        _artrn('IV_OLD', '3', docdat=datetime.date(2026, 5, 1)),   # before cutoff
        _artrn('IV_NEW', '3', docdat=datetime.date(2026, 6, 15)),  # on/after cutoff
    ]
    stcrd = [_stcrd('IV_OLD', 1, stkcod='old'), _stcrd('IV_NEW', 1, stkcod='new')]

    entries = build_sales_entries(artrn, stcrd, [], cutoff=cutoff)

    assert [e['product_code_raw'] for e in entries] == ['new']


def test_build_sales_entries_cutoff_boundary_is_inclusive():
    """A doc dated EXACTLY on cutoff is kept (>=, not >)."""
    cutoff = datetime.date(2026, 6, 1)
    artrn = [_artrn('IV_EDGE', '3', docdat=cutoff)]
    stcrd = [_stcrd('IV_EDGE', 1)]

    entries = build_sales_entries(artrn, stcrd, [], cutoff=cutoff)

    assert len(entries) == 1


def test_build_sales_entries_cutoff_none_keeps_full_history():
    """cutoff=None (the explicit override, e.g. a manual backfill) must
    behave exactly like the pre-Phase-2 unfiltered call."""
    artrn = [_artrn('IV_ANCIENT', '3', docdat=datetime.date(2003, 1, 1))]
    stcrd = [_stcrd('IV_ANCIENT', 1)]

    entries = build_sales_entries(artrn, stcrd, [], cutoff=None)

    assert len(entries) == 1


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


def test_build_purchase_entries_cutoff_excludes_older_doc():
    """Same recency-window treatment on the APTRN header set."""
    cutoff = datetime.date(2026, 6, 1)
    aptrn = [
        _aptrn('RR_OLD', '3', docdat=datetime.date(2026, 5, 1)),
        _aptrn('RR_NEW', '3', docdat=datetime.date(2026, 6, 15)),
    ]
    stcrd = [_stcrd('RR_OLD', 1, stkcod='old'), _stcrd('RR_NEW', 1, stkcod='new')]

    entries = build_purchase_entries(aptrn, stcrd, [], cutoff=cutoff)

    assert [e['product_code_raw'] for e in entries] == ['new']


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


# ── idempotency + wiring (through models.import_weekly / import_router) ──
#
# empty_db clones the LIVE schema (see conftest.py) — a dev machine whose
# live DB hasn't picked up mig 124 (bsn_unit) or 132 (express_invoice_refs,
# this PR's own migration) yet would otherwise fail these tests with "no
# such column/table". Apply them defensively, mirroring
# tests/test_import_weekly_idempotent.py's _ensure_bsn_unit pattern.

def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _ensure_migrations(c):
    cols = {r[1] for r in c.execute("PRAGMA table_info(product_code_mapping)")}
    if "bsn_unit" not in cols:
        with open(_MIG_124, encoding="utf-8") as f:
            c.executescript(f.read())
    tbl = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='express_invoice_refs'"
    ).fetchone()
    if not tbl:
        with open(_MIG_132, encoding="utf-8") as f:
            c.executescript(f.read())


def _seed_product(path, name, code, unit_type='ตัว'):
    c = _conn(path)
    _ensure_migrations(c)
    cur = c.execute(
        "INSERT INTO products (product_name, unit_type, cost_price) VALUES (?, ?, 0)",
        (name, unit_type))
    pid = cur.lastrowid
    c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES (?, ?, ?)", (code, name, pid))
    c.commit()
    c.close()
    return pid


def _stock(path, pid):
    c = _conn(path)
    row = c.execute("SELECT quantity FROM stock_levels WHERE product_id=?", (pid,)).fetchone()
    c.close()
    return row[0] if row else None


def test_dbf_entries_import_twice_is_idempotent(empty_db):
    """Building entries from DBF fixtures and importing twice must converge
    to the same stock (no double-count) — mirrors
    test_import_weekly_idempotent.py's re-import-is-noop contract."""
    import models

    pid_sale = _seed_product(empty_db, 'Psale', '804ข1108')
    pid_purch = _seed_product(empty_db, 'Ppurch', '035ป6107')

    artrn = [_artrn('IV6900929', '3', cuscod='C001')]
    stcrd_sale = [_stcrd('IV6900929', 1, stkcod='804ข1108', qty=2.0, unitpr=45.0,
                          trnval=90.0, netval=90.0)]
    aptrn = [_aptrn('RR6900077', '3', supcod='S001')]
    stcrd_purch = [_stcrd('RR6900077', 1, stkcod='035ป6107', qty=400.0, unitpr=4.80,
                           trnval=1920.0, netval=1920.0)]

    s1 = models.import_weekly(build_sales_entries(artrn, stcrd_sale, []), 'sales', 'f1')
    p1 = models.import_weekly(build_purchase_entries(aptrn, stcrd_purch, []), 'purchase', 'f1')
    assert s1['imported'] == 1
    assert p1['imported'] == 1
    assert _stock(empty_db, pid_sale) == -2.0
    assert _stock(empty_db, pid_purch) == 400.0

    # Re-build + re-import the SAME DBF fixtures — must be a pure no-op.
    s2 = models.import_weekly(build_sales_entries(artrn, stcrd_sale, []), 'sales', 'f2')
    p2 = models.import_weekly(build_purchase_entries(aptrn, stcrd_purch, []), 'purchase', 'f2')
    assert s2['unchanged'] == 1 and s2['imported'] == 0
    assert p2['unchanged'] == 1 and p2['imported'] == 0
    assert _stock(empty_db, pid_sale) == -2.0, "re-import must not double-count stock"
    assert _stock(empty_db, pid_purch) == 400.0, "re-import must not double-count stock"


def test_commit_express_dbf_wires_sales_purchase_and_refs(empty_db, monkeypatch):
    """import_router.commit_express_dbf(): open_table → build_* → import_weekly
    → express_invoice_refs upsert, end to end. open_table is monkeypatched
    (no real DBF files needed) so this exercises the wiring, not dbfread.

    No RE/PS/SR/GR docs in this fixture, so the slice-B builders all return
    empty record lists — but run_import_records still logs an
    express_import_log row per call regardless of count, so a companies row
    is required (slice A's sales/purchase path never needed one)."""
    import sys
    _scripts = os.path.join(_REPO, "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)   # import_express lives in scripts/

    import express_dbf_source as eds
    import import_router

    _seed_product(empty_db, 'Psale2', '804ข1108')
    _seed_product(empty_db, 'Ppurch2', '035ป6107')
    _seed_company(empty_db, 'BSN')

    fake_tables = {
        'ARTRN': [_artrn('IV6900930', '3', cuscod='C001', youref='วรันดา(L)')],
        'APTRN': [_aptrn('RR6900078', '3', supcod='S001')],
        'STCRD': [
            _stcrd('IV6900930', 1, stkcod='804ข1108', qty=1.0, unitpr=45.0, trnval=45.0, netval=45.0),
            _stcrd('RR6900078', 1, stkcod='035ป6107', qty=10.0, unitpr=4.80, trnval=48.0, netval=48.0),
        ],
        'ARMAS': [],
        'APMAS': [],
        'ARTRNRM': [{'DOCNUM': 'IV6900930', 'REMARK': 'แถม'}],
        'ARRCPIT': [],
        'APRCPIT': [],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])

    # since_days=None: this test's fixture dates are fixed (not relative to
    # "today") — it's pinning the WIRING, not the recency filter (that's
    # tested separately, see test_commit_express_dbf_since_days_window_*).
    result = import_router.commit_express_dbf('/fake/dataset', since_days=None)

    assert result['sales']['imported'] == 1
    assert result['purchase']['imported'] == 1
    assert result['invoice_refs_upserted'] == 1
    assert result['payments_in']['total'] == 0
    assert result['payments_out']['total'] == 0
    assert result['credit_notes_ar']['upserted'] == 0
    assert result['credit_notes_ap']['total'] == 0

    c = _conn(empty_db)
    row = c.execute(
        "SELECT youref, remark FROM express_invoice_refs WHERE doc_base='IV6900930'"
    ).fetchone()
    c.close()
    assert row['youref'] == 'วรันดา(L)'
    assert row['remark'] == 'แถม'


# ── payments_in (→ models.import_payment_records) ───────────────────────────

def test_build_payments_in_records_line_sum_not_header():
    """RE header money fields are 0 — total must come from the ARRCPIT line
    sum (IV lines only), never a header field."""
    artrn = [_artrn_re('RE6900300', cuscod='C001', slmcod='06')]
    arrcpit = [_arrcpit('RE6900300', 'IV6802996', '3', 5242.02)]
    armas = [{'CUSCOD': 'C001', 'CUSNAM': 'เจริญทรัพย์การค้า'}]

    records = build_payments_in_records(artrn, arrcpit, armas)

    assert len(records) == 1
    r = records[0]
    assert r['re_no'] == 'RE6900300'
    assert r['customer'] == 'เจริญทรัพย์การค้า'
    assert r['salesperson'] == '06'
    assert r['total'] == 5242.02
    assert r['iv_list'] == [{'iv_no': 'IV6802996', 'amount': 5242.02, 'kind': 'IV'}]


def test_build_payments_in_records_sr_line_sign_flipped_negative():
    """SR netting line (RECTYP='5') is unsigned magnitude in DBF — must be
    sign-flipped negative to match Sendy's convention, and excluded from
    `total` (which is Σ IV(+) only)."""
    artrn = [_artrn_re('RE6900208', cuscod='C002')]
    arrcpit = [
        _arrcpit('RE6900208', 'IV6802996', '3', 5242.02),
        _arrcpit('RE6900208', 'SR6900009', '5', 2293.20),   # unsigned in DBF
    ]

    records = build_payments_in_records(artrn, arrcpit, [])

    r = records[0]
    by_no = {iv['iv_no']: iv for iv in r['iv_list']}
    assert by_no['SR6900009']['amount'] == -2293.20, "SR must be sign-flipped negative"
    assert by_no['SR6900009']['kind'] == 'SR'
    assert r['total'] == 5242.02, "total = Σ IV(+) only, SR must not be netted here"


def test_build_payments_in_records_re_with_zero_iv_lines_still_emitted():
    """A pure SR-netting receipt (no IV lines) must still produce a record
    (total=0.0), not be silently dropped — mirrors the Phase 0 spike's own
    'RE docs with zero IV lines still need a dict entry' guard."""
    artrn = [_artrn_re('RE6900999', cuscod='C003')]

    records = build_payments_in_records(artrn, [], [])

    assert len(records) == 1
    assert records[0]['iv_list'] == []
    assert records[0]['total'] == 0.0


def test_build_payments_in_records_cancelled_defaults_false():
    """DOCSTAT='C' semantics are an open, non-blocking question (MAPPING.md
    §3) — cancelled must default False, never be inferred from DOCSTAT."""
    artrn = [_artrn_re('RE6900301', cuscod='C001')]

    records = build_payments_in_records(artrn, [], [])

    assert records[0]['cancelled'] is False


def test_build_payments_in_records_unknown_rectyp_skips_not_raises():
    """ARRCPIT.RECTYP outside {3,5} (real data: RECTYP='4', a 'DR' doc, 1
    row out of 57,024) must NOT crash the whole import — Put's call
    (2026-07-08, Phase 2 follow-up): skip it, don't guess the money, and
    surface it via the `skipped` list so it's never silent."""
    artrn = [_artrn_re('RE6900302', cuscod='C001')]
    arrcpit = [
        _arrcpit('RE6900302', 'IV6900001', '3', 500.0),
        _arrcpit('RE6900302', 'DR0000003', '4', 600.0),   # unsupported kind
    ]

    records = build_payments_in_records(artrn, arrcpit, [])   # skipped=None: silent-skip path

    assert len(records) == 1
    assert records[0]['iv_list'] == [{'iv_no': 'IV6900001', 'amount': 500.0, 'kind': 'IV'}]
    assert records[0]['total'] == 500.0, "the unsupported line must not corrupt the IV total"


def test_build_payments_in_records_unknown_rectyp_surfaces_in_skipped_list():
    """Same fixture, but with the `skipped` collector passed — the DR line
    must show up there with re_no/doc/rectyp/amount, never silently vanish."""
    artrn = [_artrn_re('RE6900302', cuscod='C001')]
    arrcpit = [_arrcpit('RE6900302', 'DR0000003', '4', 600.0)]
    skipped = []

    build_payments_in_records(artrn, arrcpit, [], skipped=skipped)

    assert skipped == [{'re_no': 'RE6900302', 'doc': 'DR0000003', 'rectyp': '4', 'amount': 600.0}]


def test_build_payments_in_records_excludes_out_of_scope_rectyp():
    """Only RECTYP='9' (RE) headers are in scope — an IV header must not
    be picked up as a payments_in record."""
    artrn = [_artrn('IV6900001', '3')]

    records = build_payments_in_records(artrn, [], [])

    assert records == []


# ── payments_out (→ import_express.run_import_records('payments_out', …)) ──

def test_build_payments_out_records_uses_rcvamt_not_payamt():
    """TRAP: invoice_amount must be APTRN.RCVAMT, NOT PAYAMT (PAYAMT
    diverges arbitrarily, sometimes exactly 2x the correct value)."""
    aptrn = [_aptrn_ps('PS0002097', supcod='S001', rcvamt=8540.00)]

    records = build_payments_out_records(aptrn, [], [])

    assert len(records) == 1
    assert records[0]['invoice_amount'] == 8540.00


def test_build_payments_out_records_receive_refs_from_aprcpit():
    aptrn = [_aptrn_ps('PS0001815', supcod='S002', rcvamt=1920.00)]
    aprcpit = [_aprcpit('PS0001815', 'RR6600291', 1920.00)]
    apmas = [{'SUPCOD': 'S002', 'SUPNAM': 'ซัพพลายเออร์ B'}]

    records = build_payments_out_records(aptrn, aprcpit, apmas)

    r = records[0]
    assert r['supplier_name'] == 'ซัพพลายเออร์ B'
    assert r['receive_refs'] == [
        {'receive_doc': 'RR6600291', 'receive_date_iso': None,
         'invoice_ref': None, 'amount': 1920.00}
    ]


def test_build_payments_out_records_excludes_out_of_scope_rectyp():
    aptrn = [_aptrn('RR6900077', '3')]

    records = build_payments_out_records(aptrn, [], [])

    assert records == []


# ── credit_notes_ar (→ import_credit_notes.import_credit_note_amounts_records) ──

def test_build_credit_notes_ar_records_basic_mapping():
    """Header-level mapping to credit_note_amounts — uses ARTRN.TOTAL
    (post-discount), NOT the STCRD.TRNVAL sales_transactions uses (§1's
    SR-in-sales value is intentionally different, same doc)."""
    artrn = [_artrn_sr('SR6900009', cuscod='C001', sonum='IV6802996', total=2293.20)]
    armas = [{'CUSCOD': 'C001', 'CUSNAM': 'เจริญทรัพย์การค้า'}]

    records = build_credit_notes_ar_records(artrn, armas)

    assert len(records) == 1
    r = records[0]
    assert r['sr_doc_base'] == 'SR6900009'
    assert r['ref_invoice'] == 'IV6802996'
    assert r['credited_amount'] == 2293.20
    assert r['customer'] == 'เจริญทรัพย์การค้า'


def test_build_credit_notes_ar_records_excludes_non_sr_docnum():
    """RECTYP='5' but DOCNUM doesn't start with 'SR' (i.e. it's a GR on the
    AR side, which shouldn't happen, but be defensive) — excluded."""
    artrn = [{'DOCNUM': 'GR6900001', 'RECTYP': '5', 'CUSCOD': 'C001',
              'SONUM': None, 'TOTAL': 100.0, 'DOCDAT': datetime.date(2026, 2, 1)}]

    records = build_credit_notes_ar_records(artrn, [])

    assert records == []


def test_build_credit_notes_ar_records_excludes_out_of_scope_rectyp():
    artrn = [_artrn('IV6900001', '3')]

    records = build_credit_notes_ar_records(artrn, [])

    assert records == []


# ── credit_notes_ap (→ import_express.run_import_records('credit_notes', …)) ──

def test_build_credit_notes_ap_records_uses_trnval_not_netval():
    """TRAP: total must be Σ STCRD.TRNVAL, NOT NETVAL (VAT-type-2 case,
    GR6700021: TRNVAL=390.00 vs NETVAL=364.49 — the VAT-stripped figure)."""
    aptrn = [_aptrn_gr('GR6700021', supcod='S001')]
    stcrd = [_stcrd('GR6700021', 1, trnval=390.00, netval=364.49)]

    records = build_credit_notes_ap_records(aptrn, stcrd, [])

    assert len(records) == 1
    assert records[0]['total'] == 390.00, "must be TRNVAL, not the VAT-stripped NETVAL"


def test_build_credit_notes_ap_records_ref_doc_base_doc_only():
    """ref_doc is the first STCRD line's RDOCNUM, base doc only — the raw
    field carries a trailing line-sequence suffix."""
    aptrn = [_aptrn_gr('GR6700025', supcod='S001')]
    stcrd = [_stcrd('GR6700025', 1, rdocnum='RR6700025     6')]

    records = build_credit_notes_ap_records(aptrn, stcrd, [])

    assert records[0]['ref_doc'] == 'RR6700025'


def test_build_credit_notes_ap_records_ref_doc_null_when_rdocnum_blank():
    """Open edge case (MAPPING.md §6): blank RDOCNUM → ref_doc=NULL, more
    honest than a placeholder."""
    aptrn = [_aptrn_gr('GR6700007', supcod='S001')]
    stcrd = [_stcrd('GR6700007', 1, rdocnum='')]

    records = build_credit_notes_ap_records(aptrn, stcrd, [])

    assert records[0]['ref_doc'] is None


def test_build_credit_notes_ap_records_zero_line_gr_still_emitted():
    """GR6700007-style master with ZERO STCRD lines: still emitted (total=0,
    ref_doc=None, lines=[]) — not silently dropped."""
    aptrn = [_aptrn_gr('GR6700007', supcod='S001')]

    records = build_credit_notes_ap_records(aptrn, [], [])

    assert len(records) == 1
    assert records[0]['total'] == 0.0
    assert records[0]['ref_doc'] is None
    assert records[0]['lines'] == []


def test_build_credit_notes_ap_records_line_fields():
    aptrn = [_aptrn_gr('GR69000001', supcod='S001')]
    apmas = [{'SUPCOD': 'S001', 'SUPNAM': 'ซัพพลายเออร์ทดสอบ'}]
    stcrd = [_stcrd('GR69000001', 1, stkcod='532ด6515', stkdes='ดอกสว่าน',
                     qty=3.0, unit='ดก', unitpr=235.0, trnval=389.51, netval=389.51)]

    records = build_credit_notes_ap_records(aptrn, stcrd, apmas)

    r = records[0]
    assert r['supplier_name'] == 'ซัพพลายเออร์ทดสอบ'
    assert len(r['lines']) == 1
    ln = r['lines'][0]
    assert ln['product_code'] == '532ด6515'
    assert ln['qty'] == 3.0
    assert ln['unit'] == 'ดก'
    assert ln['unit_price'] == 235.0
    assert ln['line_total'] == 389.51


def test_build_credit_notes_ap_records_excludes_non_gr_docnum():
    """RECTYP='5' but DOCNUM doesn't start with 'GR' — excluded (defensive,
    mirrors credit_notes_ar's SR-prefix guard)."""
    aptrn = [{'DOCNUM': 'SR6900001', 'RECTYP': '5', 'SUPCOD': 'S001',
              'DOCDAT': datetime.date(2024, 6, 1)}]

    records = build_credit_notes_ap_records(aptrn, [], [])

    assert records == []


def test_build_credit_notes_ap_records_excludes_out_of_scope_rectyp():
    aptrn = [_aptrn('RR6900077', '3')]

    records = build_credit_notes_ap_records(aptrn, [], [])

    assert records == []


# ── wiring: commit_express_dbf covers all 6 types (slice A + B together) ───

def _seed_company(path, code='BSN', name_th='บริษัท ทดสอบ จำกัด'):
    c = _conn(path)
    c.execute("INSERT INTO companies (code, name_th) VALUES (?, ?)", (code, name_th))
    c.commit()
    c.close()


def test_commit_express_dbf_wires_all_six_types(empty_db, monkeypatch):
    """import_router.commit_express_dbf() covers sales/purchase (slice A,
    unchanged) PLUS payments_in/out + credit_notes_ar/ap (slice B) in one
    dataset-dir pass. open_table is monkeypatched — no real DBF needed."""
    import sys
    _scripts = os.path.join(_REPO, "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)   # import_express lives in scripts/

    import express_dbf_source as eds
    import import_router

    _seed_product(empty_db, 'Psale3', '804ข1108')
    _seed_product(empty_db, 'Ppurch3', '035ป6107')
    _seed_company(empty_db, 'BSN')

    fake_tables = {
        'ARTRN': [
            _artrn('IV6900931', '3', cuscod='C001'),
            _artrn_re('RE6900400', cuscod='C001'),
            _artrn_sr('SR6900010', cuscod='C001', sonum='IV6900931', total=100.0),
        ],
        'APTRN': [
            _aptrn('RR6900079', '3', supcod='S001'),
            _aptrn_ps('PS0009000', supcod='S001', rcvamt=50.0),
            _aptrn_gr('GR6900002', supcod='S001'),
        ],
        'STCRD': [
            _stcrd('IV6900931', 1, stkcod='804ข1108', qty=1.0, unitpr=45.0, trnval=45.0, netval=45.0),
            _stcrd('RR6900079', 1, stkcod='035ป6107', qty=10.0, unitpr=4.80, trnval=48.0, netval=48.0),
            _stcrd('GR6900002', 1, stkcod='035ป6107', qty=1.0, unitpr=48.0, trnval=48.0, netval=48.0),
        ],
        'ARMAS': [{'CUSCOD': 'C001', 'CUSNAM': 'ลูกค้าทดสอบ'}],
        'APMAS': [{'SUPCOD': 'S001', 'SUPNAM': 'ซัพพลายเออร์ทดสอบ'}],
        'ARTRNRM': [],
        'ARRCPIT': [_arrcpit('RE6900400', 'IV6900931', '3', 45.0)],
        'APRCPIT': [_aprcpit('PS0009000', 'RR6900079', 48.0)],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])

    # since_days=None: fixed fixture dates, not relative to "today" — this
    # test pins the 6-type WIRING, not the recency filter.
    result = import_router.commit_express_dbf('/fake/dataset', since_days=None)

    assert result['sales']['imported'] >= 1
    assert result['purchase']['imported'] >= 1
    assert result['payments_in']['imported'] == 1
    assert result['payments_out']['imported'] == 1
    assert result['credit_notes_ar']['upserted'] == 1
    assert result['credit_notes_ap']['imported'] == 1

    c = _conn(empty_db)
    rp = c.execute("SELECT total FROM received_payments WHERE re_no='RE6900400'").fetchone()
    assert rp['total'] == 45.0
    po = c.execute("SELECT invoice_amount FROM express_payments_out WHERE doc_no='PS0009000'").fetchone()
    assert po['invoice_amount'] == 50.0
    cna = c.execute("SELECT credited_amount FROM credit_note_amounts WHERE sr_doc_base='SR6900010'").fetchone()
    assert cna['credited_amount'] == 100.0
    cn = c.execute("SELECT total_amount FROM express_credit_notes WHERE doc_no='GR6900002'").fetchone()
    assert cn['total_amount'] == 48.0
    c.close()


# ── commit_express_dbf: since_days recency window + DR-skip surfacing ──────
# Both Put's calls (2026-07-08, Phase 2 follow-up): a real full-history
# upload took 12+ minutes before the window existed (would blow Railway's
# gunicorn --timeout 60); a real ARRCPIT RECTYP='4' ("DR") row must not
# crash the whole batch.

def test_commit_express_dbf_since_days_window_excludes_old_includes_recent(empty_db, monkeypatch):
    """An old doc is excluded from the import when since_days is set (the
    default path); a recent doc within the window still imports. Dates are
    relative to today so this test doesn't rot."""
    import sys
    _scripts = os.path.join(_REPO, "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)

    import express_dbf_source as eds
    import import_router

    today = datetime.date.today()
    old_date = today - datetime.timedelta(days=100)   # outside a 60-day window
    recent_date = today - datetime.timedelta(days=5)   # inside it

    _seed_product(empty_db, 'PsaleOld', 'old-code')
    _seed_product(empty_db, 'PsaleNew', 'new-code')
    _seed_company(empty_db, 'BSN')

    fake_tables = {
        'ARTRN': [
            _artrn('IV_OLD', '3', cuscod='C001', docdat=old_date),
            _artrn('IV_NEW', '3', cuscod='C001', docdat=recent_date),
        ],
        'APTRN': [],
        'STCRD': [
            _stcrd('IV_OLD', 1, stkcod='old-code', trnval=10.0, netval=10.0),
            _stcrd('IV_NEW', 1, stkcod='new-code', trnval=20.0, netval=20.0),
        ],
        'ARMAS': [{'CUSCOD': 'C001', 'CUSNAM': 'ลูกค้าทดสอบ'}],
        'APMAS': [],
        'ARTRNRM': [],
        'ARRCPIT': [],
        'APRCPIT': [],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])

    result = import_router.commit_express_dbf('/fake/dataset', since_days=60)

    assert result['sales']['imported'] == 1, "only the in-window doc should import"
    c = _conn(empty_db)
    rows = c.execute("SELECT bsn_code FROM sales_transactions").fetchall()
    c.close()
    assert [r['bsn_code'] for r in rows] == ['new-code']


def test_commit_express_dbf_surfaces_skipped_dr_line_in_payments_in(empty_db, monkeypatch):
    """A real-shaped ARRCPIT RECTYP='4' line must not crash
    commit_express_dbf — it shows up in payments_in.skipped_rectyp instead."""
    import sys
    _scripts = os.path.join(_REPO, "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)

    import express_dbf_source as eds
    import import_router

    recent = datetime.date.today() - datetime.timedelta(days=2)
    _seed_company(empty_db, 'BSN')

    fake_tables = {
        'ARTRN': [_artrn_re('RE0041138', cuscod='C900', docdat=recent)],
        'APTRN': [],
        'STCRD': [],
        'ARMAS': [],
        'APMAS': [],
        'ARTRNRM': [],
        'ARRCPIT': [_arrcpit('RE0041138', 'DR0000003', '4', 600.0)],
        'APRCPIT': [],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])

    result = import_router.commit_express_dbf('/fake/dataset', since_days=60)

    assert result['payments_in']['skipped_rectyp'] == [
        {'re_no': 'RE0041138', 'doc': 'DR0000003', 'rectyp': '4', 'amount': 600.0}
    ]
