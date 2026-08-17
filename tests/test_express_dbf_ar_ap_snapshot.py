"""express_dbf_source — AR/AP outstanding snapshot adapters (daily DBF zip).

Pure dict-fixture tests: no DBF files needed. The field mapping is pinned in
docs/plans/2026-08-17-daily-ar-ap-snapshot-from-dbf.md, verified against the
2026-06-05 prod snapshot (95 rows tied field-for-field) — do not rediscover it:

  - row set is `round(REMAMT, 2) != 0`. Rounding is LOAD-BEARING: raw REMAMT is
    a double carrying IEEE-754 noise, and an unrounded test selects 1,330 rows
    on BSN5657 where the real answer is 232.
  - NO date window. commit_express_dbf windows sales/purchase to 60 days;
    outstanding debt must ignore that entirely (the real snapshot holds docs
    dated 2009).
  - outstanding = -REMAMT for RECTYP '5' (SR credit notes), +REMAMT otherwise.
  - bill = RCVAMT + REMAMT for RECTYP '9' (RE rows, whose header money fields
    are 0 — MAPPING trap #4); NETAMT for everything else, where
    NETAMT == RCVAMT + REMAMT is an invariant that held 189/189.
  - is_anomalous AND has_warning are both RECTYP == '9' (43/43 in the snapshot).
  - AP's supplier invoice number is APTRN.REFNUM, NOT YOUREF (which is blank).
"""
import datetime
import sqlite3

import pytest

from express_dbf_source import (
    build_ap_snapshot_records,
    build_ar_snapshot_records,
)


# ── fixtures ──────────────────────────────────────────────────────────────

def _ar(docnum, rectyp, *, cuscod='C001', slmcod='06', docdat=None,
        netamt=0.0, rcvamt=0.0, remamt=0.0):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'CUSCOD': cuscod, 'SLMCOD': slmcod,
        'DOCDAT': docdat or datetime.date(2026, 4, 1),
        'NETAMT': netamt, 'RCVAMT': rcvamt, 'REMAMT': remamt,
    }


def _armas(cuscod, cusnam, custyp='00'):
    return {'CUSCOD': cuscod, 'CUSNAM': cusnam, 'CUSTYP': custyp}


def _ap(docnum, *, supcod='S001', refnum='', youref='', docdat=None,
        netamt=0.0, rcvamt=0.0, remamt=0.0, rectyp='3'):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'SUPCOD': supcod,
        'REFNUM': refnum, 'YOUREF': youref,
        'DOCDAT': docdat or datetime.date(2026, 4, 1),
        'NETAMT': netamt, 'RCVAMT': rcvamt, 'REMAMT': remamt,
    }


def _apmas(supcod, supnam, suptyp='00'):
    return {'SUPCOD': supcod, 'SUPNAM': supnam, 'SUPTYP': suptyp}


def _one(records, doc_no):
    hit = [r for r in records if r['doc_no'] == doc_no]
    assert len(hit) == 1, f'{doc_no} appears {len(hit)}x in {[r["doc_no"] for r in records]}'
    return hit[0]


# ── AR: row selection ─────────────────────────────────────────────────────

def test_ar_snapshot_excludes_settled_rows():
    """A fully-paid doc (REMAMT 0) is not outstanding. The unpaid CONTROL in
    the same call proves the builder ran and can emit."""
    artrn = [
        _ar('IV6900001', '3', netamt=100.0, rcvamt=100.0, remamt=0.0),
        _ar('IV6900002', '3', netamt=100.0, rcvamt=0.0, remamt=100.0),
    ]
    out = build_ar_snapshot_records(artrn, [_armas('C001', 'ลูกค้า ก')])

    assert [r['doc_no'] for r in out] == ['IV6900002']


