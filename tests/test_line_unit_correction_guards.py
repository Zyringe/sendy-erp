"""แก้หน่วยบรรทัด (#692): writers that would strand an active correction refuse.

Each test runs the writer twice on the same data: refused while the correction
is active (nothing written), accepted once it is cancelled. The second call is
what shows the correction, and nothing else in the fixture, caused the refusal.
"""
import json
import os
import sys

import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'
REFUSAL = 'ยกเลิกการแก้หน่วยบรรทัดก่อน (IV6900001-1)'


@pytest.fixture
def guarded(empty_db, monkeypatch):
    """(db path, corrected product, a second product, cancel())."""
    import line_unit_correction as luc
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    other = sc.seed_product(empty_db, 'OTHER', name='อื่น')
    book = sc.standard_book()
    book.add_line('IV6900001', 2, sc.CODE, 1.0, 'แพ็ค', 30.0)
    sc.run_zip(monkeypatch, book)
    c = sc.conn(empty_db)
    cid = luc.apply(c, LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    c.close()

    def cancel():
        c = sc.conn(empty_db)
        luc.cancel(c, cid, 'ยกเลิกเพื่อทดสอบ guard', 'put')
        c.close()
    return empty_db, pid, other, cancel


def _one(path, sql, *params):
    c = sc.raw(path)
    try:
        return c.execute(sql, params).fetchone()[0]
    finally:
        c.close()


def test_repoint_bsn_code_refuses(guarded):
    import line_unit_correction as luc
    import models
    path, pid, other, cancel = guarded
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        models.repoint_bsn_code(None, sc.CODE, other)

    assert str(exc.value) == REFUSAL
    assert sc.written_state(path) == before
    assert _one(path, "SELECT product_id FROM product_code_mapping WHERE bsn_code=?",
                sc.CODE) == pid

    cancel()
    c = sc.raw(path)
    c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio)"
              " VALUES (?, 'แพ็ค', 1)", (other,))
    c.commit()
    c.close()
    models.repoint_bsn_code(None, sc.CODE, other)
    assert _one(path, "SELECT product_id FROM product_code_mapping WHERE bsn_code=?",
                sc.CODE) == other


def test_repoint_refuses_for_a_correction_on_the_destination_product(guarded):
    import line_unit_correction as luc
    import models
    path, pid, other, cancel = guarded

    with pytest.raises(luc.Refused) as exc:
        models.repoint_bsn_code(None, 'OTHER', pid)

    assert str(exc.value) == REFUSAL
    assert _one(path, "SELECT product_id FROM product_code_mapping WHERE bsn_code='OTHER'") \
        == other


def test_update_unit_conversion_ratio_refuses(guarded):
    import models
    path, pid, other, cancel = guarded
    before = sc.written_state(path)

    result = models.update_unit_conversion_ratio(pid, 'โหล', 10)

    assert result == {'error': REFUSAL}
    assert sc.written_state(path) == before
    assert _one(path, "SELECT ratio FROM unit_conversions WHERE product_id=?"
                      " AND bsn_unit='โหล'", pid) == 12

    cancel()
    assert models.update_unit_conversion_ratio(pid, 'โหล', 10) == {'ok': True}
    assert _one(path, "SELECT ratio FROM unit_conversions WHERE product_id=?"
                      " AND bsn_unit='โหล'", pid) == 10


def test_dismiss_pending_unit_conversion_refuses(guarded):
    import line_unit_correction as luc
    import models
    path, pid, other, cancel = guarded
    pending = "SELECT COUNT(*) FROM sales_transactions WHERE product_id=? AND unit='แพ็ค'"
    assert _one(path, pending, pid) == 1
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        models.dismiss_pending_unit_conversion(pid, 'แพ็ค', actor='put')

    assert str(exc.value) == REFUSAL
    assert sc.written_state(path) == before

    cancel()
    assert models.dismiss_pending_unit_conversion(pid, 'แพ็ค', actor='put') == 1
    assert _one(path, pending, pid) == 0


def test_apply_reconcile_flag_refuses(guarded):
    import models
    from models import reconcile
    path, pid, other, cancel = guarded
    c = sc.conn(path)
    payload = json.dumps(
        {'rows': reconcile._payload_for_doc(c, 'IV6900001'), 'evidence': {}},
        sort_keys=True)
    flag_id = c.execute(
        "INSERT INTO express_reconcile_flags (doc_base, class, first_payload_json,"
        " latest_payload_json) VALUES ('IV6900001', 'deleted', ?, ?)",
        (payload, payload)).lastrowid
    c.commit()
    c.close()
    before = sc.written_state(path)

    result = models.apply_reconcile_flag(flag_id, 'put')

    assert result == {'ok': False, 'error': REFUSAL}
    assert sc.written_state(path) == before
    assert _one(path, "SELECT state FROM express_reconcile_flags WHERE id=?", flag_id) \
        == 'open'

    cancel()
    c = sc.raw(path)
    payload = json.dumps(
        {'rows': reconcile._payload_for_doc(c, 'IV6900001'), 'evidence': {}},
        sort_keys=True)
    c.execute("UPDATE express_reconcile_flags SET latest_payload_json=? WHERE id=?",
              (payload, flag_id))
    c.commit()
    c.close()
    assert models.apply_reconcile_flag(flag_id, 'put') == {'ok': True}
    assert sc.sales_row(path, LINE) is None


