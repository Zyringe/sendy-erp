"""บัญชีแยกประเภท (GLACC + GLJNL + GLJNLIT).

Sendy's /accounting computes profit from sales minus cost, which is an estimate.
The GL is the book the accountant actually closes, so this makes an independent
figure available to check it against.

Two things here are unlike the other Express datasets:

  * it is WINDOWED, and for a measured reason rather than a preference — the
    whole GL is 62MB in SQLite and prod's volume had 214MB free on 2026-08-18,
    so the full book across both books would take ~87MB of it before backups
    grow to match. A full volume stops Sendy writing at all.
  * entry_side IS derived, unlike CHQSTAT and DOCSTAT which are carried raw.
    That is allowed here because it is proven, not guessed: account
    41-01-00-00 รายได้จากการขาย carries 53,764 lines and every one is TRNTYP
    '1'. Income is credited, so 1 = credit, 0 = debit.
"""
import datetime
import sqlite3

import pytest

from express_dbf_source import build_gl_records


def _glacc(num, nam='บัญชี', **kw):
    row = {'ACCNUM': num, 'ACCNAM': nam, 'LEVEL': 4, 'PARENT': '',
           'ACCTYP': '', 'NATURE': '', 'STATUS': 'A'}
    row.update(kw)
    return row


def _gljnl(voucher, *, voudat=None, jnltyp='02', descrp='', refnum='', **kw):
    row = {'VOUCHER': voucher, 'VOUDAT': voudat or datetime.date(2026, 8, 15),
           'JNLTYP': jnltyp, 'DESCRP': descrp, 'REFNUM': refnum,
           'SRCJNL': '', 'DOCSTAT': 'N'}
    row.update(kw)
    return row


def _gljnlit(voucher, seq, accnum, trntyp, amount, *, voudat=None, descrp=''):
    return {'VOUCHER': voucher, 'SEQIT': seq, 'ACCNUM': accnum, 'TRNTYP': trntyp,
            'AMOUNT': amount, 'VOUDAT': voudat or datetime.date(2026, 8, 15),
            'DESCRP': descrp}


CUTOFF = datetime.date(2024, 1, 1)


def test_maps_a_real_voucher_and_its_lines():
    """QR46058065 from the 2026-08-17 export: a cheque clearing, ฿7,800 credited
    off เช็ครับลงวันที่ล่วงหน้า against ฿7,790 into the bank and ฿10 of charges."""
    jnl = [_gljnl('QR46058065', voudat=datetime.date(2026, 12, 18), jnltyp='02',
                  descrp='เช็คผ่าน ร้าน ทองประสิทธิ์พาณิชย์')]
    it = [_gljnlit('QR46058065', 1, '11-01-02-03', '0', 7790.0,
                   voudat=datetime.date(2026, 12, 18)),
          _gljnlit('QR46058065', 2, '53-02-02-00', '0', 10.0,
                   voudat=datetime.date(2026, 12, 18)),
          _gljnlit('QR46058065', 3, '11-02-02-00', '1', 7800.0,
                   voudat=datetime.date(2026, 12, 18))]

    accounts, vouchers, lines = build_gl_records([_glacc('11-01-02-03')], jnl, it, CUTOFF)

    assert vouchers[0]['voucher'] == 'QR46058065'
    assert vouchers[0]['voucher_date_iso'] == '2026-12-18'
    assert vouchers[0]['description'] == 'เช็คผ่าน ร้าน ทองประสิทธิ์พาณิชย์'
    assert [(l['account_no'], l['entry_side'], l['amount']) for l in lines] == [
        ('11-01-02-03', 'debit', 7790.0),
        ('53-02-02-00', 'debit', 10.0),
        ('11-02-02-00', 'credit', 7800.0),
    ]
    assert accounts[0]['account_no'] == '11-01-02-03'


def test_entry_side_is_derived_but_the_raw_code_is_kept():
    """Derived because it is proven; kept raw so a future correction never needs
    to re-read the DBF."""
    _, _, lines = build_gl_records([], [_gljnl('V1')],
                                   [_gljnlit('V1', 1, 'A', '0', 5.0),
                                    _gljnlit('V1', 2, 'B', '1', 5.0)], CUTOFF)

    assert [(l['type_code'], l['entry_side']) for l in lines] == [('0', 'debit'), ('1', 'credit')]


def test_an_unexpected_type_code_is_refused():
    """0 and 1 are the only values in all 359,003 lines. A third would mean the
    proof no longer holds, and silently labelling it would put a wrong side on a
    money row."""
    with pytest.raises(ValueError, match='V1'):
        build_gl_records([], [_gljnl('V1')], [_gljnlit('V1', 1, 'A', '7', 5.0)], CUTOFF)


def test_the_window_drops_older_vouchers_and_their_lines():
    """The trade-off this dataset is built around — see the module docstring."""
    jnl = [_gljnl('OLD', voudat=datetime.date(2019, 5, 1)),
           _gljnl('NEW', voudat=datetime.date(2026, 8, 15))]
    it = [_gljnlit('OLD', 1, 'A', '0', 1.0, voudat=datetime.date(2019, 5, 1)),
          _gljnlit('NEW', 1, 'A', '0', 2.0, voudat=datetime.date(2026, 8, 15))]

    _, vouchers, lines = build_gl_records([], jnl, it, CUTOFF)

    assert [v['voucher'] for v in vouchers] == ['NEW']
    assert [l['voucher'] for l in lines] == ['NEW'], \
        'a line whose voucher was windowed out has nothing to hang from'


