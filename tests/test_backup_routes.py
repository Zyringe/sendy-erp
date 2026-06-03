"""Route tests for the auto-backup / restore admin UI (app.py)."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

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
