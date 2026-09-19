"""#590 PR 2, design §7.3: a cost row written during a request names who, how
and where — `user`, `change_source` (mig-173 vocabulary: manual / import) and
`change_reason` (`ui:<endpoint>` plus what ran).

U2 and U9 are driven as real HTTP requests through the test client. The
importer, the repoint and the conversion run are driven inside a REAL request
context for their route (the app's own before/teardown hooks run), because a
full end-to-end POST of those routes needs a dataset upload or a preview
session the test would have to forge. U3 is in test_wacc_actor.py, U11 in
test_590_master_upload_audit.py, U12-U14 in test_590_staged_swap.py. U1, U6 and
U7 create products: a products INSERT is the stated ceiling (design §5).
"""
import json
import sqlite3
from contextlib import contextmanager

import pytest

import actor
import database
import models

SETUP = dict(kind='script', who='setup', source='manual', detail='test setup')


@pytest.fixture
def db(tmp_db):
    database.init_db()
    return tmp_db


def _client(username='admin', role='admin'):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(user_id=1, username=username, role=role)
    return c


@contextmanager
def in_request(path, username='admin'):
    """The app's own hooks around real code, for the route at `path`."""
    import flask
    from app import app as flask_app
    with flask_app.test_request_context(path, method='POST'):
        flask.session.update(user_id=1, username=username, role='admin')
        flask_app.preprocess_request()
        yield


def _rows(db, after, table=None):
    conn = sqlite3.connect(db)
    sql = ("SELECT table_name, row_id, changed_fields, user, change_source, change_reason"
           " FROM audit_log WHERE id > ? AND user IS NOT NULL")
    params = [after]
    if table:
        sql += ' AND table_name = ?'
        params.append(table)
    return conn.execute(sql + ' ORDER BY id', params).fetchall()


def _mark(db):
    return sqlite3.connect(db).execute('SELECT max(id) FROM audit_log').fetchone()[0]


def _seed_product(db, *, unit='ตัว', cost=0.0, code=None):
    with actor.acting_as(**SETUP):
        c = database.get_connection()
        pid = c.execute("INSERT INTO products (product_name, unit_type, cost_price, opening_cost)"
                        " VALUES (?, ?, ?, ?)", (f'ทดสอบ590-{code}', unit, cost, cost)).lastrowid
        c.execute("INSERT OR IGNORE INTO stock_levels (product_id, quantity) VALUES (?, 0)", (pid,))
        if code:
            c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id)"
                      " VALUES (?, 'n', ?)", (code, pid))
        c.commit()
        c.close()
    return pid


def _purchase(doc, code, qty=10, unit='ตัว', price=10.0):
    return {'date_iso': '2026-04-24', 'doc_no': doc, 'line_seq': 1, 'qty': qty, 'unit': unit,
            'unit_price': price, 'vat_type': 0, 'discount': '', 'total': qty * price,
            'net': qty * price, 'product_name_raw': 'n', 'product_code_raw': code,
            'party': 'ร้านทดสอบ', 'party_code': 'pc'}


def test_u2_a_cost_edit_names_the_editor_and_the_page(db):
    pid = _seed_product(db, cost=33.0)
    mark = _mark(db)
    resp = _client().post(f'/products/{pid}/edit', data={
        'unit_type': 'ตัว', 'units_per_carton': '1', 'units_per_box': '1',
        'cost_price': '48.5', 'base_sell_price': '99', 'low_stock_threshold': '10'})
    assert resp.status_code == 302
    rows = _rows(db, mark, 'products')
    assert rows, 'control: the edit wrote no signed cost row at all'
    edit = [r for r in rows if r[5] == 'ui:products.product_edit']
    assert len(edit) == 1
    assert json.loads(edit[0][2]) == {'cost_price': [33.0, 48.5], 'opening_cost': [33.0, 48.5]}
    assert edit[0][3:5] == ('admin', 'manual')
    events = _rows(db, mark, 'product_cost_ledger')
    assert [e[5] for e in events] == ['ui:products.product_edit > wacc:recalculate']


