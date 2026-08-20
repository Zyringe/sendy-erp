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


def test_import_data_keeps_its_warn_and_continue_contract(tmp_db, monkeypatch):
    """The two import boxes deliberately differ, so pin it.

    /import-data is a preview/confirm flow the operator drives file by file and
    has warned-and-continued since day one; changing that is not this branch's
    business. /import-express-dbf refuses (see the fail-closed tests below),
    because it is a single destructive commit with no per-file preview."""
    import blueprints.bsn as _bsn
    from app import app as flask_app
    monkeypatch.setattr(_bsn.db_backup, 'safe_create_backup',
                        lambda reason, **kw: (None, 'ดิสก์เต็ม'))
    info, err = _bsn._snapshot_before_import('unified')
    assert info is None and err == 'ดิสก์เต็ม'

    # And the warning must still REACH the user -- moving the flash out of the
    # helper is only correct if the caller took it over. Drive the real confirm
    # route with an empty stage: the snapshot runs before any file work.
    # This route RENDERS (200) rather than redirecting, so the flash is
    # consumed into the body -- read it there, not from the session queue.
    # Unlike the express page, this phrase is not part of this page's chrome.
    client = _client()
    with client.session_transaction() as sess:
        sess['import_stage'] = {'token': 'tok1', 'rows': []}
    body = client.post('/import-data/confirm',
                       data={'token': 'tok1'}).get_data(as_text=True)
    assert 'ดิสก์เต็ม' in body, 'the backup error never reached the page'
    assert 'นำเข้าต่อโดยไม่มีจุดกู้คืน' in body


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


# -- fail closed: a promised rollback point must actually exist --------------

def test_backup_failure_refuses_the_import(tmp_db, monkeypatch, trace):
    """Codex round 7, P1. Best-effort is the wrong stance for the app's most
    destructive routine write: warning and then deleting receipt links anyway
    means the feature promises a rollback point it did not create. Disk-full is
    the realistic trigger, and it can break the import itself too."""
    monkeypatch.setattr(bsn.db_backup, 'safe_create_backup',
                        lambda reason, **kw: (None, 'ดิสก์เต็ม'))
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert ('import', None) not in trace, 'the import must not run'


def test_explicit_override_lets_the_import_proceed_without_a_backup(
        tmp_db, monkeypatch, trace):
    """The escape hatch has to exist -- a team that cannot import at all
    because the volume is full is worse off. Same shape as force_older on this
    route: refuse by default, proceed only on a deliberate tick."""
    monkeypatch.setattr(bsn.db_backup, 'safe_create_backup',
                        lambda reason, **kw: (None, 'ดิสก์เต็ม'))
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')],
                force_no_backup='1')
    assert r.status_code == 302
    assert ('import', None) in trace, 'the tick must let it through'


def test_a_working_backup_needs_no_override(tmp_db, monkeypatch, trace):
    """Control: the refusal is keyed on the backup FAILING, not on the tick
    being absent."""
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert [c[0] for c in trace] == ['backup', 'import'], trace


# -- retention must keep the Express rollback point ---------------------------

def test_newest_snapshot_per_reason_survives_pruning(tmp_path):
    """Codex round 7, P2. max_keep is global, so two marketplace uploads after
    the daily import could evict the only Express rollback point the same day
    -- deleting exactly what this branch exists to create."""
    import sqlite3
    src = tmp_path / 'src.db'
    sqlite3.connect(str(src)).execute('CREATE TABLE t (v)')
    bdir = tmp_path / 'b'

    # Explicit, increasing timestamps: the filename carries only whole seconds,
    # so four backups made in the same second sort unstably and this test would
    # pass by accident. (It did, before this line.)
    import datetime
    t0 = datetime.datetime(2026, 8, 19, 17, 0, 0)
    db_backup.create_backup('express_dbf', db_path=str(src),
                            backup_dir=str(bdir), now=t0)
    for i in range(1, 4):
        db_backup.create_backup('marketplace_upload', db_path=str(src),
                                backup_dir=str(bdir),
                                now=t0 + datetime.timedelta(minutes=i))

    reasons = [b['reason'] for b in db_backup.list_backups(backup_dir=str(bdir))]
    assert 'express_dbf' in reasons, reasons
    assert reasons.count('marketplace_upload') >= 1, reasons


