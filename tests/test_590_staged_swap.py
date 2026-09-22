"""#590 PR 2, design §6 (C2): a file that replaces the live DB is migrated and
proven guarded BEFORE the swap, or the swap is refused.

Four routes replace the whole file (full upload, upload confirm, backup
restore, bootstrap upload). Under gunicorn --preload the SIGHUP that follows
does not re-run init_db, so without this an older file would go live
unmigrated and unguarded until a real restart.
"""
import io
import os
import shutil
import sqlite3

import pytest

import actor
import database
import db_backup

ADMIN = dict(kind='ui', who='admin', source='manual', detail='admin.upload_db')
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ROLLBACK_191 = os.path.join(REPO, 'data', 'migrations', '191_cost_actor_guards.rollback.sql')


@pytest.fixture
def live(tmp_db):
    database.init_db()
    return tmp_db


def _copy(src, dst):
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    a.backup(b)
    a.close()
    b.close()
    return dst


def _pre_590_file(live, tmp_path):
    """A file from before migration 191: rolled back AND un-stamped."""
    path = _copy(live, str(tmp_path / 'old.db'))
    c = actor.install(sqlite3.connect(path), actor.Actor(who='setup', kind='test'))
    with open(ROLLBACK_191, encoding='utf-8') as f:
        c.executescript(f.read())
    c.execute("DELETE FROM applied_migrations WHERE filename = '191_cost_actor_guards.sql'")
    c.commit()
    c.close()
    return path


def _stamped_but_unguarded(live, tmp_path):
    """applied_migrations says 191 ran, but a guard is missing — the file the
    behavioural probe exists for: its stamp alone would read as safe."""
    path = _copy(live, str(tmp_path / 'tampered.db'))
    c = sqlite3.connect(path)
    c.execute('DROP TRIGGER products_cost_needs_actor')
    c.commit()
    c.close()
    return path


def _refuses_unsigned_cost(path):
    c = actor.install(sqlite3.connect(path), None)
    actor.set_fallback(None)
    try:
        c.execute('UPDATE products SET cost_price = cost_price + 1 WHERE id = (SELECT min(id) FROM products)')
    except sqlite3.IntegrityError as e:
        return actor.REFUSAL_MARK in str(e)
    finally:
        c.rollback()
        c.close()
    return False


# ── prepare_staged_db itself ────────────────────────────────────────────────

def test_an_older_file_is_migrated_guarded_and_stamped(live, tmp_path):
    old = _pre_590_file(live, tmp_path)
    assert not _refuses_unsigned_cost(old), 'control: the old file really is unguarded'
    with actor.acting_as(**ADMIN):
        database.prepare_staged_db(old, 'full_upload')
    assert _refuses_unsigned_cost(old)
    stamp = sqlite3.connect(old).execute(
        "SELECT user, change_source, change_reason FROM audit_log"
        " WHERE table_name = 'database' AND row_key = 'full_upload'").fetchall()
    assert stamp == [('admin', 'manual', 'ui:admin.upload_db')]


def test_a_stamped_but_unguarded_file_is_refused(live, tmp_path):
    bad = _stamped_but_unguarded(live, tmp_path)
    with actor.acting_as(**ADMIN):
        with pytest.raises(database.StagedDbRefused, match='products'):
            database.prepare_staged_db(bad, 'full_upload')


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    junk = tmp_path / 'junk.db'
    junk.write_bytes(b'not a sqlite database at all' * 100)
    with actor.acting_as(**ADMIN):
        with pytest.raises(database.StagedDbRefused):
            database.prepare_staged_db(str(junk), 'full_upload')


def test_an_unsigned_swap_is_refused(live, tmp_path):
    good = _copy(live, str(tmp_path / 'good.db'))
    actor.set_fallback(None)
    with pytest.raises(database.StagedDbRefused):
        database.prepare_staged_db(good, 'full_upload')


# ── the four routes ─────────────────────────────────────────────────────────

def _marker(path):
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE IF NOT EXISTS _live_marker (x)')
    c.execute('INSERT INTO _live_marker VALUES (1)')
    c.commit()
    c.close()


def _marker_survived(path):
    return sqlite3.connect(path).execute(
        "SELECT count(*) FROM sqlite_master WHERE name = '_live_marker'").fetchone()[0] == 1


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(user_id=1, username='admin', role='admin', db_routes_enabled=True)
    return c


def _flashes(c):
    """The refusal branch is the only one that flashes these words. A test that
    asserts only 'the live DB survived' also passes when the route returns early
    for an unrelated reason — which is exactly how the first version of the full
    upload test went vacuous (it posted the wrong file field)."""
    with c.session_transaction() as s:
        return ' '.join(m for _, m in s.get('_flashes', []))


def test_the_full_upload_route_refuses_the_file(live, tmp_path):
    bad = _stamped_but_unguarded(live, tmp_path)
    _marker(live)
    c = _client()
    with open(bad, 'rb') as f:
        resp = c.post('/admin/upload-db', data={
            'mode': 'full', 'confirm': 'yes', 'db_file': (io.BytesIO(f.read()), 'x.db')},
            content_type='multipart/form-data')
    assert resp.status_code == 302
    assert 'StagedDbRefused' in _flashes(c) or 'ไม่อัปโหลด' in _flashes(c), _flashes(c)
    assert _marker_survived(live)


