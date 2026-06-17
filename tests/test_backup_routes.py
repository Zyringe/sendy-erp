"""Route tests for the auto-backup / restore admin UI (app.py)."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import io
import sqlite3

import pytest

import config
import db_backup


def _client(role, db_routes=False):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = role
        sess['role'] = role
        if db_routes:
            sess['db_routes_enabled'] = True
    return c


def _marker_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM import_log WHERE filename='MARKER'").fetchone()[0]
    finally:
        conn.close()


def _add_marker(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO import_log (filename, rows_imported, rows_skipped) "
                 "VALUES ('MARKER', 0, 0)")
    conn.commit()
    conn.close()


def test_backups_list_admin_ok(tmp_db):
    resp = _client('admin').get('/admin/backups')
    assert resp.status_code == 200
    assert 'สำรอง'.encode() in resp.data


def test_backups_list_forbidden_for_non_admin(tmp_db):
    assert _client('staff').get('/admin/backups').status_code == 403
    assert _client('manager').get('/admin/backups').status_code == 403


def test_restore_round_trip(tmp_db):
    bdir = db_backup.default_backup_dir(tmp_db)
    snap = db_backup.create_backup('unified', db_path=tmp_db, backup_dir=bdir)
    _add_marker(tmp_db)                       # simulate a bad import after the snapshot
    assert _marker_count(tmp_db) == 1

    c = _client('admin', db_routes=True)
    resp = c.post('/admin/backups/restore',
                  data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert _marker_count(tmp_db) == 0         # rolled back

    # the pre-restore safety snapshot (with the marker) exists
    reasons = [b['reason'] for b in db_backup.list_backups(backup_dir=bdir)]
    assert 'pre-restore' in reasons


def test_restore_blocked_without_db_routes_enabled(tmp_db):
    bdir = db_backup.default_backup_dir(tmp_db)
    snap = db_backup.create_backup('unified', db_path=tmp_db, backup_dir=bdir)
    _add_marker(tmp_db)

    c = _client('admin', db_routes=False)     # toggle OFF
    resp = c.post('/admin/backups/restore',
                  data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert _marker_count(tmp_db) == 1         # NOT restored


def test_restore_rejects_unknown_name(tmp_db):
    c = _client('admin', db_routes=True)
    resp = c.post('/admin/backups/restore',
                  data={'name': 'auto-unified-20990101_000000.db.gz', 'confirm': 'yes'},
                  follow_redirects=False)
    assert resp.status_code == 302            # flashed + redirected, no 500


# ── force a worker reload after restore (so both gunicorn -w 2 workers drop
#    any pre-restore connections) ───────────────────────────────────────────

def test_restore_signals_gunicorn_reload(tmp_db, monkeypatch):
    """Under gunicorn, a successful restore must SIGHUP the master so ALL workers
    gracefully reload and stop serving the pre-restore DB."""
    import signal as _signal
    bdir = db_backup.default_backup_dir(tmp_db)
    snap = db_backup.create_backup('unified', db_path=tmp_db, backup_dir=bdir)
    _add_marker(tmp_db)

    calls = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(os, 'getppid', lambda: 4242)

    c = _client('admin', db_routes=True)
    resp = c.post('/admin/backups/restore',
                  data={'name': snap['name'], 'confirm': 'yes'},
                  environ_overrides={'SERVER_SOFTWARE': 'gunicorn/21.2.0'})
    assert resp.status_code == 302
    assert _marker_count(tmp_db) == 0                 # restore happened
    assert calls == [(4242, _signal.SIGHUP)]          # graceful all-worker reload


def test_restore_no_reload_signal_off_gunicorn(tmp_db, monkeypatch):
    """Off gunicorn (Flask dev server / tests) there is no master to signal —
    a single process picks up the restored file on its next per-request
    connection, so we must NOT send a stray signal."""
    bdir = db_backup.default_backup_dir(tmp_db)
    snap = db_backup.create_backup('unified', db_path=tmp_db, backup_dir=bdir)
    _add_marker(tmp_db)

    calls = []
    monkeypatch.setattr(os, 'kill', lambda pid, sig: calls.append((pid, sig)))

    c = _client('admin', db_routes=True)
    resp = c.post('/admin/backups/restore',
                  data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert _marker_count(tmp_db) == 0                 # restore still happened
    assert calls == []                                # no stray signal


# ── Decision B: staff may import everything, and EVERY staff-reachable import
#    must snapshot the DB first so a wrong import is recoverable ─────────────
#
# Imports are consolidated into the unified box (/import-data); the retired
# /import-payments and /import-credit-notes/* routes' snapshot-before-write
# guarantee now lives in /import-data/confirm (_snapshot_before_import('unified')).

def test_staff_import_via_unified_box_snapshots_first(tmp_db, tmp_path, monkeypatch):
    """Staff import via the unified box, and /import-data/confirm must take a
    rollback snapshot BEFORE the ledger write (Decision B)."""
    import models as _models
    from app import app as flask_app
    monkeypatch.setitem(flask_app.config, 'UPLOAD_FOLDER', str(tmp_path))
    bdir = db_backup.default_backup_dir(tmp_db)

    seen = {}

    def fake_import(path):
        # the snapshot must already exist by the time the commit runs
        seen['reasons'] = [b['reason'] for b in db_backup.list_backups(backup_dir=bdir)]
        return {'imported': 1, 'updated': 0, 'skipped': 0}

    monkeypatch.setattr(_models, 'import_payments', fake_import)

    payments = (
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                หน้า   :        1"\n'
        '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"\n'
        '"วันที่จาก   1 ม.ค. 2567  ถึง  31 ธ.ค. 2569"\n'
        '">>>> จบรายงาน <<<<"\n'
    ).encode('cp874')

    c = _client('staff')
    resp = c.post('/import-data',
                  data={'files': (io.BytesIO(payments), 'การรับชำระหนี้_x.csv')},
                  content_type='multipart/form-data')
    assert resp.status_code == 200, 'staff must reach the unified preview'
    with c.session_transaction() as sess:
        token = sess['import_stage']['token']

    resp2 = c.post('/import-data/confirm', data={'token': token, 'type_0': 'payments_in'})
    assert resp2.status_code == 200
    assert 'unified' in seen.get('reasons', []), \
        'a rollback snapshot must be taken before the import write'