def test_prune_removes_stale_part_files(tmp_path):
    """A hard kill mid-write leaves a .part that never matches _NAME_RE, so
    list_backups and prune could not see it and it sat on the volume forever."""
    import time
    bdir = tmp_path / 'b'
    bdir.mkdir()
    stale = bdir / 'auto-x-20260101_000000.db.gz.part'
    stale.write_bytes(b'x' * 1024)
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    fresh = bdir / 'auto-y-20260101_000001.db.gz.part'
    fresh.write_bytes(b'y')

    db_backup.prune_backups(backup_dir=str(bdir))
    assert not stale.exists(), 'a stale .part must be reclaimed'
    assert fresh.exists(), 'an in-flight .part must be left alone'


def test_the_override_checkbox_is_actually_on_the_page(tmp_db):
    """A fail-closed guard whose only escape hatch is invisible is a dead end
    for the team. Assert the ELEMENT, not a bare substring — the Thai wording
    appears in the flash text too, so a substring check could pass on a page
    that never rendered the input."""
    html = _client().get('/import-express-dbf').get_data(as_text=True)
    assert 'name="force_no_backup"' in html
    assert 'id="force_no_backup"' in html


def test_an_aged_out_snapshot_dies_even_if_it_is_newest_for_its_reason(tmp_path):
    """Codex round 8, P1 -- my bug.

    The per-reason floor `continue`d BEFORE the age check, so the newest
    snapshot of every reason lived forever. My own commit message claimed
    "keep_days still ages everything out"; it did not. With ~5 live reasons at
    ~20MB each on a 433MB volume that is 100MB pinned permanently, and
    DEFAULT_MAX_KEEP stops being the hard count cap its comment promises.

    Age must beat the floor: the floor exists to stop a same-day marketplace
    upload evicting the Express rollback point, not to make backups immortal.
    """
    import datetime
    import sqlite3
    src = tmp_path / 'src.db'
    sqlite3.connect(str(src)).execute('CREATE TABLE t (v)')
    bdir = tmp_path / 'b'

    old = datetime.datetime(2026, 6, 1, 12, 0, 0)          # ~80 days before
    now = datetime.datetime(2026, 8, 20, 12, 0, 0)
    db_backup.create_backup('express_dbf', db_path=str(src),
                            backup_dir=str(bdir), now=old)
    # Control BEFORE anything can prune it: create_backup prunes on every call,
    # so checking after the later ones would race the very behaviour under test.
    assert [b['reason'] for b in db_backup.list_backups(backup_dir=str(bdir))] \
        == ['express_dbf']

    db_backup.create_backup('unified', db_path=str(src),
                            backup_dir=str(bdir), now=now)
    db_backup.create_backup('marketplace_upload', db_path=str(src),
                            backup_dir=str(bdir), now=now)
    db_backup.prune_backups(backup_dir=str(bdir), now=now)

    reasons = [b['reason'] for b in db_backup.list_backups(backup_dir=str(bdir))]
    assert 'express_dbf' not in reasons, (
        'an 80-day-old snapshot must age out even as newest for its reason')
    assert reasons, 'pruning must not empty the directory'


def test_the_floor_still_protects_a_fresh_express_snapshot(tmp_path):
    """Control for the test above: the age rule must not undo P2's fix. Same
    setup, all three fresh."""
    import datetime
    import sqlite3
    src = tmp_path / 'src.db'
    sqlite3.connect(str(src)).execute('CREATE TABLE t (v)')
    bdir = tmp_path / 'b'
    t0 = datetime.datetime(2026, 8, 20, 12, 0, 0)
    db_backup.create_backup('express_dbf', db_path=str(src),
                            backup_dir=str(bdir), now=t0)
    for i in (1, 2, 3):
        db_backup.create_backup('marketplace_upload', db_path=str(src),
                                backup_dir=str(bdir),
                                now=t0 + datetime.timedelta(minutes=i))
    db_backup.prune_backups(backup_dir=str(bdir),
                            now=t0 + datetime.timedelta(minutes=4))
    reasons = [b['reason'] for b in db_backup.list_backups(backup_dir=str(bdir))]
    assert 'express_dbf' in reasons, reasons