def test_ar_snapshot_rounds_remamt_before_testing_zero():
    """IEEE-754 noise in REMAMT must NOT count as outstanding — this is the
    single trap that inflated a 232-row snapshot to 1,330."""
    artrn = [
        _ar('IV6900003', '3', netamt=100.0, rcvamt=100.0, remamt=1e-13),
        _ar('IV6900004', '3', netamt=100.0, rcvamt=99.99, remamt=0.01),  # CONTROL
    ]
    out = build_ar_snapshot_records(artrn, [_armas('C001', 'ลูกค้า ก')])

    assert [r['doc_no'] for r in out] == ['IV6900004'], (
        'float noise must be rounded away, but a real one-satang balance must survive')


def test_ar_snapshot_has_no_date_window():
    """Outstanding debt is never windowed — the real snapshot carries docs from
    2009. The builder must not even accept a cutoff."""
    artrn = [_ar('IV0900001', '3', docdat=datetime.date(2009, 1, 24),
                 netamt=500.0, rcvamt=0.0, remamt=500.0)]

    out = build_ar_snapshot_records(artrn, [_armas('C001', 'ลูกค้า ก')])

    assert _one(out, 'IV0900001')['doc_date_iso'] == '2009-01-24'
    with pytest.raises(TypeError):
        build_ar_snapshot_records(artrn, [], cutoff=datetime.date(2026, 1, 1))


# ── AR: per-RECTYP money semantics ────────────────────────────────────────

def test_ar_snapshot_iv_row_maps_every_field():
    artrn = [_ar('IV6901234', '3', cuscod='01ว18', slmcod='02',
                 docdat=datetime.date(2026, 5, 18),
                 netamt=14544.38, rcvamt=4000.0, remamt=10544.38)]
    armas = [_armas('01ว18', 'วีรวุฒิฮาร์ดแวร์', '02')]

    r = _one(build_ar_snapshot_records(artrn, armas), 'IV6901234')

    assert r == {
        'customer_code': '01ว18',
        'customer_name': 'วีรวุฒิฮาร์ดแวร์',
        'customer_type': 'ตัวแทนจำหน่าย(ยี่ปั้ว)',
        'doc_date_iso': '2026-05-18',
        'doc_no': 'IV6901234',
        'is_anomalous': False,
        'salesperson_code': '02',
        'bill_amount': 14544.38,
        'paid_amount': 4000.0,
        'outstanding_amount': 10544.38,
        'has_warning': False,
    }


def test_ar_snapshot_re_row_bill_is_paid_plus_outstanding():
    """RE rows (RECTYP '9') carry NETAMT 0 — the report's ยอดบิล is
    RCVAMT + REMAMT, and both the ! and *** flags are on."""
    artrn = [_ar('RE0013824', '9', netamt=0.0, rcvamt=0.0, remamt=1188.0)]

    r = _one(build_ar_snapshot_records(artrn, [_armas('C001', 'ลูกค้า ก')]), 'RE0013824')

    assert (r['bill_amount'], r['paid_amount'], r['outstanding_amount']) == (1188.0, 0.0, 1188.0)
    assert r['is_anomalous'] is True
    assert r['has_warning'] is True


def test_ar_snapshot_re_row_with_overpayment_goes_negative():
    """The real snapshot's smallest row: paid ฿0.40 against a ฿0 bill."""
    artrn = [_ar('RE0012680', '9', netamt=0.0, rcvamt=0.4, remamt=-0.4)]

    r = _one(build_ar_snapshot_records(artrn, []), 'RE0012680')

    assert (r['bill_amount'], r['paid_amount'], r['outstanding_amount']) == (0.0, 0.4, -0.4)


def test_ar_snapshot_sr_row_outstanding_sign_flips():
    """SR credit notes (RECTYP '5') are stored positive in the DBF but REDUCE
    the receivable — the report prints them negative."""
    artrn = [_ar('SR0002867', '5', netamt=92973.0, rcvamt=0.0, remamt=92973.0)]

    r = _one(build_ar_snapshot_records(artrn, []), 'SR0002867')

    assert r['outstanding_amount'] == -92973.0
    assert r['bill_amount'] == 92973.0
    assert r['is_anomalous'] is False


