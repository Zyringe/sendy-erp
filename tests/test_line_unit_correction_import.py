"""แก้หน่วยบรรทัด (#692) against the daily zip: every test goes through
`import_router.commit_express_dbf`, the entry point the upload route calls.

IV6900001-1 (2 โหล, ledger -24) is corrected to หลอด in `hold` mode: ledger -2
plus an offset of -22, stock 176 as before the correction.
"""
import datetime

import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'
KEY = (LINE, sc.CODE)
KIND = 'unit_correction_retired'


def _apply(path, mode='hold', *, doc_no=LINE, unit='หลอด'):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return luc.apply(c, doc_no, sc.CODE, unit, mode, sc.REASON, 'put')
    finally:
        c.close()


@pytest.fixture
def corrected(empty_db, monkeypatch):
    """(db path, product id, correction id) with the standard book imported
    and IV6900001-1 corrected in hold mode."""
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db, ratios=(('โหล', 12), ('กล่อง', 6)))
    sc.run_zip(monkeypatch, sc.standard_book())
    cid = _apply(empty_db)
    assert sc.sale_ledger(empty_db, LINE, pid)[0][1] == -2
    assert len(sc.offsets(empty_db, pid)) == 1
    assert sc.stock(empty_db, pid) == 176
    return empty_db, pid, cid


def _book(line):
    """The standard book with the sale line replaced by `line`
    (qty, unit, price), or dropped when None."""
    book = (sc.Book()
            .purchase('RR6900001', [(1, sc.CODE, 100.0, 'หลอด', 10.0)],
                      datetime.date(2026, 3, 10))
            .purchase('RR6900002', [(1, sc.CODE, 100.0, 'หลอด', 20.0)],
                      datetime.date(2026, 5, 1)))
    lines = [(2, 'OTHER', 1.0, 'หลอด', 5.0)]
    if line is not None:
        lines.insert(0, (1, sc.CODE) + tuple(line))
    return book.sale('IV6900001', lines)


def _control(tmp_path, monkeypatch, path, book):
    """Run `book` through the zip on a DB that never had the correction and
    return that DB's path. `path` must not be corrected yet."""
    control = sc.clone_db(path, str(tmp_path / 'control.db'))
    with sc.on_db(monkeypatch, control) as m:
        sc.run_zip(m, book)
    return control


def test_the_zip_reads_a_corrected_line_as_unchanged(corrected, monkeypatch):
    path, pid, cid = corrected
    row_id = sc.sales_row(path, LINE)['id']
    ledger = sc.sale_ledger(path, LINE, pid)

    result = sc.run_zip(monkeypatch, sc.standard_book())

    assert result['sales']['unchanged'] == 1
    assert result['sales']['overwritten'] == 0
    assert result['sales']['unit_corrections_retired'] == 0
    row = sc.sales_row(path, LINE)
    assert (row['id'], row['unit']) == (row_id, 'หลอด')
    assert sc.sale_ledger(path, LINE, pid) == ledger
    assert sc.stock(path, pid) == 176
    assert sc.corrections(path)[0]['status'] == 'active'
    assert sc.alerts(path, KIND) == []


def test_a_sibling_change_replays_the_corrected_line_at_its_corrected_qty(
        empty_db, monkeypatch):
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.standard_book().sale(
        'IV6900002', [(1, sc.CODE, 1.0, 'โหล', 49.0)], datetime.date(2026, 4, 2)))
    _apply(empty_db)
    assert sc.stock(empty_db, pid) == 164
    offset_id = sc.offsets(empty_db, pid)[0]['id']
    ledger_id = sc.sale_ledger(empty_db, LINE, pid)[0][0]

    result = sc.run_zip(monkeypatch, sc.standard_book().sale(
        'IV6900002', [(1, sc.CODE, 3.0, 'โหล', 49.0)], datetime.date(2026, 4, 2)))

    assert result['sales']['overwritten'] == 1
    (new_id, qty), = sc.sale_ledger(empty_db, LINE, pid)
    assert qty == -2 and new_id != ledger_id
    assert [r['id'] for r in sc.offsets(empty_db, pid)] == [offset_id]
    assert sc.sale_ledger(empty_db, 'IV6900002-1', pid)[0][1] == -36
    assert sc.stock(empty_db, pid) == 140
    assert sc.corrections(empty_db)[0]['status'] == 'active'
    assert sc.sales_row(empty_db, LINE)['unit'] == 'หลอด'


