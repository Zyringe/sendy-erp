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

def test_staff_can_import_payments_and_snapshots_first(tmp_db, tmp_path, monkeypatch):
    """Staff (not just manager/admin) can import payments via /import-payments,
    and a rollback snapshot is taken BEFORE the ledger write."""
    import models as _models
    monkeypatch.setattr(config, 'UPLOAD_FOLDER', str(tmp_path))
    bdir = db_backup.default_backup_dir(tmp_db)

    seen = {}

    def fake_import(path):
        # the snapshot must already exist by the time the import runs
        seen['reasons'] = [b['reason'] for b in db_backup.list_backups(backup_dir=bdir)]
        return {'imported': 1, 'updated': 0, 'skipped': 0}

    monkeypatch.setattr(_models, 'import_payments', fake_import)

    c = _client('staff')
    resp = c.post('/import-payments',
                  data={'payment_file': (io.BytesIO(b'date,amount\n'), 'pay.csv')},
                  content_type='multipart/form-data')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/payment-status')   # staff allowed (not the dashboard bounce)
    assert 'payments' in seen.get('reasons', [])                  # snapshot taken before the import


def test_staff_can_commit_credit_notes_and_snapshots_first(tmp_db, tmp_path, monkeypatch):
    """Staff can commit a credit-note import, with a snapshot taken first."""
    import import_credit_notes as _cn
    monkeypatch.setattr(config, 'UPLOAD_FOLDER', str(tmp_path))
    bdir = db_backup.default_backup_dir(tmp_db)

    # stage a preview file the commit route will accept
    cn_dir = os.path.join(str(tmp_path), 'cn-preview')
    os.makedirs(cn_dir, exist_ok=True)
    token = 'deadbeef.csv'
    with open(os.path.join(cn_dir, token), 'w') as fh:
        fh.write('doc_no,amount\n')

    seen = {}

    def fake_commit(path):
        seen['reasons'] = [b['reason'] for b in db_backup.list_backups(backup_dir=bdir)]
        return {'refs_backfilled': 0, 'credit_note_amounts': {'upserted': 0},
                'new_recorded': 0, 'already_new': 0, 'skipped': 0}

    monkeypatch.setattr(_cn, 'import_credit_notes', fake_commit)

    c = _client('staff')
    resp = c.post('/import-credit-notes/commit', data={'token': token})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/payment-status')   # staff allowed
    assert 'credit-notes' in seen.get('reasons', [])              # snapshot taken before the import


def test_staff_can_open_credit_notes_preview(tmp_db, tmp_path, monkeypatch):
    """Staff can run the credit-note preview (no perm bounce to dashboard).

    The stub parser raises so the route hits its own error path → redirect to
    payment_status. A permission-blocked staff would instead be bounced to the
    dashboard by before_request, so the destination distinguishes the two."""
    import import_credit_notes as _cn
    monkeypatch.setattr(config, 'UPLOAD_FOLDER', str(tmp_path))

    def boom(path):
        raise ValueError('stub parse error')

    monkeypatch.setattr(_cn, 'preview_credit_notes_import', boom)
    c = _client('staff')
    resp = c.post('/import-credit-notes/preview',
                  data={'cn_file': (io.BytesIO(b'doc_no,amount\n'), 'cn.csv')},
                  content_type='multipart/form-data')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/payment-status')   # not the dashboard perm-bounce
