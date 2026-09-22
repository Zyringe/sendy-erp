"""#590 PR 2, design C3: the master upload names who replaced the costs.

`_replace_master_tables` swaps `products` wholesale (DELETE + INSERT ... SELECT),
so no UPDATE trigger ever sees a cost move. It writes the signed audit rows
itself, inside its own transaction, and refuses an unsigned upload before the
DELETE.
"""
import json
import sqlite3

import pytest

import actor
import database
from blueprints import admin

ADMIN = dict(kind='ui', who='admin', source='manual', detail='admin.upload_db')


@pytest.fixture
def pair(tmp_db, tmp_path):
    """The current DB and an 'uploaded' copy where two products' costs moved."""
    database.init_db()
    upl = str(tmp_path / 'uploaded.db')
    src = sqlite3.connect(tmp_db)
    dst = sqlite3.connect(upl)
    src.backup(dst)
    src.close()
    dst.close()
    c = actor.install(sqlite3.connect(upl))
    pids = [r[0] for r in c.execute('SELECT id FROM products ORDER BY id LIMIT 2')]
    c.execute('UPDATE products SET cost_price = cost_price + 7 WHERE id = ?', (pids[0],))
    c.execute('UPDATE products SET opening_cost = opening_cost + 3 WHERE id = ?', (pids[1],))
    c.commit()
    c.close()
    return tmp_db, upl, pids


def _max_audit(path):
    return sqlite3.connect(path).execute('SELECT max(id) FROM audit_log').fetchone()[0]


def test_every_moved_cost_and_the_upload_itself_are_signed(pair):
    cur_db, upl, pids = pair
    mark = _max_audit(cur_db)
    with actor.acting_as(**ADMIN):
        admin._replace_master_tables(cur_db, upl)
    rows = sqlite3.connect(cur_db).execute(
        "SELECT row_id, row_key, changed_fields, user, change_source, change_reason FROM audit_log"
        " WHERE id > ? AND user IS NOT NULL ORDER BY id", (mark,)).fetchall()
    moved = {r[0]: json.loads(r[2]) for r in rows if r[1] is None}
    assert set(moved) == set(pids)
    assert moved[pids[0]]['cost_price'][1] == moved[pids[0]]['cost_price'][0] + 7
    summary = [r for r in rows if r[1] == 'master_upload']
    assert len(summary) == 1
    assert json.loads(summary[0][2])['products_cost_changed'] == 2
    assert {(r[3], r[4], r[5]) for r in rows} == {
        ('admin', 'manual', 'ui:admin.upload_db > master_upload')}


def test_an_unsigned_upload_is_refused_before_anything_is_replaced(pair):
    cur_db, upl, pids = pair
    before = sqlite3.connect(cur_db).execute(
        'SELECT cost_price FROM products WHERE id = ?', (pids[0],)).fetchone()[0]
    mark = _max_audit(cur_db)
    actor.set_fallback(None)
    with pytest.raises(actor.ActorMissing):
        admin._replace_master_tables(cur_db, upl)
    conn = sqlite3.connect(cur_db)
    assert conn.execute('SELECT cost_price FROM products WHERE id = ?', (pids[0],)).fetchone()[0] == before
    assert conn.execute('SELECT count(*) FROM audit_log WHERE id > ?', (mark,)).fetchone()[0] == 0


def test_a_rolled_back_upload_leaves_no_audit_row(pair, monkeypatch):
    cur_db, upl, _ = pair
    mark = _max_audit(cur_db)
    real = admin._audit_master_upload_costs

    def _then_fail(cur, replaced):
        real(cur, replaced)
        raise RuntimeError('simulated failure after the audit rows were written')
    monkeypatch.setattr(admin, '_audit_master_upload_costs', _then_fail)
    with actor.acting_as(**ADMIN):
        with pytest.raises(RuntimeError):
            admin._replace_master_tables(cur_db, upl)
    assert sqlite3.connect(cur_db).execute(
        'SELECT count(*) FROM audit_log WHERE id > ?', (mark,)).fetchone()[0] == 0
