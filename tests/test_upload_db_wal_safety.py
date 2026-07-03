"""Tests for the WAL-safe pre-swap backup + sidecar handling on the legacy
full-replace /admin/upload-db flow.

Why: a bare `shutil.copy` backup of a WAL-mode live DB can capture a torn
snapshot (missing pages still sitting in -wal), and a raw `shutil.move` swap
leaves stale -wal/-shm sidecars behind that a sibling gunicorn worker could
pair with the new main file. See erp-engineering-discipline.md.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import gzip
import io
import sqlite3
import tempfile

import db_backup


def _client(role, db_routes=True):
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


def _touch(path, data=b'stale wal frames'):
    with open(path, 'wb') as f:
        f.write(data)


def _snapshot_bytes(db_path):
    """A WAL-safe standalone copy of db_path, as raw bytes (for use as the
    uploaded file's content)."""
    fd, tmp_copy = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        db_backup.snapshot_db(db_path, tmp_copy)
        with open(tmp_copy, 'rb') as f:
            return f.read()
    finally:
        os.remove(tmp_copy)


def _post_full_replace(upload_bytes):
    """POST an upload whose row counts exactly match the current DB (an
    identical snapshot), so the diff has zero warnings and the direct-swap
    branch runs (not the two-step confirm flow)."""
    c = _client('admin')
    return c.post('/admin/upload-db',
                  data={'db_file': (io.BytesIO(upload_bytes), 'inventory.db'),
                        'mode': 'full'},
                  content_type='multipart/form-data')


def test_full_replace_upload_clears_stale_sidecars(tmp_db):
    upload_bytes = _snapshot_bytes(tmp_db)
    _touch(tmp_db + '-wal')
    _touch(tmp_db + '-shm')

    resp = _post_full_replace(upload_bytes)
    assert resp.status_code == 302
    assert not os.path.exists(tmp_db + '-wal')
    assert not os.path.exists(tmp_db + '-shm')


def test_full_replace_upload_backup_is_a_valid_sqlite_db(tmp_db):
    upload_bytes = _snapshot_bytes(tmp_db)
    resp = _post_full_replace(upload_bytes)
    assert resp.status_code == 302

    bdir = db_backup.default_backup_dir(tmp_db)
    backups = [b for b in db_backup.list_backups(backup_dir=bdir)
               if b['reason'] == 'pre-upload-full']
    assert len(backups) == 1, 'expected exactly one pre-upload-full snapshot'

    fd, decompressed = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        with gzip.open(backups[0]['path'], 'rb') as fi, open(decompressed, 'wb') as fo:
            fo.write(fi.read())
        conn = sqlite3.connect(decompressed)
        try:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0] > 0
        finally:
            conn.close()
    finally:
        os.remove(decompressed)


def test_master_only_upload_still_replaces_master_tables(tmp_db):
    """Regression guard: the selective master-tables path is SQL-level (ATTACH
    + INSERT), untouched by the full-replace WAL fix, and must keep working."""
    fd, tmp_copy = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        db_backup.snapshot_db(tmp_db, tmp_copy)
        conn = sqlite3.connect(tmp_copy)
        conn.execute("UPDATE brands SET name_th='__TEST_MARKER__' WHERE id="
                     "(SELECT id FROM brands LIMIT 1)")
        conn.commit()
        conn.close()
        with open(tmp_copy, 'rb') as f:
            upload_bytes = f.read()
    finally:
        os.remove(tmp_copy)

    c = _client('admin')
    resp = c.post('/admin/upload-db',
                  data={'db_file': (io.BytesIO(upload_bytes), 'inventory.db'),
                        'mode': 'master_only'},
                  content_type='multipart/form-data')
    assert resp.status_code == 302

    live = sqlite3.connect(tmp_db)
    try:
        assert live.execute(
            "SELECT COUNT(*) FROM brands WHERE name_th='__TEST_MARKER__'"
        ).fetchone()[0] == 1
    finally:
        live.close()