@pytest.mark.parametrize('direction', ['from', 'to'])
def test_merge_product_refuses(guarded, direction, capsys):
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'scripts')
    if scripts not in sys.path:
        sys.path.append(scripts)
    import merge_product
    path, pid, other, cancel = guarded
    src, dst = (pid, other) if direction == 'from' else (other, pid)
    argv = ['--operator', 'pytest', '--reason', 'guard test', '--from', str(src),
            '--to', str(dst), '--db', path, '--apply']
    before = sc.written_state(path)

    assert merge_product.main(argv) == 2

    assert REFUSAL in capsys.readouterr().err
    assert sc.written_state(path) == before
    assert _one(path, "SELECT is_active FROM products WHERE id=?", src) == 1

    cancel()
    assert merge_product.main(argv) == 0
    assert _one(path, "SELECT is_active FROM products WHERE id=?", src) == 0


def _ratio(path, pid, unit):
    c = sc.raw(path)
    try:
        row = c.execute("SELECT ratio FROM unit_conversions WHERE product_id=?"
                        " AND bsn_unit=?", (pid, unit)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def test_save_unit_conversions_refuses_a_changed_ratio_and_allows_a_new_unit(guarded):
    import models
    path, pid, other, cancel = guarded

    result = models.save_unit_conversions([
        {'product_id': pid, 'bsn_unit': 'โหล', 'ratio': 10},
        {'product_id': pid, 'bsn_unit': 'โหล', 'ratio': 12},
        {'product_id': pid, 'bsn_unit': 'ลัง', 'ratio': 24},
    ])

    assert result['refused'] == [REFUSAL]
    assert result['saved'] == 2
    assert (_ratio(path, pid, 'โหล'), _ratio(path, pid, 'ลัง')) == (12, 24)
    assert sc.sale_ledger(path, LINE, pid)[0][1] == -2

    cancel()
    result = models.save_unit_conversions(
        [{'product_id': pid, 'bsn_unit': 'โหล', 'ratio': 10}])
    assert (result['saved'], result['refused']) == (1, [])
    assert _ratio(path, pid, 'โหล') == 10


def test_upsert_unit_conversion_refuses_a_changed_ratio_and_allows_a_new_unit(guarded):
    import line_unit_correction as luc
    import models
    path, pid, other, cancel = guarded

    with pytest.raises(luc.Refused) as exc:
        models.upsert_unit_conversion(pid, 'โหล', 10)

    assert str(exc.value) == REFUSAL
    assert _ratio(path, pid, 'โหล') == 12
    assert models.upsert_unit_conversion(pid, 'โหล', 12) is True
    assert models.upsert_unit_conversion(pid, 'ลัง', 24) is True
    assert _ratio(path, pid, 'ลัง') == 24

    cancel()
    assert models.upsert_unit_conversion(pid, 'โหล', 10) is True
    assert _ratio(path, pid, 'โหล') == 10


def test_update_product_refuses_a_base_unit_change(guarded):
    import line_unit_correction as luc
    import models
    path, pid, other, cancel = guarded

    def unit_type():
        return _one(path, "SELECT unit_type FROM products WHERE id=?", pid)

    with pytest.raises(luc.Refused) as exc:
        models.update_product(pid, {'unit_type': 'โหล', 'product_name': 'ใหม่'})

    assert str(exc.value) == REFUSAL
    assert unit_type() == 'หลอด'
    assert _one(path, "SELECT product_name FROM products WHERE id=?", pid) != 'ใหม่'
    models.update_product(pid, {'unit_type': 'หลอด', 'product_name': 'ใหม่'})
    assert _one(path, "SELECT product_name FROM products WHERE id=?", pid) == 'ใหม่'

    cancel()
    models.update_product(pid, {'unit_type': 'โหล'})
    assert unit_type() == 'โหล'


def test_cancel_refuses_when_a_hold_would_move_stock(guarded):
    import line_unit_correction as luc
    path, pid, other, cancel = guarded
    c = sc.raw(path)
    c.execute("UPDATE unit_conversions SET ratio=10 WHERE product_id=? AND bsn_unit='โหล'",
              (pid,))
    c.commit()
    c.close()
    before = sc.written_state(path)

    with pytest.raises(luc.Refused) as exc:
        cancel()

    assert exc.value.code == 'stock_moved'
    assert sc.written_state(path) == before
    assert sc.corrections(path)[0]['status'] == 'active'


def test_apply_refuses_when_a_hold_would_move_stock(empty_db, monkeypatch):
    import line_unit_correction as luc
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.standard_book())
    before = sc.written_state(empty_db)
    readings = iter(range(100, 200))
    monkeypatch.setattr(luc, '_stock', lambda conn, product_id: next(readings))

    c = sc.conn(empty_db)
    with pytest.raises(luc.Refused) as exc:
        luc.apply(c, LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    c.close()

    assert exc.value.code == 'stock_moved'
    assert sc.written_state(empty_db) == before


def test_a_correction_cannot_slip_in_between_the_ratio_guard_and_its_write(
        empty_db, monkeypatch):
    import sqlite3
    import line_unit_correction as luc
    import models
    sc.seed_company(empty_db)
    pid = sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, sc.standard_book())
    real_blocking = luc.blocking
    attempts = []

    def blocking_then_a_rival_apply(conn, **kwargs):
        found = real_blocking(conn, **kwargs)
        rival = sc.conn(empty_db)
        rival.execute("PRAGMA busy_timeout = 100")
        try:
            luc.apply(rival, LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
            attempts.append('applied')
        except sqlite3.OperationalError as exc:
            attempts.append(str(exc))
        finally:
            rival.close()
        return found
    monkeypatch.setattr(luc, 'blocking', blocking_then_a_rival_apply)

    result = models.update_unit_conversion_ratio(pid, 'โหล', 10)

    assert attempts == ['database is locked']
    assert result == {'ok': True}
    assert sc.corrections(empty_db) == []
    assert sc.sale_ledger(empty_db, LINE, pid)[0][1] == -20