def test_ar_snapshot_rejects_netamt_that_breaks_the_invariant():
    """For non-RE rows NETAMT == RCVAMT + REMAMT held 189/189. A file where it
    does not is format drift, and a silently wrong ยอดบิล is worse than a
    refused import."""
    artrn = [_ar('IV6900009', '3', netamt=999.0, rcvamt=0.0, remamt=100.0)]

    with pytest.raises(ValueError, match='IV6900009'):
        build_ar_snapshot_records(artrn, [])


# ── AR: customer master join ──────────────────────────────────────────────

def test_ar_snapshot_customer_type_falls_back_to_raw_code():
    artrn = [_ar('IV6900010', '3', cuscod='C009', netamt=10.0, remamt=10.0)]
    armas = [_armas('C009', 'ลูกค้า ข', '77')]

    r = _one(build_ar_snapshot_records(artrn, armas), 'IV6900010')

    assert r['customer_type'] == '77'


def test_ar_snapshot_unknown_customer_leaves_name_and_type_blank():
    """One real row (RE0012680 / 038ก01) has no ARMAS record; the Express
    report prints a blank customer there too."""
    artrn = [_ar('RE0012680', '9', cuscod='038ก01', rcvamt=0.4, remamt=-0.4)]

    r = _one(build_ar_snapshot_records(artrn, []), 'RE0012680')

    assert r['customer_code'] == '038ก01'
    assert r['customer_name'] == ''
    assert r['customer_type'] == ''


# ── AP ────────────────────────────────────────────────────────────────────

def test_ap_snapshot_maps_every_field_and_uses_refnum():
    aptrn = [_ap('RR2600007', supcod='เซ็น', refnum='IV6900002', youref='',
                 docdat=datetime.date(2026, 1, 6),
                 netamt=385.2, rcvamt=0.0, remamt=385.2)]
    apmas = [_apmas('เซ็น', 'เซ็นไดเทรดดิ้ง จำกัด', '00')]

    r = _one(build_ap_snapshot_records(aptrn, apmas), 'RR2600007')

    assert r == {
        'supplier_type': 'ผู้จำหน่ายประจำ',
        'supplier_name': 'เซ็นไดเทรดดิ้ง จำกัด',
        'supplier_code': 'เซ็น',
        'doc_no': 'RR2600007',
        'supplier_invoice_no': 'IV6900002',
        'doc_date_iso': '2026-01-06',
        'bill_amount': 385.2,
        'paid_amount': 0.0,
        'outstanding_amount': 385.2,
    }


def test_ap_snapshot_credit_note_flips_sign_and_reads_payamt():
    """Real row GR6900005 (BSN5657, 2026-07-31), which is what caught this: a
    purchase credit note carries NETAMT == RCVAMT == REMAMT == 1040.25 with
    PAYAMT 0. So on GR rows `RCVAMT` is NOT the amount paid — it mirrors the
    credit — and `bill = paid + remaining` only holds against PAYAMT.

    Same family as MAPPING trap #5 on the payments_out side, where PAYAMT is the
    unreliable one on PS rows: on APTRN these two fields swap roles by RECTYP, and
    neither is safe to use blind.

    A credit note reduces what we owe, so the outstanding flips negative — mirroring
    how SR is already handled on the AR side, which IS tied to the Express report.
    """
    aptrn = [_ap('GR6900005', supcod='ศรีไทย', rectyp='5', refnum='',
                 docdat=datetime.date(2026, 7, 31),
                 netamt=1040.25, rcvamt=1040.25, remamt=1040.25)]
    aptrn[0]['PAYAMT'] = 0.0

    r = _one(build_ap_snapshot_records(aptrn, [_apmas('ศรีไทย', 'ศรีไทยเจริญโลหะกิจ', '03')]),
             'GR6900005')

    assert r['bill_amount'] == 1040.25
    assert r['paid_amount'] == 0.0
    assert r['outstanding_amount'] == -1040.25


def test_ap_snapshot_ordinary_invoice_still_reads_rcvamt():
    """CONTROL for the branch above: on RR rows RCVAMT IS the paid amount, and that
    is the reading that tied 7/7 to the prod snapshot. Splitting the credit-note
    case must not disturb it."""
    aptrn = [_ap('RR6900070', refnum='X', netamt=10740.6, rcvamt=740.6, remamt=10000.0)]
    aptrn[0]['PAYAMT'] = 99999.0        # the unreliable field must be ignored here

    r = _one(build_ap_snapshot_records(aptrn, []), 'RR6900070')

    assert (r['bill_amount'], r['paid_amount'], r['outstanding_amount']) == (10740.6, 740.6, 10000.0)


