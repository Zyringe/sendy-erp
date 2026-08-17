"""ใบวางบิล (ARBIL) — the billing notes the daily zip started carrying on
2026-08-17.

Why they matter: `/ar` cannot tell an invoice nobody has billed yet from one
that has been formally billed and is waiting for the customer's payment run.
Measured on the 2026-08-17 export, 17 of the 170 invoices it would chase
(฿107,845) were already on a ใบวางบิล.

Deliberately NOT windowed. The bills referenced by currently-open invoices are
dated 2014-02-01 .. 2026-07-25, and a 60-day window would miss 11 of the 19 —
the whole table is 11,925 rows, so importing all of it costs nothing and is the
only way a bill_no on an old open invoice ever resolves to a name and a date.
"""
import datetime
import sqlite3

import pytest

from express_dbf_source import build_billing_note_records


def _arbil(bilnum, *, bildat=None, cuscod='C001', netamt=0.0, docstat='N',
           bilout=None, appdat=None, paycond='', remark=''):
    return {'BILNUM': bilnum, 'BILDAT': bildat or datetime.date(2026, 7, 25),
            'CUSCOD': cuscod, 'NETAMT': netamt, 'DOCSTAT': docstat,
            'BILOUT': bilout, 'APPDAT': appdat, 'PAYCOND': paycond,
            'REMARK': remark}


def _armas(cuscod, cusnam):
    return {'CUSCOD': cuscod, 'CUSNAM': cusnam}


def test_maps_every_field_from_a_real_row():
    """BI6800508, the newest bill in the 2026-08-17 export."""
    arbil = [_arbil('BI6800508', bildat=datetime.date(2026, 7, 25), cuscod='11ป12',
                    netamt=13395.0, bilout=datetime.date(2026, 7, 25),
                    paycond='เครดิต 30 วัน', remark='รอบวางบิลสิ้นเดือน')]

    r = build_billing_note_records(arbil, [_armas('11ป12', 'ประเสริฐค้าวัสดุ')])[0]

    assert r == {
        'bill_no': 'BI6800508',
        'bill_date_iso': '2026-07-25',
        'sent_date_iso': '2026-07-25',
        'approved_date_iso': None,
        'customer_code': '11ป12',
        'customer_name': 'ประเสริฐค้าวัสดุ',
        'pay_cond': 'เครดิต 30 วัน',
        'net_amount': 13395.0,
        'is_cancelled': False,
        'remark': 'รอบวางบิลสิ้นเดือน',
    }


def test_cancelled_bills_are_kept_but_flagged():
    """138 of the 11,925 rows are DOCSTAT 'C'. They are kept so a cancelled
    bill_no still resolves — dropping them would leave an invoice pointing at a
    bill number that does not exist, which reads as a data bug."""
    arbil = [_arbil('BI6800001', docstat='C'), _arbil('BI6800002', docstat='N')]

    out = {r['bill_no']: r['is_cancelled'] for r in build_billing_note_records(arbil, [])}

    assert out == {'BI6800001': True, 'BI6800002': False}


def test_unknown_customer_leaves_the_name_blank():
    arbil = [_arbil('BI6800003', cuscod='ZZZ')]

    assert build_billing_note_records(arbil, [])[0]['customer_name'] == ''


def test_skips_rows_with_no_bill_number():
    arbil = [_arbil('   '), _arbil('BI6800004')]

    assert [r['bill_no'] for r in build_billing_note_records(arbil, [])] == ['BI6800004']


def test_refuses_a_duplicated_bill_number():
    """BILNUM is unique across all 11,925 rows today. A duplicate would make the
    (entity, bill_no) upsert silently keep whichever row happened to be last."""
    arbil = [_arbil('BI6800005', netamt=100.0), _arbil('BI6800005', netamt=999.0)]

    with pytest.raises(ValueError, match='BI6800005'):
        build_billing_note_records(arbil, [])


def test_takes_no_cutoff_argument():
    """A window here would drop the old bills that open invoices still point at
    — 11 of the 19 referenced bills predate a 60-day window."""
    with pytest.raises(TypeError):
        build_billing_note_records([], [], cutoff=datetime.date(2026, 6, 18))