def test_the_chart_of_accounts_is_never_windowed():
    """135 rows that old lines and new both point at — windowing it would leave
    account numbers that resolve to nothing."""
    accounts, _, _ = build_gl_records([_glacc('11-01-01-00'), _glacc('41-01-00-00')],
                                      [], [], CUTOFF)

    assert sorted(a['account_no'] for a in accounts) == ['11-01-01-00', '41-01-00-00']


def test_refuses_a_duplicated_voucher():
    """VOUCHER is unique across all 109,458, which is what makes (entity,
    voucher) safe. SEQIT is NOT unique — 3,367 repeats — so lines carry no key
    and are replaced wholesale instead."""
    with pytest.raises(ValueError, match='V1'):
        build_gl_records([], [_gljnl('V1'), _gljnl('V1')], [], CUTOFF)


def test_duplicate_line_sequences_are_kept():
    """CONTROL for the above: 3,367 (VOUCHER, SEQIT) pairs really do repeat in
    the book, so refusing them would reject the real file."""
    _, _, lines = build_gl_records([], [_gljnl('V1')],
                                   [_gljnlit('V1', 1, 'A', '0', 5.0),
                                    _gljnlit('V1', 1, 'B', '1', 5.0)], CUTOFF)

    assert len(lines) == 2


# ── wiring through commit_express_dbf ───────────────────────────────────────

def _q(db_path, sql):
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql)]
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
        if key in ('GLACC', 'GLJNL', 'GLJNLIT', 'OESO', 'OESOIT', 'BKTRN', 'ARBIL'):
            raise FileNotFoundError(f'{key}.DBF')
        return []
    monkeypatch.setattr(eds, 'open_table', fake)


def test_commit_stores_the_ledger_and_replaces_it_next_run(empty_db, monkeypatch):
    import import_router

    tables = {'GLACC': [_glacc('11-01-01-00', 'เงินสด')],
              'GLJNL': [_gljnl('V1'), _gljnl('V2')],
              'GLJNLIT': [_gljnlit('V1', 1, '11-01-01-00', '0', 100.0),
                          _gljnlit('V2', 1, '11-01-01-00', '1', 100.0)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert out['general_ledger'] == {'accounts': 1, 'vouchers': 2, 'lines': 2}

    tables['GLJNL'] = [_gljnl('V1')]
    tables['GLJNLIT'] = [_gljnlit('V1', 1, '11-01-01-00', '0', 100.0)]
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    assert len(_q(empty_db, 'SELECT * FROM express_gl_vouchers')) == 1
    assert len(_q(empty_db, 'SELECT * FROM express_gl_lines')) == 1, \
        'lines are replaced with their vouchers, never left pointing at nothing'


def test_an_empty_journal_does_not_erase_a_stored_one(empty_db, monkeypatch):
    import import_router

    tables = {'GLACC': [_glacc('A')], 'GLJNL': [_gljnl('V1')],
              'GLJNLIT': [_gljnlit('V1', 1, 'A', '0', 1.0)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert len(_q(empty_db, 'SELECT * FROM express_gl_vouchers')) == 1     # CONTROL

    tables['GLJNL'] = []
    tables['GLJNLIT'] = []
    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    assert 'error' in out['general_ledger']
    assert len(_q(empty_db, 'SELECT * FROM express_gl_vouchers')) == 1


def test_a_zip_without_the_gl_still_imports(empty_db, monkeypatch):
    import import_router

    _patch(monkeypatch, {})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert 'skipped' in out['general_ledger'] and 'error' not in out['general_ledger']
    assert 'error' not in out['ar_snapshot']


def test_debits_equal_credits_for_what_was_stored(empty_db, monkeypatch):
    """The GL's own invariant, and the reason it is worth having: a book that
    does not balance cannot be used to check anything else."""
    import import_router

    _patch(monkeypatch, {
        'GLACC': [_glacc('11-01-02-03'), _glacc('11-02-02-00'), _glacc('53-02-02-00')],
        'GLJNL': [_gljnl('QR46058065', voudat=datetime.date(2026, 8, 15))],
        'GLJNLIT': [_gljnlit('QR46058065', 1, '11-01-02-03', '0', 7790.0),
                    _gljnlit('QR46058065', 2, '53-02-02-00', '0', 10.0),
                    _gljnlit('QR46058065', 3, '11-02-02-00', '1', 7800.0)]})
    _seed_company(empty_db)

    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    rows = _q(empty_db, "SELECT entry_side, SUM(amount) t FROM express_gl_lines "
                        "GROUP BY entry_side ORDER BY entry_side")
    assert rows == [{'entry_side': 'credit', 't': 7800.0},
                    {'entry_side': 'debit', 't': 7800.0}]