@pytest.mark.parametrize('express_line,unit,ledger_qty', [
    ((3.0, 'โหล', 49.0), 'โหล', -36),
    ((2.0, 'กล่อง', 49.0), 'กล่อง', -12),
], ids=['qty', 'third-unit'])
def test_express_changing_the_line_retires_the_correction(
        empty_db, tmp_path, monkeypatch, express_line, unit, ledger_qty):
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db, ratios=(('โหล', 12), ('กล่อง', 6)))
    sc.seed_product(empty_db, 'OTHER', ratios=(), name='อื่น')
    sc.run_zip(monkeypatch, _book((2.0, 'โหล', 49.0)))
    control = _control(tmp_path, monkeypatch, empty_db, _book(express_line))
    cid = _apply(empty_db)
    assert len(sc.offsets(empty_db, pid)) == 1

    result = sc.run_zip(monkeypatch, _book(express_line))

    assert result['sales']['unit_corrections_retired'] == 1
    assert result['sales']['unit_correction_docs'] == ['IV6900001']
    correction, = sc.corrections(empty_db)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_changed')
    assert correction['ended_at'] is not None
    row = sc.sales_row(empty_db, LINE)
    assert (row['qty'], row['unit']) == (express_line[0], unit)
    assert sc.offsets(empty_db, pid) == []
    assert sc.sale_ledger(empty_db, LINE, pid)[0][1] == ledger_qty
    assert sc.semantic_state(empty_db, pid)['ledger'] == \
        sc.semantic_state(control, pid)['ledger']
    assert sc.stock(empty_db, pid) == sc.stock(control, pid) == 200 + ledger_qty
    assert sc.cost_ledger(empty_db, pid) == sc.cost_ledger(control, pid)
    alert, = sc.alerts(empty_db, KIND)
    assert LINE in alert['message'] and f'"correction_id": {cid}' in alert['context_json']


def test_express_agreeing_with_the_correction_retires_it_and_keeps_the_offset(
        corrected, monkeypatch):
    path, pid, cid = corrected
    row_id = sc.sales_row(path, LINE)['id']
    offset_id = sc.offsets(path, pid)[0]['id']
    agreeing = (sc.Book()
                .purchase('RR6900001', [(1, sc.CODE, 100.0, 'หลอด', 10.0)],
                          datetime.date(2026, 3, 10))
                .sale('IV6900001', [(1, sc.CODE, 2.0, 'หลอด', 49.0)])
                .purchase('RR6900002', [(1, sc.CODE, 100.0, 'หลอด', 20.0)],
                          datetime.date(2026, 5, 1)))

    result = sc.run_zip(monkeypatch, agreeing)

    assert result['sales']['unit_corrections_retired'] == 1
    assert result['sales']['unchanged'] == 1 and result['sales']['overwritten'] == 0
    correction, = sc.corrections(path)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_agrees')
    assert [r['id'] for r in sc.offsets(path, pid)] == [offset_id]
    assert sc.sales_row(path, LINE)['id'] == row_id
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -2
    assert sc.stock(path, pid) == 176
    assert len(sc.alerts(path, KIND)) == 1


def test_express_removing_the_line_retires_the_correction(empty_db, monkeypatch):
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.seed_product(empty_db, 'OTHER', ratios=(), name='อื่น')
    sc.run_zip(monkeypatch, _book((2.0, 'โหล', 49.0)))
    _apply(empty_db)
    assert sc.stock(empty_db, pid) == 176

    result = sc.run_zip(monkeypatch, _book(None))

    assert result['sales']['removed'] == 1
    assert result['sales']['unit_corrections_retired'] == 1
    assert result['sales']['unit_correction_docs'] == ['IV6900001']
    correction, = sc.corrections(empty_db)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_removed')
    assert sc.sales_row(empty_db, LINE) is None
    assert sc.offsets(empty_db, pid) == []
    assert sc.sale_ledger(empty_db, LINE, pid) == []
    assert sc.stock(empty_db, pid) == 200
    assert len(sc.alerts(empty_db, KIND)) == 1