def test_the_full_upload_route_still_swaps_a_good_file(live, tmp_path):
    """Control: the same POST with a good file really replaces the live DB."""
    good = _pre_590_file(live, tmp_path)
    _marker(live)
    c = _client()
    with open(good, 'rb') as f:
        resp = c.post('/admin/upload-db', data={
            'mode': 'full', 'confirm': 'yes', 'db_file': (io.BytesIO(f.read()), 'x.db')},
            content_type='multipart/form-data')
    assert resp.status_code == 302
    assert not _marker_survived(live), _flashes(c)
    assert _refuses_unsigned_cost(live)


def test_the_upload_confirm_route_refuses_the_held_file(live, tmp_path):
    held = str(tmp_path / 'held.db')
    shutil.copy(_stamped_but_unguarded(live, tmp_path), held)
    _marker(live)
    c = _client()
    with c.session_transaction() as s:
        s['pending_upload_path'] = held
    resp = c.post('/admin/upload-db/confirm', data={'action': 'apply'})
    assert resp.status_code == 302
    assert 'ไม่อัปโหลด' in _flashes(c), _flashes(c)
    assert _marker_survived(live)


def test_the_restore_route_refuses_the_backup(live, tmp_path):
    bdir = db_backup.default_backup_dir(live)
    snap = db_backup.create_backup('unified', db_path=_stamped_but_unguarded(live, tmp_path),
                                   backup_dir=bdir)
    _marker(live)
    c = _client()
    resp = c.post('/admin/backups/restore', data={'name': snap['name'], 'confirm': 'yes'})
    assert resp.status_code == 302
    assert 'products' in _flashes(c) and 'กู้คืนไม่สำเร็จ' in _flashes(c), _flashes(c)
    assert _marker_survived(live)


def test_the_bootstrap_route_refuses_the_file(live, tmp_path, monkeypatch):
    monkeypatch.setenv('BOOTSTRAP_TOKEN', 'tok-590')
    bad = _stamped_but_unguarded(live, tmp_path)
    _marker(live)
    from app import app as flask_app
    with open(bad, 'rb') as f:
        resp = flask_app.test_client().post('/bootstrap/upload-db', data={
            'token': 'tok-590', 'db': (io.BytesIO(f.read()), 'x.db')},
            content_type='multipart/form-data')
    assert resp.status_code == 422
    assert _marker_survived(live)


def test_the_bootstrap_route_still_accepts_a_good_file(live, tmp_path, monkeypatch):
    """Control for the one route whose refusal is a status code."""
    monkeypatch.setenv('BOOTSTRAP_TOKEN', 'tok-590')
    good = _pre_590_file(live, tmp_path)
    from app import app as flask_app
    with open(good, 'rb') as f:
        resp = flask_app.test_client().post('/bootstrap/upload-db', data={
            'token': 'tok-590', 'db': (io.BytesIO(f.read()), 'x.db')},
            content_type='multipart/form-data')
    assert resp.status_code == 200
    assert _refuses_unsigned_cost(live)
    stamp = sqlite3.connect(live).execute(
        "SELECT user, change_reason FROM audit_log WHERE table_name = 'database'"
        " AND row_key = 'bootstrap'").fetchall()
    assert stamp == [('bootstrap-token', 'system:bootstrap_upload_db')]


# ── the VAT-book build runs as the person who uploaded ──────────────────────

def test_the_vat_build_is_declared_as_the_uploader(monkeypatch):
    """U5 → P2: the detached builder has no request, so the spawning request
    passes its actor down (--uploader) and build() declares it."""
    import import_router
    import vat_book_builder as vb
    seen = {}

    def _cap(*a, **k):
        seen['actor'] = actor.current()
        raise RuntimeError('stop after the declaration was observed')
    monkeypatch.setattr(vb, '_guard_subprocess_target', lambda: '/tmp/unused.db')
    monkeypatch.setattr(database, 'init_db', lambda *a, **k: None)
    monkeypatch.setattr(database, 'get_connection', lambda *a, **k: sqlite3.connect(':memory:'))
    import express_dbf_source as eds
    monkeypatch.setattr(eds, 'open_table', lambda *a, **k: [])
    monkeypatch.setattr(vb, 'seed_companies', lambda conn: None)
    monkeypatch.setattr(vb, '_use_main_unit_map', lambda *a, **k: None)
    monkeypatch.setattr(vb, 'seed_products_from_stmas', lambda conn, rows: {})
    monkeypatch.setattr(import_router, 'commit_express_dbf', _cap)
    with pytest.raises(RuntimeError, match='declaration was observed'):
        vb.build('/nonexistent', uploader='admin')
    got = seen['actor']
    assert (got.who, got.source, got.reason) == ('admin', 'import', 'system:vat-book-build')


def test_the_upload_route_passes_its_actor_to_the_builder(tmp_db, tmp_path, monkeypatch):
    from blueprints import bsn
    argv = {}
    dataset = tmp_path / 'dataset'
    dataset.mkdir()

    class _Proc:
        def __init__(self, args, **kw):
            argv['args'] = args

        def poll(self):
            return 0
    monkeypatch.setattr(bsn.subprocess, 'Popen', _Proc)
    with actor.acting_as(kind='ui', who='admin', source='manual', detail='bsn.express_dbf_upload'):
        bsn._spawn_vat_rebuild(str(dataset), 1, '2026-08-17')
    args = argv['args']
    assert args[args.index('--uploader') + 1] == 'admin'