def test_ap_snapshot_ignores_youref():
    """YOUREF is blank on every observed open AP row; REFNUM is the one that
    tied 7/7 to the prod snapshot. A builder reading YOUREF would look fine on
    real data (both '' → '') and be wrong the day someone fills it in."""
    aptrn = [_ap('RR2600019', refnum='IV26041213', youref='ใบส่งของ 77',
                 netamt=1367.7, remamt=1367.7)]

    r = _one(build_ap_snapshot_records(aptrn, []), 'RR2600019')

    assert r['supplier_invoice_no'] == 'IV26041213'


def test_ap_snapshot_rounds_remamt_and_falls_back_on_unknown_suptyp():
    aptrn = [
        _ap('RR6900044', supcod='กิจนำ', refnum='', netamt=1717.45, remamt=1717.45),
        _ap('RR6900045', supcod='กิจนำ', refnum='', netamt=10.0, rcvamt=10.0,
            remamt=-1e-14),
    ]
    apmas = [_apmas('กิจนำ', 'กิจนำเจริญ', '02')]

    out = build_ap_snapshot_records(aptrn, apmas)

    assert [r['doc_no'] for r in out] == ['RR6900044']
    assert out[0]['supplier_type'] == '02', 'unlabelled SUPTYP falls back to the raw code'


# ── wiring: commit_express_dbf writes both snapshots ──────────────────────

def _rows(db_path, table):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(f'SELECT * FROM {table}')]
    finally:
        c.close()


def _seed_company(db_path):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO companies (code, name_th, short_name) "
                  "VALUES ('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN') "
                  "ON CONFLICT(code) DO NOTHING")
        c.commit()
    finally:
        c.close()


def _patch_tables(monkeypatch, tables):
    import express_dbf_source as eds
    monkeypatch.setattr(eds, 'open_table',
                        lambda _dir, name: list(tables.get(name.upper(), [])))


def test_commit_express_dbf_writes_ar_and_ap_snapshots(empty_db, monkeypatch):
    """End-to-end wiring: the daily zip now lands ลูกหนี้คงค้าง / เจ้าหนี้คงค้าง
    in the SAME DB it writes sales into — which is what makes the VAT book get
    its own copy for free (vat_book_builder calls this same function)."""
    import import_router

    _seed_company(empty_db)
    _patch_tables(monkeypatch, {
        'ARTRN': [_ar('IV6901234', '3', cuscod='01ว18',
                      netamt=14544.38, rcvamt=4000.0, remamt=10544.38)],
        'ARMAS': [_armas('01ว18', 'วีรวุฒิฮาร์ดแวร์', '00')],
        'APTRN': [_ap('RR6900044', supcod='กิจนำ', refnum='IV777',
                      netamt=1717.45, remamt=1717.45)],
        'APMAS': [_apmas('กิจนำ', 'กิจนำเจริญ', '00')],
    })

    out = import_router.commit_express_dbf('/nonexistent', db_path=empty_db,
                                           snapshot_date='2026-08-17')

    assert out['ar_snapshot']['imported'] == 1
    assert out['ap_snapshot']['imported'] == 1

    ar = _rows(empty_db, 'express_ar_outstanding')
    assert len(ar) == 1
    assert ar[0]['entity'] == 'BSN'
    assert ar[0]['snapshot_date_iso'] == '2026-08-17'
    assert ar[0]['outstanding_amount'] == 10544.38

    ap = _rows(empty_db, 'express_ap_outstanding')
    assert len(ap) == 1
    assert ap[0]['entity'] == 'BSN'
    assert ap[0]['snapshot_date_iso'] == '2026-08-17'
    assert ap[0]['supplier_invoice_no'] == 'IV777'