def test_a_corrected_line_deleted_behind_the_importer_is_retired_and_reinserted(
        corrected, monkeypatch):
    path, pid, cid = corrected
    c = sc.raw(path)
    c.execute("DELETE FROM sales_transactions WHERE doc_no=?", (LINE,))
    c.execute("DELETE FROM transactions WHERE reference_no=?", (LINE,))
    c.commit()
    c.close()

    result = sc.run_zip(monkeypatch, sc.standard_book())

    assert result['sales']['imported'] == 1
    correction, = sc.corrections(path)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_removed')
    assert sc.sales_row(path, LINE)['unit'] == 'โหล'
    assert sc.offsets(path, pid) == []
    assert sc.stock(path, pid) == 176


def test_retiring_rebuilds_the_ledger_even_when_the_stored_row_already_equals_express(
        corrected, monkeypatch):
    path, pid, cid = corrected
    c = sc.raw(path)
    c.execute("UPDATE sales_transactions SET qty=3, net=147, unit='โหล',"
              " change_source='import', change_actor='hand', change_token='hand-1'"
              " WHERE doc_no=?", (LINE,))
    c.commit()
    c.close()
    changed = sc.standard_book()
    changed.stcrd[1]['TRNQTY'] = 3.0
    changed.stcrd[1]['TRNVAL'] = changed.stcrd[1]['NETVAL'] = 147.0

    result = sc.run_zip(monkeypatch, changed)

    assert result['sales']['overwritten'] == 0
    assert sc.corrections(path)[0]['end_cause'] == 'express_changed'
    assert sc.offsets(path, pid) == []
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -36
    assert sc.stock(path, pid) == 164


@pytest.mark.parametrize('column,value', [('unit', 'โหล'), ('product_id', None)],
                         ids=['unit', 'product'])
def test_a_stored_row_that_lost_its_correction_is_not_kept(
        corrected, monkeypatch, column, value):
    path, pid, cid = corrected
    other = sc.seed_product(path, 'OTHER', name='อื่น')
    c = sc.raw(path)
    c.execute(f"UPDATE sales_transactions SET {column}=?, change_source='import',"
              " change_actor='hand', change_token='hand-1' WHERE doc_no=?",
              (value if column == 'unit' else other, LINE))
    c.commit()
    c.close()

    result = sc.run_zip(monkeypatch, sc.standard_book())

    assert result['sales']['unit_corrections_retired'] == 1
    correction, = sc.corrections(path)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_changed')
    assert sc.offsets(path, pid) == []
    row = sc.sales_row(path, LINE)
    assert (row['unit'], row['product_id']) == ('โหล', pid)
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -24
    assert sc.stock(path, pid) == 176
    assert sc.stock(path, other) == 0


def test_a_retired_line_can_be_corrected_again(corrected, monkeypatch):
    path, pid, cid = corrected
    changed = sc.standard_book()
    changed.stcrd[1]['TRNQTY'] = 3.0
    changed.stcrd[1]['TRNVAL'] = changed.stcrd[1]['NETVAL'] = 147.0
    sc.run_zip(monkeypatch, changed)
    assert sc.corrections(path)[0]['status'] == 'retired'

    second = _apply(path, 'move')

    assert [(r['id'], r['status']) for r in sc.corrections(path)] == \
        [(cid, 'retired'), (second, 'active')]
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -3


