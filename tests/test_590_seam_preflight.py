"""#590 PR 2, design §A2: every seam refuses BEFORE its first write.

A trigger's RAISE(ABORT) backs out only its own statement, and these seams
commit documents and stock before they reach the cost step. So the refusal
that matters is the Python preflight at the seam's entry: no document, stock,
mapping or cost row may land, and the refusal must leave a durable alert.

Each case pairs the unsigned refusal with a CONTROL where the same call gets
past the preflight once someone is declared (it may then fail for its own
reasons, e.g. a formula that does not exist; that is the point: it got past).
"""
import json
import sqlite3

import pytest

import actor
import database
import models

PUT = dict(kind='script', who='put', source='manual', detail='t.py: why')


@pytest.fixture
def db(tmp_db):
    database.init_db()
    actor.set_fallback(None)       # nobody declared unless a test says so
    return tmp_db


def _count(db, sql, params=()):
    return sqlite3.connect(db).execute(sql, params).fetchone()[0]


def _alerts(db, operation):
    rows = sqlite3.connect(db).execute(
        "SELECT context_json FROM system_alerts WHERE kind = 'actor_missing' AND resolved_at IS NULL"
    ).fetchall()
    return [json.loads(r[0]) for r in rows if json.loads(r[0])['operation'] == operation]


def _entry(doc):
    return {"date_iso": "2026-05-09", "doc_no": doc, "product_code_raw": "Z590X",
            "product_name_raw": "P", "party": "S", "party_code": "S1", "qty": 1.0,
            "unit": "ตัว", "unit_price": 10.0, "vat_type": 0, "discount": 0,
            "total": 10.0, "net": 10.0}


def test_import_weekly_refuses_before_logging_the_batch(db):
    logs = _count(db, 'SELECT count(*) FROM import_log')
    with pytest.raises(actor.ActorMissing):
        models.import_weekly([_entry('RR590-1')], 'purchase', 'unsigned.csv')
    assert _count(db, 'SELECT count(*) FROM import_log') == logs
    assert _count(db, "SELECT count(*) FROM purchase_transactions WHERE doc_no = 'RR590-1'") == 0
    assert len(_alerts(db, 'import:unsigned.csv')) == 1
    with actor.acting_as(**PUT):                                             # control
        models.import_weekly([_entry('RR590-2')], 'purchase', 'signed.csv')
    assert _count(db, 'SELECT count(*) FROM import_log') == logs + 1


def test_commit_express_dbf_refuses_before_reading_a_single_table(db, tmp_path):
    import import_router
    with pytest.raises(actor.ActorMissing):
        import_router.commit_express_dbf(str(tmp_path / 'no-such-dataset'))
    assert len(_alerts(db, 'import:express_dbf')) == 1
    with actor.acting_as(**PUT):                                             # control
        with pytest.raises(Exception) as got:
            import_router.commit_express_dbf(str(tmp_path / 'no-such-dataset'))
    assert not isinstance(got.value, actor.ActorMissing)


def test_run_conversion_refuses_before_moving_stock(db):
    txns = _count(db, 'SELECT count(*) FROM transactions')
    with pytest.raises(actor.ActorMissing):
        models.run_conversion(999999, 1)
    assert _count(db, 'SELECT count(*) FROM transactions') == txns
    assert len(_alerts(db, 'conversion')) == 1
    with actor.acting_as(**PUT):                                             # control
        ok, message, _ = models.run_conversion(999999, 1)
    assert ok is False and 'ไม่พบสูตร' in message


def _a_unit_conversion(db):
    row = sqlite3.connect(db).execute(
        'SELECT product_id, bsn_unit, ratio FROM unit_conversions ORDER BY id LIMIT 1').fetchone()
    assert row, 'the live-DB copy has no unit conversion; the test would prove nothing'
    return row


def test_a_ratio_change_refuses_before_rewriting_the_ratio(db):
    pid, unit, ratio = _a_unit_conversion(db)
    with pytest.raises(actor.ActorMissing):
        models.update_unit_conversion_ratio(pid, unit, ratio + 1)
    assert _count(db, 'SELECT ratio FROM unit_conversions WHERE product_id = ? AND bsn_unit = ?',
                  (pid, unit)) == ratio
    assert len(_alerts(db, 'ratio_change')) == 1


def test_a_repoint_refuses_before_moving_the_mapping(db):
    code, pid = sqlite3.connect(db).execute(
        'SELECT bsn_code, product_id FROM product_code_mapping WHERE product_id IS NOT NULL'
        ' ORDER BY id LIMIT 1').fetchone()
    other = _count(db, 'SELECT min(id) FROM products WHERE id <> ?', (pid,))
    with pytest.raises(actor.ActorMissing):
        models.repoint_bsn_code(None, code, other)
    assert _count(db, 'SELECT product_id FROM product_code_mapping WHERE bsn_code = ?'
                      ' ORDER BY id LIMIT 1', (code,)) == pid
    assert len(_alerts(db, 'repoint')) == 1
    conn = database.get_connection()                     # a caller's connection: caller alerts
    try:
        with pytest.raises(actor.ActorMissing):
            models.repoint_bsn_code(conn, code, other)
    finally:
        conn.close()


def test_a_cost_edit_is_refused_but_a_non_cost_edit_is_not(db):
    pid, cost, base = sqlite3.connect(db).execute(
        'SELECT id, cost_price, base_sell_price FROM products WHERE is_active = 1 ORDER BY id LIMIT 1'
    ).fetchone()
    with pytest.raises(actor.ActorMissing):
        models.update_product(pid, {'cost_price': cost + 1, 'opening_cost': cost + 1})
    assert _count(db, 'SELECT cost_price FROM products WHERE id = ?', (pid,)) == cost
    assert len(_alerts(db, 'cost_edit')) == 1
    models.update_product(pid, {'base_sell_price': base + 1})     # not a cost write
    assert _count(db, 'SELECT base_sell_price FROM products WHERE id = ?', (pid,)) == base + 1
    with actor.acting_as(**PUT):                                             # control
        models.update_product(pid, {'cost_price': cost + 1})
    assert _count(db, 'SELECT cost_price FROM products WHERE id = ?', (pid,)) == cost + 1