def test_commit_express_dbf_same_day_rerun_replaces_the_snapshot(empty_db, monkeypatch):
    """The team re-uploads the same zip when something goes wrong. A snapshot
    is a full replacement per (entity, date), never an append — otherwise every
    retry doubles the day's AR."""
    import import_router

    _seed_company(empty_db)
    tables = {
        'ARTRN': [_ar('IV6901234', '3', netamt=100.0, remamt=100.0)],
        'ARMAS': [_armas('C001', 'ลูกค้า ก')],
        'APTRN': [_ap('RR6900044', refnum='X', netamt=50.0, remamt=50.0)],
        'APMAS': [_apmas('S001', 'ผู้ขาย ก')],
    }
    _patch_tables(monkeypatch, tables)

    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert len(_rows(empty_db, 'express_ar_outstanding')) == 1
    assert len(_rows(empty_db, 'express_ap_outstanding')) == 1

    # A LATER day is a new snapshot, kept alongside — readers take MAX(date).
    tables['ARTRN'] = [_ar('IV6901234', '3', netamt=100.0, rcvamt=40.0, remamt=60.0)]
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    ar = _rows(empty_db, 'express_ar_outstanding')
    assert {r['snapshot_date_iso'] for r in ar} == {'2026-08-17', '2026-08-18'}
    assert sorted(r['outstanding_amount'] for r in ar) == [60.0, 100.0], (
        'the older snapshot keeps its own figure; only the same date is replaced')


def test_ar_snapshot_refuses_a_duplicated_doc_no():
    """ARTRN can carry more than one header for the same DOCNUM — models/reconcile.py
    documents exactly that ("every DOCNUM header as Express actually wrote it,
    including duplicates"). Two OPEN rows for one document would post that balance
    twice, and `express_ar_outstanding` has no unique constraint to catch it.
    Measured 2026-08-17: zero duplicates in either book, so this is latent, which is
    precisely why it needs a guard rather than a comment."""
    artrn = [_ar('IV6900011', '3', netamt=100.0, remamt=100.0),
             _ar('IV6900011', '3', netamt=100.0, remamt=100.0)]

    with pytest.raises(ValueError, match='IV6900011'):
        build_ar_snapshot_records(artrn, [])


def test_ap_snapshot_refuses_a_duplicated_doc_no():
    aptrn = [_ap('RR6900044', refnum='X', netamt=50.0, remamt=50.0),
             _ap('RR6900044', refnum='X', netamt=50.0, remamt=50.0)]

    with pytest.raises(ValueError, match='RR6900044'):
        build_ap_snapshot_records(aptrn, [])


def test_duplicate_guard_ignores_settled_rows():
    """CONTROL: only OPEN rows are in the snapshot, so a settled row sharing a
    DOCNUM with an open one is not a collision — otherwise the guard would refuse
    ordinary files."""
    artrn = [_ar('IV6900012', '3', netamt=100.0, rcvamt=100.0, remamt=0.0),
             _ar('IV6900012', '3', netamt=100.0, remamt=100.0)]

    out = build_ar_snapshot_records(artrn, [])

    assert [r['doc_no'] for r in out] == ['IV6900012']


def test_empty_snapshot_does_not_erase_the_same_day_snapshot(empty_db, monkeypatch):
    """The destructive case behind the full-replacement design: a retry that parses
    to ZERO open rows would DELETE the good rows written earlier the same day and
    insert nothing, leaving AR reading a day older with no trace of why.

    'zero open rows' and 'the parse went wrong' are indistinguishable at the writer,
    so the writer refuses and lets _commit_snapshot report it."""
    import import_router

    _seed_company(empty_db)
    tables = {
        'ARTRN': [_ar('IV6901234', '3', netamt=100.0, remamt=100.0)],
        'ARMAS': [_armas('C001', 'ลูกค้า ก')],
        'APTRN': [_ap('RR6900044', refnum='X', netamt=50.0, remamt=50.0)],
        'APMAS': [_apmas('S001', 'ผู้ขาย ก')],
    }
    _patch_tables(monkeypatch, tables)
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert len(_rows(empty_db, 'express_ar_outstanding')) == 1   # CONTROL: it landed

    # Same day, same snapshot date, but every row now reads as settled.
    tables['ARTRN'] = [_ar('IV6901234', '3', netamt=100.0, rcvamt=100.0, remamt=0.0)]
    tables['APTRN'] = [_ap('RR6900044', refnum='X', netamt=50.0, rcvamt=50.0, remamt=0.0)]
    out = import_router.commit_express_dbf('/x', db_path=empty_db,
                                           snapshot_date='2026-08-17')

    assert 'error' in out['ar_snapshot']
    assert len(_rows(empty_db, 'express_ar_outstanding')) == 1, 'the good snapshot survived'
    assert _rows(empty_db, 'express_ar_outstanding')[0]['outstanding_amount'] == 100.0
    assert 'error' in out['ap_snapshot']
    assert len(_rows(empty_db, 'express_ap_outstanding')) == 1