# ── wiring through commit_express_dbf ───────────────────────────────────────

def _rows(db_path, table):
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(f'SELECT * FROM {table}')]
    finally:
        c.close()


def _seed_company(db_path):
    """empty_db carries the live SCHEMA with zero rows, and the express writers
    require companies code='BSN' (mig 011's seed, which the bootstrap runner
    stamps without executing). Force it rather than inherit it."""
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO companies (code, name_th, short_name) "
                  "VALUES ('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN') "
                  "ON CONFLICT(code) DO NOTHING")
        c.commit()
    finally:
        c.close()


def _patch(monkeypatch, tables):
    import express_dbf_source as eds
    real = eds.open_table

    def fake(_dir, name):
        key = name.upper()
        if key in tables:
            return list(tables[key])
        if key == 'ARBIL':                    # simulate a pre-2026-08-17 zip
            raise FileNotFoundError(f'{key}.DBF')
        return []
    monkeypatch.setattr(eds, 'open_table', fake)
    return real


def test_commit_express_dbf_stores_and_then_updates_a_billing_note(empty_db, monkeypatch):
    """Upsert by identity, not replace-per-date: the same bill re-imported the
    next day must update in place, or the table would double every run."""
    import import_router

    tables = {'ARBIL': [_arbil('BI6800508', netamt=13395.0, cuscod='11ป12')],
              'ARMAS': [_armas('11ป12', 'ประเสริฐค้าวัสดุ')]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert out['billing_notes']['upserted'] == 1
    rows = _rows(empty_db, 'express_billing_notes')
    assert len(rows) == 1 and rows[0]['net_amount'] == 13395.0
    assert rows[0]['entity'] == 'BSN'

    tables['ARBIL'] = [_arbil('BI6800508', netamt=99999.0, cuscod='11ป12')]
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    rows = _rows(empty_db, 'express_billing_notes')
    assert len(rows) == 1, 'the same bill must not be inserted twice'
    assert rows[0]['net_amount'] == 99999.0


def test_a_zip_without_arbil_still_imports(empty_db, monkeypatch):
    """A flash drive that has not been refreshed still produces the old 9-table
    zip. That is a thinner import, not a failed one."""
    import import_router

    _patch(monkeypatch, {'ARMAS': [_armas('C001', 'ลูกค้า ก')]})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert out['billing_notes']['upserted'] == 0
    assert 'skipped' in out['billing_notes']
    assert 'error' not in out['billing_notes']
    assert _rows(empty_db, 'express_billing_notes') == []
    # CONTROL: the rest of the import still ran.
    assert out['sales']['imported'] == 0 and 'error' not in out['ar_snapshot']


def test_a_bad_arbil_does_not_fail_the_whole_import(empty_db, monkeypatch):
    """Reference data, imported after the ledger has already committed — a
    refusal here must be reported, not raised."""
    import import_router

    _patch(monkeypatch, {'ARBIL': [_arbil('BI1'), _arbil('BI1')],   # duplicate
                         'ARMAS': [_armas('C001', 'ลูกค้า ก')]})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert 'BI1' in out['billing_notes']['error']
    assert _rows(empty_db, 'express_billing_notes') == []
    assert 'error' not in out['ar_snapshot'], 'the rest of the import is unaffected'


def test_an_unreadable_arbil_is_reported_as_an_error_not_as_absent(empty_db, monkeypatch):
    """"Not in this zip" and "in the zip but corrupt" are different facts, and
    reporting the second as the first would hide a broken export behind a
    reassuring message. Both are isolated from the ledger either way."""
    import import_router
    import express_dbf_source as eds

    def fake(_dir, name):
        if name.upper() == 'ARBIL':
            raise ValueError('dbf header is garbage')
        if name.upper() == 'ARMAS':
            return [_armas('C001', 'ลูกค้า ก')]
        return []
    monkeypatch.setattr(eds, 'open_table', fake)
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert 'garbage' in out['billing_notes']['error']
    assert 'skipped' not in out['billing_notes'], 'a corrupt file is not an absent file'
    # CONTROL: the ledger import is untouched by it.
    assert 'error' not in out['ar_snapshot']
