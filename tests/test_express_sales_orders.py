"""ใบสั่งขาย (OESO + OESOIT) — customer demand ordered but not yet invoiced.

⚠ The number that matters before anyone builds a view on this: 1,732 of the
9,333 orders carry a remaining quantity, but only 20 are dated 2026 (฿112,464).
The other 1,712 run back to 2003 and are orders nobody closed out. An "open
orders" view without a date filter reports ฿13.98M of demand that does not exist.

DOCSTAT (M/N/C) is stored verbatim for the same reason as BKTRN.CHQSTAT: the
letters are not decodable from the data, and a guessed label becomes permanent.
"""
import datetime
import sqlite3

import pytest

from express_dbf_source import build_sales_order_records


def _oeso(sonum, *, sodat=None, cuscod='01ว18', docstat='N', total=1417.0, **kw):
    row = {'SONUM': sonum, 'SODAT': sodat or datetime.date(2026, 7, 7),
           'CUSCOD': cuscod, 'SLMCOD': '02', 'YOUREF': '', 'PAYTRM': 30,
           'DLVDAT': None, 'CMPLDAT': None, 'TOTAL': total, 'DISCAMT': 0.0,
           'VATAMT': 0.0, 'NETAMT': total, 'DOCSTAT': docstat}
    row.update(kw)
    return row


def _oesoit(sonum, seq, *, stkcod='804', ordqty=10.0, remqty=0.0, cancelqty=0.0,
            unitpr=45.0, trnval=450.0):
    return {'SONUM': sonum, 'SEQNUM': seq, 'STKCOD': stkcod, 'STKDES': 'ของทดสอบ',
            'ORDQTY': ordqty, 'CANCELQTY': cancelqty, 'REMQTY': remqty,
            'TQUCOD': 'ตัว', 'UNITPR': unitpr, 'TRNVAL': trnval}


def _armas(cuscod, cusnam):
    return {'CUSCOD': cuscod, 'CUSNAM': cusnam}


def test_maps_a_header_and_its_lines():
    heads, lines = build_sales_order_records(
        [_oeso('SO0008842')], [_oesoit('SO0008842', 1, remqty=4.0)],
        [_armas('01ว18', 'วีรวุฒิฮาร์ดแวร์')])

    assert heads == [{
        'so_no': 'SO0008842', 'so_date_iso': '2026-07-07',
        'customer_code': '01ว18', 'customer_name': 'วีรวุฒิฮาร์ดแวร์',
        'salesperson_code': '02', 'your_ref': '', 'pay_terms': 30,
        'delivery_date_iso': None, 'completed_date_iso': None,
        'total': 1417.0, 'discount_amount': 0.0, 'vat_amount': 0.0,
        'net_amount': 1417.0, 'status_code': 'N',
    }]
    assert lines == [{
        'so_no': 'SO0008842', 'line_seq': 1, 'product_code': '804',
        'product_name': 'ของทดสอบ', 'ordered_qty': 10.0, 'cancelled_qty': 0.0,
        'remaining_qty': 4.0, 'unit': 'ตัว', 'unit_price': 45.0, 'line_total': 450.0,
    }]


def test_status_is_carried_verbatim():
    heads, _ = build_sales_order_records(
        [_oeso('A', docstat='M'), _oeso('B', docstat='N'), _oeso('C', docstat='C')], [], [])

    assert {h['so_no']: h['status_code'] for h in heads} == {'A': 'M', 'B': 'N', 'C': 'C'}


def test_lines_of_an_unknown_order_are_dropped():
    """A line whose header is missing would violate the (entity, so_no) shape and
    could never be shown against an order — Express should not produce one, so a
    silent orphan is a signal, not something to store."""
    _, lines = build_sales_order_records(
        [_oeso('SO1')], [_oesoit('SO1', 1), _oesoit('SO-GHOST', 1)], [])

    assert [l['so_no'] for l in lines] == ['SO1']


def test_refuses_a_duplicated_order_number():
    """SONUM is unique across all 9,333 rows, which is what makes the
    (entity, so_no) key safe."""
    with pytest.raises(ValueError, match='SO0008842'):
        build_sales_order_records([_oeso('SO0008842'), _oeso('SO0008842')], [], [])


def test_refuses_a_duplicated_line_sequence():
    """(so_no, line_seq) is the line key; a repeat would silently drop one line."""
    with pytest.raises(ValueError, match='SO1'):
        build_sales_order_records([_oeso('SO1')],
                                  [_oesoit('SO1', 1), _oesoit('SO1', 1)], [])


def test_takes_no_cutoff_argument():
    """Orders stay open for years — the oldest with a remaining quantity is from
    2003 — so a window would hide exactly what the table is for."""
    with pytest.raises(TypeError):
        build_sales_order_records([], [], [], cutoff=datetime.date(2026, 6, 18))


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
        if key in ('OESO', 'OESOIT', 'BKTRN', 'ARBIL'):
            raise FileNotFoundError(f'{key}.DBF')
        return []
    monkeypatch.setattr(eds, 'open_table', fake)


def test_commit_stores_orders_and_lines_then_replaces_them(empty_db, monkeypatch):
    import import_router

    tables = {'OESO': [_oeso('SO1'), _oeso('SO2')],
              'OESOIT': [_oesoit('SO1', 1), _oesoit('SO1', 2), _oesoit('SO2', 1)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert out['sales_orders'] == {'orders': 2, 'lines': 3}

    tables['OESO'] = [_oeso('SO1')]
    tables['OESOIT'] = [_oesoit('SO1', 1)]
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    assert len(_q(empty_db, 'SELECT * FROM express_sales_orders')) == 1
    assert len(_q(empty_db, 'SELECT * FROM express_sales_order_lines')) == 1, \
        'lines are replaced with their headers, never left orphaned'


def test_an_empty_order_book_does_not_erase_a_stored_one(empty_db, monkeypatch):
    import import_router

    tables = {'OESO': [_oeso('SO1')], 'OESOIT': [_oesoit('SO1', 1)]}
    _patch(monkeypatch, tables)
    _seed_company(empty_db)
    import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')
    assert len(_q(empty_db, 'SELECT * FROM express_sales_orders')) == 1   # CONTROL

    tables['OESO'] = []
    tables['OESOIT'] = []
    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-18')

    assert 'error' in out['sales_orders']
    assert len(_q(empty_db, 'SELECT * FROM express_sales_orders')) == 1


def test_a_zip_without_oeso_still_imports(empty_db, monkeypatch):
    import import_router

    _patch(monkeypatch, {})
    _seed_company(empty_db)

    out = import_router.commit_express_dbf('/x', db_path=empty_db, snapshot_date='2026-08-17')

    assert 'skipped' in out['sales_orders'] and 'error' not in out['sales_orders']
    assert 'error' not in out['ar_snapshot']