def test_empty_snapshot_on_a_fresh_date_is_allowed(empty_db, monkeypatch):
    """The refusal above must not fire when there is nothing to lose: a date with no
    stored rows writes nothing and reports success, so a genuinely debt-free book (or
    a first run) is not an error."""
    import import_router

    _seed_company(empty_db)
    _patch_tables(monkeypatch, {
        'ARTRN': [_ar('IV6901234', '3', netamt=100.0, rcvamt=100.0, remamt=0.0)],
        'ARMAS': [_armas('C001', 'ลูกค้า ก')],
    })

    out = import_router.commit_express_dbf('/x', db_path=empty_db,
                                           snapshot_date='2026-08-17')

    assert 'error' not in out['ar_snapshot']
    assert out['ar_snapshot']['imported'] == 0
    assert _rows(empty_db, 'express_ar_outstanding') == []


def test_commit_express_dbf_isolates_a_refusing_snapshot(empty_db, monkeypatch):
    """The ledger commits before the snapshot runs, so a refused snapshot must
    be REPORTED, not raised — otherwise a format drift makes a successful money
    import read as failed and the team re-uploads in a loop.

    The AP row is deliberately sound: the two sides must fail independently."""
    import import_router

    _seed_company(empty_db)
    _patch_tables(monkeypatch, {
        # NETAMT 999 != RCVAMT 0 + REMAMT 100 → build_ar_snapshot_records raises.
        'ARTRN': [_ar('IV6900009', '3', netamt=999.0, remamt=100.0)],
        'ARMAS': [_armas('C001', 'ลูกค้า ก')],
        'APTRN': [_ap('RR6900044', refnum='X', netamt=50.0, remamt=50.0)],
        'APMAS': [_apmas('S001', 'ผู้ขาย ก')],
    })

    out = import_router.commit_express_dbf('/x', db_path=empty_db,
                                           snapshot_date='2026-08-17')

    assert 'IV6900009' in out['ar_snapshot']['error']
    assert out['ar_snapshot']['imported'] == 0
    assert _rows(empty_db, 'express_ar_outstanding') == []
    # CONTROL: the healthy side still landed, and the ledger import is untouched.
    assert 'error' not in out['ap_snapshot']
    assert len(_rows(empty_db, 'express_ap_outstanding')) == 1


def test_commit_express_dbf_snapshot_ignores_the_sales_date_window(empty_db, monkeypatch):
    """since_days windows the LEDGER, never the outstanding snapshot: an
    unpaid 2009 invoice must still be reported as owed today."""
    import import_router

    _seed_company(empty_db)
    _patch_tables(monkeypatch, {
        'ARTRN': [_ar('IV0900001', '3', docdat=datetime.date(2009, 1, 24),
                      netamt=500.0, remamt=500.0)],
        'ARMAS': [_armas('C001', 'ลูกค้า ก')],
    })

    import_router.commit_express_dbf('/x', db_path=empty_db, since_days=60,
                                     snapshot_date='2026-08-17')

    ar = _rows(empty_db, 'express_ar_outstanding')
    assert [r['doc_no'] for r in ar] == ['IV0900001']
    assert ar[0]['outstanding_amount'] == 500.0