def test_the_refusal_does_not_also_say_it_is_carrying_on(tmp_db, monkeypatch,
                                                         trace):
    """Codex round 8, P2. The helper used to flash "นำเข้าต่อโดยไม่มีจุดกู้คืน"
    unconditionally, so a refused upload showed the user BOTH that and
    "ยกเลิกทั้งไฟล์ ไม่มีการนำเข้าใดๆ" in one response. Follow the redirect and
    read the rendered flashes."""
    monkeypatch.setattr(bsn.db_backup, 'safe_create_backup',
                        lambda reason, **kw: (None, 'ดิสก์เต็ม'))
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    client = _client()
    client.post('/import-express-dbf/upload',
                data={'file': (_zip_bytes([('data/ARTRN.DBF', b'x')]),
                               'daily.zip')},
                content_type='multipart/form-data')

    # Read the FLASH QUEUE, not the rendered page: the phrase under test is
    # also the override checkbox's own label, so a body substring check would
    # match the page chrome and could never fail. (It did exactly that on the
    # first version of this test.)
    with client.session_transaction() as sess:
        msgs = [m for _cat, m in sess.get('_flashes', [])]
    assert msgs, 'no flash at all -- the test is not reaching the guard'
    assert any('ยกเลิกทั้งไฟล์' in m for m in msgs), msgs      # control: refused
    assert not any('— นำเข้าต่อโดยไม่มีจุดกู้คืน' in m for m in msgs), (
        'the refusal must not also flash that the import carried on: %s' % msgs)
    assert ('import', None) not in trace


def test_retention_is_bounded_regardless_of_how_many_reasons_exist(tmp_path):
    """Codex round 9, P1. A floor that protects the newest of EVERY reason
    grows with the number of reasons, and there are 10 of them
    (unified, express_dbf, marketplace, marketplace_settlement,
    marketplace_balance, marketplace_upload, pre-upload-full, pre-restore,
    master_naming_cascade, master_naming_edit).

    At ~20MB each that is ~160MB pinned on a 433MB volume with 166MB free --
    and because the free-space guard is a PRE-check, a run can legally start at
    65MB free, write 20MB, and finish at ~45MB, under MIN_FREE_BYTES.

    The requirement was never "keep one of everything". It was "a marketplace
    upload must not evict the Express rollback point". So the floor covers only
    express_dbf, and the total stays bounded at max_keep + 1.
    """
    import sqlite3
    src = tmp_path / 'src.db'
    sqlite3.connect(str(src)).execute('CREATE TABLE t (v)')
    bdir = tmp_path / 'b'

    import datetime
    t0 = datetime.datetime(2026, 8, 20, 9, 0, 0)
    reasons = ['express_dbf', 'unified', 'marketplace', 'marketplace_settlement',
               'marketplace_balance', 'marketplace_upload', 'pre-upload-full',
               'pre-restore', 'master_naming_cascade', 'master_naming_edit']
    for i, reason in enumerate(reasons):
        db_backup.create_backup(reason, db_path=str(src), backup_dir=str(bdir),
                                now=t0 + datetime.timedelta(minutes=i))

    kept = db_backup.list_backups(backup_dir=str(bdir))
    assert kept, 'pruning emptied the directory'        # control
    assert len(kept) <= db_backup.DEFAULT_MAX_KEEP + 1, (
        'retention must not grow with the number of reasons: %s'
        % [b['reason'] for b in kept])
    assert 'express_dbf' in [b['reason'] for b in kept], (
        'the Express rollback point is the one thing the floor exists for')
