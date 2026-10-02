"""#680: the full-replace /admin/upload-db data-loss gate must compare every
app-written table.

`_diff_db_row_counts` only looks at `_UPLOAD_DIFF_TABLES`. Before #680 that
list missed cashbook, salary advances and payroll (and most HR, purchasing,
marketplace and CRM tables), so an older local DB swapped in with no
confirmation and silently erased them.

The sweep below classifies every table in `data/schema.sql` (kept equal to the
live schema by `test_schema_sql_in_sync_with_live`): diffed, or exempt with a
written reason. A new table fails it until someone decides which.
"""
import io
import os
import re
import sqlite3

import pytest

import actor
from blueprints import admin

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), '..', 'data', 'schema.sql')

SIX = ('cashbook_transactions', 'cashbook_accounts', 'cashbook_categories',
       'salary_advances', 'payroll_runs', 'payroll_items')


def _schema_tables():
    with open(SCHEMA_SQL, encoding='utf-8') as f:
        sql = f.read()
    return set(re.findall(r'^CREATE TABLE (?:IF NOT EXISTS )?"?(\w+)"?', sql, re.M))


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'admin'
        sess['role'] = 'admin'
        sess['db_routes_enabled'] = True
    return c


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def _upload_missing_one_row(tmp_db, tmp_path, table):
    """A copy of the current DB with the newest row of `table` deleted."""
    upl = str(tmp_path / 'older_local.db')
    src, dst = sqlite3.connect(tmp_db), sqlite3.connect(upl)
    src.backup(dst)
    src.close()
    dst.close()
    c = actor.install(sqlite3.connect(upl))
    with actor.acting_as(kind='script', who='test', source='pytest', detail='680'):
        c.execute(f'DELETE FROM {table} WHERE rowid = (SELECT MAX(rowid) FROM {table})')
        c.commit()
    c.close()
    with open(upl, 'rb') as f:
        return f.read()


@pytest.mark.parametrize('table', SIX)
def test_fewer_rows_in_upload_lands_on_confirmation(tmp_db, tmp_path, table):
    # The first `import app` migrates tmp_db: copy the upload only after it,
    # or migration-seeded tables (unit_map) differ too.
    c = _client()
    before = _count(tmp_db, table)
    assert before >= 1, f'precondition: current DB needs a {table} row to lose'
    upload = _upload_missing_one_row(tmp_db, tmp_path, table)

    resp = c.post('/admin/upload-db',
                  data={'db_file': (io.BytesIO(upload), 'inventory.db'), 'mode': 'full'},
                  content_type='multipart/form-data')

    assert resp.status_code == 200, 'expected the confirmation page, got a swap'
    html = resp.get_data(as_text=True)
    assert f'<code>{table}</code>' in html
    assert '<code>brands</code>' not in html, 'unchanged tables stay off the page'
    with c.session_transaction() as sess:
        assert sess.get('pending_upload_path')
    assert _count(tmp_db, table) == before, 'current DB must be untouched'
    warned = {d['table'] for d in admin._diff_db_row_counts(
        tmp_db, sess['pending_upload_path']) if d['warning']}
    assert warned == {table}


def test_every_schema_table_is_diffed_or_exempt_with_a_reason():
    tables = _schema_tables()
    assert len(tables) > 100 and 'cashbook_transactions' in tables  # parse control

    diffed = set(admin._UPLOAD_DIFF_TABLES)
    exempt = admin._UPLOAD_DIFF_EXEMPT
    assert len(diffed) == len(admin._UPLOAD_DIFF_TABLES), 'duplicate diff entry'
    assert not diffed & set(exempt), 'a table is both diffed and exempt'
    assert all(isinstance(r, str) and r.strip() for r in exempt.values()), \
        'every exemption needs a written reason'
    assert sorted(tables - diffed - set(exempt)) == [], \
        'unclassified table: add it to _UPLOAD_DIFF_TABLES or _UPLOAD_DIFF_EXEMPT'
    assert sorted((diffed | set(exempt)) - tables) == [], 'stale entry: table not in schema'


def test_six_app_written_tables_are_diffed():
    assert set(SIX) <= set(admin._UPLOAD_DIFF_TABLES)
