"""/import-express-dbf must leave a rollback point before it mutates money.

The daily Express zip is the most destructive routine write in the app:
models/payments.py DELETEs receipt->invoice links the file no longer lists,
scan_reconcile flags vanished documents, and both outstanding snapshots replace
their whole (entity, date). /import-data/confirm has taken a full-DB snapshot
before its ledger writes since day one; this route never did, so a bad zip had
no way back.

TIMING IS THE DESIGN CONSTRAINT, not a detail. Prod runs `gunicorn --timeout
60` and this route already measured 36.8s on 2026-08-18 (21s three days
earlier -- Phase 0's AR/AP snapshots added the jump). Measured on prod
2026-08-19: `sqlite .backup` of the 144.5MB DB costs 0.16s and gzip level 9
costs 7.34s, so the snapshot is essentially all compression. Level 6 costs
2.18s for 20.1MB instead of 19.6MB -- 5.2s of the 23s headroom bought back for
0.5MB. Hence the compresslevel change in db_backup, and hence the tests below
that pin the backup to the paths which actually import (a refused upload must
not pay for a snapshot it will never use).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import io
import zipfile

import pytest

import blueprints.bsn as bsn
import db_backup


def _client(role='staff'):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'u'
        s['role'] = role
    return c


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries:
            zf.writestr(name, data)
    buf.seek(0)
    return buf


def _upload(client, entries, **form):
    data = {'file': (_zip_bytes(entries), 'daily.zip')}
    data.update(form)
    return client.post('/import-express-dbf/upload', data=data,
                       content_type='multipart/form-data',
                       follow_redirects=False)


def _fake_per_type(n=0):
    return ({t: {'imported': n} for t in
             ('sales', 'purchase', 'payments_in', 'payments_out',
              'ar_snapshot', 'ap_snapshot')}
            | {'credit_notes_ar': {'upserted': n},
               'credit_notes_ap': {'imported': n},
               'snapshot_date': '2026-08-17'})


@pytest.fixture
def trace(monkeypatch):
    """Record backup and import calls IN ORDER on one shared list.

    Ordering is the point: a snapshot taken after commit_express_dbf is not a
    rollback point, it is a copy of the damage. Asserting only "a backup
    happened" cannot tell those apart.
    """
    calls = []
    monkeypatch.setattr(
        bsn.db_backup, 'safe_create_backup',
        lambda reason, **kw: (calls.append(('backup', reason)), ({'name': 'x'}, None))[1])
    monkeypatch.setattr(
        bsn.import_router, 'commit_express_dbf',
        lambda *a, **k: (calls.append(('import', None)), _fake_per_type())[1])
    return calls


def test_backup_is_taken_before_the_bsn_import(tmp_db, monkeypatch, trace):
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    # Control first: the import actually ran, so this is not a vacuous pass on
    # a route that bailed early.
    assert ('import', None) in trace, trace
    assert [c[0] for c in trace] == ['backup', 'import'], trace
    assert trace[0][1] == 'express_dbf'


def test_refused_upload_takes_no_backup(tmp_db, monkeypatch, trace):
    """An unknown book rejects the whole file -- nothing is mutated, so nothing
    needs a rollback point and the operator must not wait ~2s for one."""
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: None)
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert trace == []


def test_stale_zip_refused_without_force_takes_no_backup(tmp_db, monkeypatch,
                                                         trace):
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn, '_claim_export_date',
                        lambda *a, **k: (False, '2030-01-01'))
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert trace == []


def test_vat_only_upload_takes_no_backup(tmp_db, monkeypatch, trace):
    """The VAT half rebuilds vat_book.db in a detached subprocess and never
    touches the main DB, so the main DB needs no rollback point for it."""
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'vat')
    monkeypatch.setattr(bsn, '_spawn_vat_rebuild', lambda *a, **k: None)
    r = _upload(_client(), [('xp5/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert trace == []


def test_backup_failure_warns_but_does_not_block_the_import(tmp_db, monkeypatch):
    """A backup-infra failure (disk full) must not cost the team its import --
    same contract _snapshot_before_import already gives /import-data."""
    imported = []
    monkeypatch.setattr(bsn.db_backup, 'safe_create_backup',
                        lambda reason, **kw: (None, 'ดิสก์เต็ม'))
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: (imported.append(1), _fake_per_type())[1])
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert imported == [1]


# -- the compression level that makes the above affordable -------------------

def test_backup_default_compresslevel_is_the_measured_one():
    assert db_backup.DEFAULT_COMPRESSLEVEL == 6


def test_backup_honours_the_compresslevel(tmp_path):
    """Pins that the level actually reaches gzip. Level 0 stores uncompressed,
    so a same-content backup is dramatically larger -- an assertion that cannot
    pass if the parameter is ignored."""
    import sqlite3
    src = tmp_path / 'src.db'
    conn = sqlite3.connect(str(src))
    conn.execute('CREATE TABLE t (v TEXT)')
    conn.executemany('INSERT INTO t VALUES (?)', [('compressible ' * 40,)] * 4000)
    conn.commit()
    conn.close()

    lo = db_backup.create_backup('lvl0', db_path=str(src),
                                 backup_dir=str(tmp_path / 'a'), compresslevel=0)
    hi = db_backup.create_backup('lvl9', db_path=str(src),
                                 backup_dir=str(tmp_path / 'b'), compresslevel=9)
    assert lo['size'] > hi['size'] * 5, (lo['size'], hi['size'])