def test_u4_an_import_names_the_uploader_the_file_and_the_engine(db):
    pid = _seed_product(db, code='Z590U4')
    mark = _mark(db)
    with in_request('/import-data/confirm'):
        models.import_weekly([_purchase('RR590U4', 'Z590U4')], 'purchase', 'u4.csv')
    rows = _rows(db, mark)
    reasons = {(r[0], r[5]) for r in rows if r[1] == pid}
    assert ('product_cost_ledger',
            'ui:bsn.unified_import_confirm > import:u4.csv > wacc:import') in reasons
    assert ('products', 'ui:bsn.unified_import_confirm > import:u4.csv > wacc:import') in reasons
    assert {(r[3], r[4]) for r in rows if r[1] == pid} == {('admin', 'import')}


def test_u5_the_dbf_upload_import_is_attributed_the_same_way(db):
    pid = _seed_product(db, code='Z590U5')
    mark = _mark(db)
    with in_request('/import-express-dbf/upload'):
        models.import_weekly([_purchase('RR590U5', 'Z590U5')], 'purchase', 'express_dbf:BSN5657')
    reasons = {r[5] for r in _rows(db, mark, 'product_cost_ledger') if r[1] == pid}
    assert reasons == {'ui:bsn.express_dbf_upload > import:express_dbf:BSN5657 > wacc:import'}


def test_u8_a_repoint_names_the_person_and_the_engine(db):
    a = _seed_product(db, code='Z590U8')
    b = _seed_product(db)
    with actor.acting_as(**SETUP):
        models.import_weekly([_purchase('RR590U8', 'Z590U8')], 'purchase', 'setup.csv')
    mark = _mark(db)
    with in_request('/mapping/split-save'):
        models.repoint_bsn_code(None, 'Z590U8', b)
    events = {r[1]: r for r in _rows(db, mark, 'product_cost_ledger')}
    assert set(events) >= {a, b}
    assert {events[p][3:] for p in (a, b)} == {
        ('admin', 'manual', 'ui:bsn.mapping_split_save > wacc:repoint')}


def test_u9_a_ratio_change_names_the_person_and_the_replay(db):
    pid = _seed_product(db, code='Z590U9')
    with actor.acting_as(**SETUP):
        c = database.get_connection()
        c.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, 'โหล', 12)",
                  (pid,))
        c.commit()
        c.close()
        models.import_weekly([_purchase('RR590U9', 'Z590U9', qty=2, unit='โหล', price=120.0)],
                             'purchase', 'setup.csv')
    mark = _mark(db)
    resp = _client().post('/unit-conversions/edit',
                          data={'product_id': str(pid), 'bsn_unit': 'โหล', 'ratio': '10'})
    assert resp.status_code == 302
    events = [r for r in _rows(db, mark, 'product_cost_ledger') if r[1] == pid]
    assert len(events) == 1
    assert events[0][3:] == ('admin', 'manual', 'ui:bsn.unit_conversions_edit > wacc:ratio_replay')


def test_u10_a_conversion_run_names_the_person_on_the_log_and_the_rebuilds(db):
    row = sqlite3.connect(db).execute(
        "SELECT cf.id FROM conversion_formulas cf JOIN conversion_formula_inputs i"
        " ON i.formula_id = cf.id JOIN stock_levels s ON s.product_id = i.product_id"
        " JOIN products p ON p.id = i.product_id WHERE cf.is_active = 1"
        " AND s.quantity >= i.quantity AND p.cost_price > 0 ORDER BY cf.id LIMIT 1").fetchone()
    assert row, 'the live-DB copy has no runnable formula; the test would prove nothing'
    fid = row[0]
    mark = _mark(db)
    log_before = sqlite3.connect(db).execute('SELECT max(id) FROM conversion_cost_log').fetchone()[0]
    with in_request(f'/conversions/{fid}/run'):
        ok, message, _ = models.run_conversion(fid, 1, run_token='t590')
    assert ok, message
    stamp = sqlite3.connect(db).execute(
        'SELECT written_by FROM conversion_cost_log WHERE id > ?', (log_before,)).fetchall()
    assert len(stamp) == 1
    assert json.loads(stamp[0][0]) == {'who': 'admin', 'source': 'manual',
                                       'reason': 'ui:inventory.conversion_run'}
    reasons = {r[5] for r in _rows(db, mark, 'product_cost_ledger')}
    assert reasons == {'ui:inventory.conversion_run > wacc:conversion_recalc'}
