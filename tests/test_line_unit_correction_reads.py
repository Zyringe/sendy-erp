"""แก้หน่วยบรรทัด (#692): the read-only functions the page is built from.

The line under test is IV6900001-1, 2 โหล of a product whose base unit is
หลอด (โหล = 12, กล่อง = 100).
"""
import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'


@pytest.fixture
def line(empty_db, monkeypatch):
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db, ratios=(('โหล', 12), ('กล่อง', 100)))
    sc.run_zip(monkeypatch, sc.standard_book())
    return empty_db, pid


def _luc(path, fn, *args):
    import line_unit_correction as luc
    c = sc.conn(path)
    try:
        return getattr(luc, fn)(c, *args)
    finally:
        c.close()


def test_line_view_is_the_stored_line_with_the_units_it_may_become(line):
    path, pid = line

    view = _luc(path, 'line_view', LINE, sc.CODE)

    assert (view['doc_no'], view['bsn_code'], view['doc_base']) == (LINE, sc.CODE, 'IV6900001')
    assert (view['qty'], view['unit'], view['unit_price'], view['net']) == (2.0, 'โหล', 49.0, 98.0)
    assert view['product_id'] == pid
    assert view['display_name'] == 'ใบมีดคัตเตอร์'
    assert view['customer'] == 'ลูกค้าทดสอบ'
    assert view['base_unit'] == 'หลอด'
    # โหล is the line's own unit: offering it would only earn a refusal.
    assert sorted(view['unit_choices']) == sorted(['หลอด', 'กล่อง'])


def test_line_view_offers_the_old_unit_again_once_the_line_is_corrected(line):
    path, _pid = line
    _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')

    view = _luc(path, 'line_view', LINE, sc.CODE)

    assert view['unit'] == 'หลอด'
    assert sorted(view['unit_choices']) == sorted(['โหล', 'กล่อง'])


def test_line_view_of_a_line_that_does_not_exist_is_none(line):
    path, _pid = line
    assert _luc(path, 'line_view', 'IV6900001-9', sc.CODE) is None
    assert _luc(path, 'line_view', LINE, 'no-such-code') is None


def test_line_view_of_an_unmapped_line_has_no_units_to_offer(line):
    path, _pid = line
    c = sc.raw(path)
    c.execute("INSERT INTO sales_transactions (date_iso, doc_no, doc_base, bsn_code,"
              " product_name_raw, qty, unit, unit_price, net)"
              " VALUES ('2026-04-01', 'IV6900001-2', 'IV6900001', 'XX', 'ของไม่รู้จัก',"
              " 1, 'ตัว', 5, 5)")
    c.commit()
    c.close()

    view = _luc(path, 'line_view', 'IV6900001-2', 'XX')

    assert view['product_id'] is None
    assert view['display_name'] == 'ของไม่รู้จัก'
    assert view['unit_choices'] == []


def test_corrections_for_line_lists_this_lines_corrections_newest_first(line):
    path, _pid = line
    first = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    _luc(path, 'cancel', first, 'ยกเลิกเพราะแก้ผิดบรรทัด', 'put')
    second = _luc(path, 'apply', LINE, sc.CODE, 'กล่อง', 'move', sc.REASON, 'put')
    # Another line's correction must not leak in.
    c = sc.raw(path)
    c.execute("INSERT INTO sales_line_unit_corrections (doc_no, bsn_code, doc_base,"
              " product_id, express_unit_raw, express_unit, qty, corrected_unit,"
              " stock_mode, reason, created_by) VALUES ('IV6900009-1', ?, 'IV6900009',"
              " 1, 'โหล', 'โหล', 1, 'หลอด', 'move', ?, 'put')", (sc.CODE, sc.REASON))
    c.commit()
    c.close()
    assert len(sc.corrections(path)) == 3

    rows = _luc(path, 'corrections_for_line', LINE, sc.CODE)

    assert [(r['id'], r['status'], r['corrected_unit']) for r in rows] == [
        (second, 'active', 'กล่อง'), (first, 'cancelled', 'หลอด')]


def test_correction_returns_one_row_by_id_or_none(line):
    path, _pid = line
    cid = _luc(path, 'apply', LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')

    assert _luc(path, 'correction', cid)['doc_no'] == LINE
    assert _luc(path, 'correction', cid + 1) is None


@pytest.mark.parametrize('doc, qty', [('IV6900001', 2.0), ('SR6900001', 2.0)])
def test_hold_offset_is_the_offset_apply_then_posts(empty_db, monkeypatch, doc, qty):
    """For a sale and for a return: the number the confirm screen prints is
    the ADJUST row a hold correction writes."""
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.Book().sale(doc, [(1, sc.CODE, qty, 'โหล', 49.0)]))
    line_no = doc + '-1'

    effect = _luc(empty_db, 'preview', line_no, sc.CODE, 'หลอด')
    _luc(empty_db, 'apply', line_no, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')

    offset, = sc.offsets(empty_db, pid)
    assert offset['quantity_change'] != 0
    assert effect.hold_offset == offset['quantity_change']
