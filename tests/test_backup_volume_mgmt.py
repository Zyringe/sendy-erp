"""Tests for the volume-management additions: pending-stash sweep, manual
backup delete (route + pure helper), and path-traversal safety."""
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


def _touch(path, size=16):
    with open(path, 'wb') as f:
        f.write(b'x' * size)


# ── sweep_pending_uploads (pure) ─────────────────────────────────────────────

def test_sweep_pending_removes_only_pending(tmp_path):
    d = tmp_path / 'pending_uploads'
    d.mkdir()
    _touch(d / 'pending-20260101_000000.db')
    _touch(d / 'pending-20260102_120000.db')
    _touch(d / 'other.txt')                       # foreign — must NOT be touched
    _touch(d / 'inventory.db')                     # not a pending stash — must survive
    deleted, freed = db_backup.sweep_pending_uploads(str(d))
    assert set(deleted) == {'pending-20260101_000000.db', 'pending-20260102_120000.db'}
    assert freed == 32
    left = set(os.listdir(d))
    assert left == {'other.txt', 'inventory.db'}


def test_sweep_pending_keeps_named(tmp_path):
    d = tmp_path / 'pending_uploads'
    d.mkdir()
    _touch(d / 'pending-20260101_000000.db')
    keep = 'pending-20260102_120000.db'
    _touch(d / keep)
    deleted, _ = db_backup.sweep_pending_uploads(str(d), keep_name=keep)
    assert deleted == ['pending-20260101_000000.db']
    assert os.path.exists(d / keep)               # in-flight stash preserved


def test_sweep_pending_missing_dir_is_noop(tmp_path):
    assert db_backup.sweep_pending_uploads(str(tmp_path / 'nope')) == ([], 0)


# ── delete_backup (pure) ─────────────────────────────────────────────────────

def test_delete_backup_removes_snapshot(tmp_path):
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    name = 'auto-unified-20260101_120000.db.gz'
    _touch(bdir / name, size=1024)
    freed = db_backup.delete_backup(name, backup_dir=str(bdir))
    assert freed == 1024
    assert not os.path.exists(bdir / name)


@pytest.mark.parametrize('bad', [
    '../inventory.db',                 # path escape
    'inventory.db',                    # not a snapshot
    'auto-unified-20260101_120000.db', # not gzipped (wrong suffix)
    'pending-20260101_000000.db',      # a stash, not a backup
    '',
])
def test_delete_backup_rejects_bad_names(tmp_path, bad):
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    # a real file the escape attempt could hit, to prove it is NOT removed
    _touch(tmp_path / 'inventory.db')
    with pytest.raises(ValueError):
        db_backup.delete_backup(bad, backup_dir=str(bdir))
    assert os.path.exists(tmp_path / 'inventory.db')


def test_delete_backup_missing_raises(tmp_path):
    bdir = tmp_path / 'backups'
    bdir.mkdir()
    with pytest.raises(ValueError):
        db_backup.delete_backup('auto-unified-20990101_000000.db.gz', backup_dir=str(bdir))


# ── /admin/backups/delete route ──────────────────────────────────────────────

def _make_backup(tmp_db):
    bdir = db_backup.default_backup_dir(tmp_db)
    return db_backup.create_backup('unified', db_path=tmp_db, backup_dir=bdir), bdir


def test_delete_route_removes_backup(tmp_db):
    snap, bdir = _make_backup(tmp_db)
    assert os.path.exists(snap['path'])
    resp = _client('admin', db_routes=True).post(
        '/admin/backups/delete', data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert not os.path.exists(snap['path'])


def test_delete_route_blocked_without_db_routes(tmp_db):
    snap, _ = _make_backup(tmp_db)
    resp = _client('admin', db_routes=False).post(
        '/admin/backups/delete', data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert os.path.exists(snap['path'])           # NOT deleted


def test_delete_route_requires_confirm(tmp_db):
    snap, _ = _make_backup(tmp_db)
    resp = _client('admin', db_routes=True).post(
        '/admin/backups/delete', data={'name': snap['name']})
    assert resp.status_code == 302
    assert os.path.exists(snap['path'])           # NOT deleted


def test_delete_route_forbidden_non_admin(tmp_db):
    snap, _ = _make_backup(tmp_db)
    for role in ('staff', 'manager'):
        resp = _client(role, db_routes=True).post(
            '/admin/backups/delete', data={'name': snap['name'], 'confirm': 'yes'})
        # blocked either by the before_request POST whitelist (302 redirect) or
        # the route's own admin check (403) — never executed.
        assert resp.status_code in (302, 403)
        assert os.path.exists(snap['path'])       # the real invariant: not deleted


def test_backups_page_renders_delete_button(tmp_db):
    """With db_routes on, the row renders the delete <form> — exercises
    url_for('backup_delete') in the real template (catches a BuildError/typo)."""
    _make_backup(tmp_db)
    resp = _client('admin', db_routes=True).get('/admin/backups')
    assert resp.status_code == 200
    assert b'/admin/backups/delete' in resp.data
    assert 'ลบ'.encode() in resp.data


def test_upload_db_page_shows_disk_bar(tmp_db):
    resp = _client('admin', db_routes=True).get('/admin/upload-db')
    assert resp.status_code == 200
    assert 'พื้นที่ดิสก์'.encode() in resp.data


def test_delete_route_path_traversal_is_safe(tmp_db):
    """A path-escaping name must flash + redirect (no 500) and never delete the
    live DB."""
    assert os.path.exists(tmp_db)
    resp = _client('admin', db_routes=True).post(
        '/admin/backups/delete',
        data={'name': '../inventory.db', 'confirm': 'yes'})
    assert resp.status_code == 302                # graceful, not a crash
    assert os.path.exists(tmp_db)                 # live DB untouched
