"""#590 PR 2: the WACC engine names who set it off and what it ran.

Design §A2/§A3/§A5. The engine refuses before its first write when nobody is
declared, writes ONE recalc-event row per rebuild (the ledger itself carries no
actor), and runs inside a scope that adds `wacc:<operation>` to whoever is
already acting — so a page view that triggers a lazy rebuild is recorded as a
page view, never as someone typing a cost.
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
    database.init_db()          # the copy at this tree's migration level
    return tmp_db


def _pid(db, *, with_purchases):
    sql = ("SELECT p.id FROM products p WHERE p.is_active = 1 AND p.opening_cost > 0"
           " AND {} EXISTS (SELECT 1 FROM purchase_transactions t WHERE t.product_id = p.id)"
           " AND EXISTS (SELECT 1 FROM product_cost_ledger l WHERE l.product_id = p.id)"
           " ORDER BY p.id LIMIT 1").format('' if with_purchases else 'NOT')
    row = sqlite3.connect(db).execute(sql).fetchone()
    assert row, 'the live-DB copy has no such product; the test would prove nothing'
    return row[0]


def _events(db, pid, after):
    conn = sqlite3.connect(db)
    return [(json.loads(f), u, s, r) for f, u, s, r in conn.execute(
        "SELECT changed_fields, user, change_source, change_reason FROM audit_log"
        " WHERE table_name = 'product_cost_ledger' AND row_id = ? AND id > ? ORDER BY id",
        (pid, after))]


def _max_audit(db):
    return sqlite3.connect(db).execute('SELECT COALESCE(max(id), 0) FROM audit_log').fetchone()[0]


def _ledger(db, pid):
    return sqlite3.connect(db).execute(
        'SELECT id, unit_cost, wacc_after FROM product_cost_ledger WHERE product_id = ? ORDER BY id',
        (pid,)).fetchall()


def test_a_rebuild_writes_one_event_row_naming_who_and_what(db):
    pid = _pid(db, with_purchases=True)
    rows_before = len(_ledger(db, pid))
    mark = _max_audit(db)
    with actor.acting_as(**PUT):
        models.recalculate_product_wacc(pid, operation='ratio_replay')
    ev = _events(db, pid, mark)
    assert len(ev) == 1
    fields, user, source, reason = ev[0]
    assert (user, source, reason) == ('put', 'manual', 'script:t.py: why > wacc:ratio_replay')
    assert fields['operation'] == 'ratio_replay'
    assert fields['ledger_rows'] == [rows_before, len(_ledger(db, pid))]


def test_a_cost_the_engine_moves_is_audited_with_the_engine_operation(db):
    pid = _pid(db, with_purchases=False)
    with actor.acting_as(**PUT):
        c = database.get_connection()
        c.execute('UPDATE products SET opening_cost = opening_cost + 1 WHERE id = ?', (pid,))
        c.commit()
        c.close()
        mark = _max_audit(db)
        models.recalculate_product_wacc(pid)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT changed_fields, user, change_reason FROM audit_log WHERE table_name = 'products'"
        " AND row_id = ? AND id > ?", (pid, mark)).fetchall()
    assert len(rows) == 1
    fields, user, reason = rows[0]
    assert set(json.loads(fields)) == {'cost_price'}
    assert (user, reason) == ('put', 'script:t.py: why > wacc:recalculate')


def test_an_unsigned_rebuild_is_refused_before_it_touches_anything(db):
    pid = _pid(db, with_purchases=True)
    ledger, mark = _ledger(db, pid), _max_audit(db)
    actor.set_fallback(None)
    with pytest.raises(actor.ActorMissing):
        models.recalculate_product_wacc(pid)
    assert _ledger(db, pid) == ledger
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT count(*) FROM audit_log WHERE id > ?', (mark,)).fetchone()[0] == 0
    alerts = conn.execute("SELECT context_json FROM system_alerts WHERE kind = 'actor_missing'"
                          " AND resolved_at IS NULL").fetchall()
    assert len(alerts) == 1
    assert json.loads(alerts[0][0])['product_id'] == pid


def test_a_callers_unsigned_connection_is_refused_before_the_delete(db):
    pid = _pid(db, with_purchases=True)
    ledger = _ledger(db, pid)
    actor.set_fallback(None)
    conn = database.get_connection()
    try:
        with pytest.raises(actor.ActorMissing):
            models.recalculate_product_wacc(pid, conn)
        assert [tuple(r) for r in conn.execute(
            'SELECT id, unit_cost, wacc_after FROM product_cost_ledger WHERE product_id = ?'
            ' ORDER BY id', (pid,))] == ledger
    finally:
        conn.rollback()
        conn.close()


def test_a_raw_connection_is_refused_before_the_delete(db):
    pid = _pid(db, with_purchases=True)
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    with pytest.raises(actor.ActorMissing):
        models.recalculate_product_wacc(pid, raw)
    assert raw.execute('SELECT count(*) FROM product_cost_ledger WHERE product_id = ?',
                       (pid,)).fetchone()[0] == len(_ledger(db, pid)) > 0


def test_a_page_view_that_rebuilds_is_recorded_as_a_view(db):
    """U3: GET /products/<id>/cost-history rebuilds a missing ledger lazily."""
    pid = _pid(db, with_purchases=True)
    with actor.acting_as(**PUT):
        c = database.get_connection()
        c.execute('DELETE FROM product_cost_ledger WHERE product_id = ?', (pid,))
        c.commit()
        c.close()
    mark = _max_audit(db)
    from app import app as flask_app
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=1, username='admin', role='admin')
    resp = client.get(f'/products/{pid}/cost-history')
    assert resp.status_code == 200
    assert resp.get_json()['history'], 'the lazy rebuild produced no ledger'
    ev = _events(db, pid, mark)
    assert len(ev) == 1
    assert ev[0][1:] == ('admin', 'manual', 'ui:products.product_cost_history > wacc:lazy_read')