class _Spy:
    """The importer's connection, with a hook fired just before its first
    write (the import_log INSERT)."""

    def __init__(self, real, before_first_write):
        self._real = real
        self._hook = before_first_write

    def execute(self, sql, *args):
        if self._hook and sql.lstrip().upper().startswith('INSERT INTO IMPORT_LOG'):
            hook, self._hook = self._hook, None
            hook(self._real)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_correction_committed_before_the_importers_first_write_is_honoured(
        empty_db, monkeypatch):
    from models import imports
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.standard_book())
    seen = []

    def correct_on_a_second_connection(importer_conn):
        seen.append(importer_conn.in_transaction)
        _apply(empty_db)

    real_get_connection = imports.get_connection
    hooks = [correct_on_a_second_connection]
    monkeypatch.setattr(
        imports, 'get_connection',
        lambda: _Spy(real_get_connection(), hooks.pop() if hooks else None))

    result = sc.run_zip(monkeypatch, sc.standard_book())

    assert seen == [False]
    assert result['sales']['overwritten'] == 0
    assert sc.sales_row(empty_db, LINE)['unit'] == 'หลอด'
    assert sc.corrections(empty_db)[0]['status'] == 'active'
    assert sc.stock(empty_db, pid) == 176


def test_respelling_express_code_in_the_unit_map_keeps_the_correction(
        empty_db, monkeypatch):
    import bsn_units
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db, ratios=(('ZZ', 12),))
    book = sc.standard_book()
    book.stcrd[1]['TQUCOD'] = 'ZZ'
    sc.run_zip(monkeypatch, book)
    assert sc.sales_row(empty_db, LINE)['unit'] == 'ZZ'
    _apply(empty_db)
    bsn_units.learn('ZZ', bsn_units.DEFAULT_BOOK, 'โหล')

    result = sc.run_zip(monkeypatch, book)

    assert result['sales']['unit_corrections_retired'] == 0
    assert result['sales']['overwritten'] == 0
    assert sc.corrections(empty_db)[0]['status'] == 'active'
    assert sc.sales_row(empty_db, LINE)['unit'] == 'หลอด'
    assert sc.stock(empty_db, pid) == 176


def test_respelling_the_corrected_word_in_the_unit_map_keeps_the_correction(
        corrected, monkeypatch):
    import bsn_units
    path, pid, cid = corrected
    bsn_units.learn('หลอด', bsn_units.DEFAULT_BOOK, 'หลอดยาว')

    result = sc.run_zip(monkeypatch, sc.standard_book())

    assert result['sales']['unit_corrections_retired'] == 0
    assert result['sales']['overwritten'] == 0
    assert sc.corrections(path)[0]['status'] == 'active'
    assert sc.sales_row(path, LINE)['unit'] == 'หลอด'
    assert sc.stock(path, pid) == 176


def test_the_vat_book_import_ignores_corrections(corrected):
    import bsn_units
    import models
    from express_dbf_source import build_sales_entries
    path, pid, cid = corrected
    tables = sc.standard_book().tables()
    entries = build_sales_entries(tables['ARTRN'], tables['STCRD'], tables['ARMAS'])

    default_book = models.import_weekly(entries, 'sales', 'f', apply_removals=True)
    assert default_book['unchanged'] == 1 and default_book['overwritten'] == 0

    entries = build_sales_entries(tables['ARTRN'], tables['STCRD'], tables['ARMAS'])
    xp5 = models.import_weekly(entries, 'sales', 'f', apply_removals=True,
                               book=bsn_units.BOOK_XP5)

    assert xp5['overwritten'] == 1
    assert xp5['unit_corrections_retired'] == 0
    assert sc.sales_row(path, LINE)['unit'] == 'โหล'
    assert sc.corrections(path)[0]['status'] == 'active'

    # Two books into ONE database happens only here (the VAT book is built in
    # its own file), and it leaves a correction whose line no longer holds it.
    # The main book's next zip ends that state instead of keeping it.
    entries = build_sales_entries(tables['ARTRN'], tables['STCRD'], tables['ARMAS'])
    healed = models.import_weekly(entries, 'sales', 'f', apply_removals=True)

    assert healed['unit_corrections_retired'] == 1
    assert sc.corrections(path)[0]['end_cause'] == 'express_changed'
    assert sc.offsets(path, pid) == []
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -24
    assert sc.stock(path, pid) == 176
