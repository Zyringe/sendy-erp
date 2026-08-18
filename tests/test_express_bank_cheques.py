"""ทะเบียนเช็ค (BKTRN) — the cheque register the daily zip started carrying on
2026-08-17.

Why it matters: a customer who paid by post-dated cheque still reads as "owing"
in Sendy until it clears, so /ar would have the team chasing someone who has
already paid. On the 2026-08-17 export there were 9 such cheques worth ฿69,814.

⚠ What this module deliberately does NOT do is interpret. CHQSTAT's six values
cannot be decoded from the data — status 10 holds both long-cleared cheques and
all 9 still in the future, so it does not mean "cleared" — and the four dates
are not consistently ordered. Everything is stored under its DBF name and the
only derived field is kind, which follows from BKTRNTYP with no ambiguity.
"""
import datetime
import sqlite3

import pytest

from express_dbf_source import build_bank_cheque_records


def _bktrn(chqnum, typ='QR', **kw):
    row = {'BKTRNTYP': typ, 'CHQNUM': chqnum,
           'TRNDAT': datetime.date(2026, 8, 15), 'CHQDAT': datetime.date(2026, 8, 15),
           'GETDAT': datetime.date(2026, 7, 15), 'PAYINDAT': None,
           'BNKCOD': '04', 'BRANCH': '', 'BNKACC': '', 'CUSCOD': 'C001',
           'NAME': 'ร้าน จุ้นเฮงโลหะกิจ', 'AMOUNT': 13490.0, 'CHARGE': 0.0,
           'VATAMT': 0.0, 'NETAMT': 13490.0, 'REMAMT': 0.0, 'CHQSTAT': '00',
           'REMARK': '', 'REFDOC': '~', 'REFNUM': '', 'VOUCHER': ''}
    row.update(kw)
    return row


def test_maps_a_real_received_cheque():
    r = build_bank_cheque_records([_bktrn('QR00803324')])[0]

    assert r == {
        'kind': 'received',
        'type_code': 'QR',
        'cheque_no': 'QR00803324',
        'trn_date_iso': '2026-08-15',
        'cheque_date_iso': '2026-08-15',
        'received_date_iso': '2026-07-15',
        'paid_in_date_iso': None,
        'bank_code': '04',
        'branch': '',
        'bank_account': '',
        'party_code': 'C001',
        'party_name': 'ร้าน จุ้นเฮงโลหะกิจ',
        'amount': 13490.0,
        'charge': 0.0,
        'vat_amount': 0.0,
        'net_amount': 13490.0,
        'remaining_amount': 0.0,
        'status_code': '00',
        'remark': '',
        'ref_doc': '',
        'ref_no': '',
        'voucher': '',
    }


def test_kind_follows_the_type_code():
    """The one derived field. QR/QP is unambiguous — 1,426 QP rows are exactly
    the 1,426 CHQSTAT '05' rows — unlike CHQSTAT itself."""
    out = {r['cheque_no']: r['kind'] for r in build_bank_cheque_records(
        [_bktrn('A', typ='QR'), _bktrn('B', typ='QP'), _bktrn('C', typ='BQ')])}

    assert out == {'A': 'received', 'B': 'paid', 'C': 'other'}


def test_status_code_is_carried_verbatim_and_not_translated():
    """Status 10 covers both long-cleared cheques and every one still in the
    future, so any mapping to a business meaning would be invented. Storing it
    raw keeps the option open; inventing a label closes it wrongly."""
    rows = [_bktrn(str(i), CHQSTAT=s) for i, s in enumerate(('10', '05', '01', '00', '20', '02'))]

    assert sorted(r['status_code'] for r in build_bank_cheque_records(rows)) == \
        ['00', '01', '02', '05', '10', '20']


def test_the_tilde_placeholder_becomes_blank():
    """Express writes '~' for an empty reference, the same placeholder ARBIL and
    APTRN use. Carried through verbatim it would read as a real document."""
    r = build_bank_cheque_records([_bktrn('X', REFDOC='~', REFNUM='~')])[0]

    assert r['ref_doc'] == '' and r['ref_no'] == ''


def test_duplicate_cheque_numbers_are_kept():
    """CHQNUM repeats on 38 of the 12,805 rows, which is why this table is
    replaced per entity rather than upserted. Refusing duplicates — the right
    call for ใบวางบิล, where BILNUM IS unique — would reject the real file here."""
    rows = [_bktrn('QR00082463', AMOUNT=100.0), _bktrn('QR00082463', AMOUNT=200.0)]

    assert sorted(r['amount'] for r in build_bank_cheque_records(rows)) == [100.0, 200.0]


def test_takes_no_cutoff_argument():
    """Post-dated cheques run months ahead and the register is the whole book."""
    with pytest.raises(TypeError):
        build_bank_cheque_records([], cutoff=datetime.date(2026, 6, 18))


# ── wiring through commit_express_dbf ───────────────────────────────────────

def _rows(db_path):
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute('SELECT * FROM express_bank_cheques')]
    finally:
        c.close()


def _seed_company(db_path):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO companies (code, name_th, short_name) "
                  "VALUES ('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN') ON CONFLICT(code) DO NOTHING")
        c.commit()
    finally:
        c.close()


def _patch(monkeypatch, tables):
    import express_dbf_source as eds

    def fake(_dir, name):
        key = name.upper()
        if key in tables:
            return list(tables[key])
        if key in ('BKTRN', 'ARBIL'):
            raise FileNotFoundError(f'{key}.DBF')
        return []
    monkeypatch.setattr(eds, 'open_table', fake)


def test_commit_replaces_rather_than_accumulating(empty_db, monkeypatch):
    """Replace-per-entity is the whole design (no unique key to upsert on), so
    the test that matters is that a second import does not double the register."""
    import import_router

    tables = {'BKTRN': [_bktrn('A', AMOUNT=100.0), _bktrn('B', AMOUNT=200.0)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert out['bank_cheques']['stored'] == 2
    assert len(_rows(empty_db)) == 2

    tables['BKTRN'] = [_bktrn('A', AMOUNT=999.0)]
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    rows = _rows(empty_db)
    assert len(rows) == 1, 'the register is replaced, not appended to'
    assert rows[0]['amount'] == 999.0


def test_an_empty_register_does_not_erase_a_stored_one(empty_db, monkeypatch):
    """Same guard as the outstanding snapshots, for the same reason: a parse that
    yields nothing and a genuinely empty register look identical here, and
    deleting a good register to write nothing is the one unrecoverable outcome."""
    import import_router

    tables = {'BKTRN': [_bktrn('A', AMOUNT=100.0)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert len(_rows(empty_db)) == 1                      # CONTROL

    tables['BKTRN'] = []
    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    assert 'error' in out['bank_cheques']
    assert len(_rows(empty_db)) == 1, 'the stored register survived'


def test_an_empty_register_on_a_first_run_is_fine(empty_db, monkeypatch):
    """CONTROL for the guard above: with nothing to lose it must not complain."""
    import import_router

    _patch(monkeypatch, {'BKTRN': []})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert out['bank_cheques']['stored'] == 0 and 'error' not in out['bank_cheques']


def test_a_zip_without_bktrn_still_imports(empty_db, monkeypatch):
    import import_router

    _patch(monkeypatch, {})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert 'skipped' in out['bank_cheques'] and 'error' not in out['bank_cheques']
    assert 'error' not in out['ar_snapshot'], 'the rest of the import is unaffected'
