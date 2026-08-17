"""/import-express-dbf multi-book upload (P3): zip preflight, dataset
discovery + ISINFO classification, per-dataset outcomes persisted, async VAT
rebuild spawn, and the reject-before-any-import contract."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import io
import json
import sqlite3
import zipfile

import pytest

import blueprints.bsn as bsn


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


# ── preflight ───────────────────────────────────────────────────────────────

def test_preflight_rejects_path_traversal():
    with zipfile.ZipFile(_zip_bytes([('../evil.txt', b'x')])) as zf:
        assert 'ไม่ปลอดภัย' in bsn._zip_preflight(zf)


def test_preflight_rejects_absolute_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zi = zipfile.ZipInfo('/abs.txt')
        zf.writestr(zi, b'x')
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        assert 'ไม่ปลอดภัย' in bsn._zip_preflight(zf)


def test_preflight_rejects_member_count(monkeypatch):
    monkeypatch.setattr(bsn, '_EXPRESS_DBF_MAX_MEMBERS', 2)
    with zipfile.ZipFile(_zip_bytes([('a', b''), ('b', b''), ('c', b'')])) as zf:
        assert 'เกิน' in bsn._zip_preflight(zf)


def test_preflight_rejects_total_uncompressed(monkeypatch):
    monkeypatch.setattr(bsn, '_EXPRESS_DBF_MAX_TOTAL_BYTES', 10)
    with zipfile.ZipFile(_zip_bytes([('a', b'0123456789ABC')])) as zf:
        assert 'ใหญ่เกินไป' in bsn._zip_preflight(zf)


def test_preflight_accepts_normal_zip():
    with zipfile.ZipFile(_zip_bytes([('xp5/ARTRN.DBF', b'x')])) as zf:
        assert bsn._zip_preflight(zf) is None


# ── discovery + classification ──────────────────────────────────────────────

def test_find_all_dataset_dirs(tmp_path):
    for d in ('one', 'two/nested'):
        p = tmp_path / d
        p.mkdir(parents=True)
        (p / 'ARTRN.DBF').write_bytes(b'')
    (tmp_path / 'noise').mkdir()
    dirs = bsn._find_express_dbf_dataset_dirs(str(tmp_path))
    assert len(dirs) == 2


def test_classify_by_isinfo_signature(monkeypatch, tmp_path):
    import express_dbf_source as eds
    sigs = {
        'b': [{'THINAM': '(BSN)บจก.บุญสวัสดิ์นำชัย', 'TAXID': '0000000000000'}],
        'v': [{'THINAM': 'บริษัท บุญสวัสดิ์ นำชัย จำกัด', 'TAXID': '0105542067386'}],
        'x': [{'THINAM': 'บริษัทอื่น', 'TAXID': '1234567890123'}],
    }
    monkeypatch.setattr(eds, 'open_table',
                        lambda d, name: sigs[os.path.basename(d)])
    assert bsn._classify_dataset(str(tmp_path / 'b')) == 'bsn'
    assert bsn._classify_dataset(str(tmp_path / 'v')) == 'vat'
    assert bsn._classify_dataset(str(tmp_path / 'x')) is None


# ── upload route behavior ───────────────────────────────────────────────────

def _upload(client, entries):
    return client.post('/import-express-dbf/upload',
                       data={'file': (_zip_bytes(entries), 'daily.zip')},
                       content_type='multipart/form-data',
                       follow_redirects=False)


def _fake_per_type(n=0):
    """A stand-in for commit_express_dbf()'s return value. ONE definition, so a
    new key in that contract is a single edit here rather than a hunt through
    every stub — the summary builder reads it with [], and a missing key makes
    the route report the whole import as failed."""
    return ({t: {'imported': n} for t in
             ('sales', 'purchase', 'payments_in', 'payments_out',
              'ar_snapshot', 'ap_snapshot')}
            | {'credit_notes_ar': {'upserted': n},
               'credit_notes_ap': {'imported': n},
               'snapshot_date': '2026-08-17'})


def test_single_dataset_without_isinfo_stays_legacy_bsn(tmp_db, monkeypatch):
    """The team's existing daily zip carries no ISINFO.DBF — it must keep
    importing as the BSN book exactly like before this feature."""
    called = []
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(
        bsn.import_router, 'commit_express_dbf',
        lambda *a, **k: (called.append(1) or _fake_per_type()))
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert called == [1]


def test_multi_dataset_with_missing_isinfo_rejects_all(tmp_db, monkeypatch):
    kinds = {'bsn5657': 'bsn', 'xp5': 'missing'}
    monkeypatch.setattr(bsn, '_classify_dataset',
                        lambda d: kinds[os.path.basename(d)])
    called = []
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: called.append(1))
    r = _upload(_client(), [('bsn5657/ARTRN.DBF', b'x'), ('xp5/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert called == []


def test_unknown_dataset_rejects_whole_upload(tmp_db, monkeypatch):
    called = []
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: None)
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: called.append(1))
    r = _upload(_client(), [('data/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert called == []                       # nothing imported


def test_both_datasets_import_and_persist_results(tmp_db, monkeypatch):
    kinds = {'bsn5657': 'bsn', 'xp5': 'vat'}
    monkeypatch.setattr(bsn, '_classify_dataset',
                        lambda d: kinds[os.path.basename(d)])
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: _fake_per_type(1))
    spawned = []
    monkeypatch.setattr(bsn, '_spawn_vat_rebuild',
                        lambda d, rid, sd=None: spawned.append((d, rid, sd)))
    r = _upload(_client(), [('bsn5657/ARTRN.DBF', b'x'), ('xp5/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    assert len(spawned) == 1
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    notes = json.loads(conn.execute(
        "SELECT notes FROM import_log WHERE filename='express-dbf-upload' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    conn.close()
    assert notes['bsn']['ok'] is True
    assert notes['vat']['status'] == 'building'
    assert spawned[0][1] > 0                  # run_id handed to the builder


def test_bsn_failure_still_reported_and_vat_spawned(tmp_db, monkeypatch):
    kinds = {'bsn5657': 'bsn', 'xp5': 'vat'}
    monkeypatch.setattr(bsn, '_classify_dataset',
                        lambda d: kinds[os.path.basename(d)])

    def _boom(*a, **k):
        raise RuntimeError('parse died')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf', _boom)
    spawned = []
    monkeypatch.setattr(bsn, '_spawn_vat_rebuild',
                        lambda d, rid, sd=None: spawned.append(rid))
    r = _upload(_client(), [('bsn5657/ARTRN.DBF', b'x'), ('xp5/ARTRN.DBF', b'x')])
    assert r.status_code == 302
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    notes = json.loads(conn.execute(
        "SELECT notes FROM import_log WHERE filename='express-dbf-upload' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    conn.close()
    assert notes['bsn']['ok'] is False and 'parse died' in notes['bsn']['error']
    assert len(spawned) == 1                  # BSN failing doesn't block VAT


def test_spawn_refuses_while_lock_held(tmp_db, tmp_path):
    import book_registry as br
    import vat_book_builder as vb
    fd = vb.acquire_publish_lock(br.book_db_path('vat'))
    try:
        with pytest.raises(RuntimeError, match='กำลังทำงาน'):
            bsn._spawn_vat_rebuild(str(tmp_path), 1)
    finally:
        vb.release_publish_lock(fd)


def test_spawn_proceeds_past_unheld_lockfile(tmp_db, tmp_path, monkeypatch):
    """A lockfile from a dead builder is UNflocked (kernel released it) —
    the probe passes without any removal, and the file is left in place."""
    import book_registry as br
    lock = br.book_db_path('vat') + '.lock'
    with open(lock, 'w') as fh:
        fh.write('99999 2026-08-02T00:00:00')  # breadcrumb from a dead run
    dataset = tmp_path / 'ds'
    dataset.mkdir()
    (dataset / 'ARTRN.DBF').write_bytes(b'')
    spawned = []
    monkeypatch.setattr(
        bsn.subprocess, 'Popen',
        lambda *a, **k: spawned.append(a) or type('P', (), {'poll': lambda s: 0})())
    bsn._spawn_vat_rebuild(str(dataset), 7)
    assert os.path.exists(lock)                # never unlinked
    assert len(spawned) == 1


def test_import_page_shows_vat_freshness_and_last_run(tmp_db, monkeypatch):
    import book_registry as br
    path = br.book_db_path('vat')
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO book_meta VALUES ('built_at', '2026-08-02T09:00:00')")
    c.commit()
    c.close()
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
        "VALUES ('express-dbf-upload', 0, 0, ?)",
        (json.dumps({'bsn': {'ok': True, 'summary': 'นำเข้าสำเร็จ'},
                     'vat': {'ok': True, 'built_at': '2026-08-02T09:00:00'}},
                    ensure_ascii=False),))
    conn.commit()
    conn.close()
    html = _client().get('/import-express-dbf').get_data(as_text=True)
    assert 'สมุด VAT (xp5): rebuild ล่าสุด' in html
    assert 'ผลการนำเข้าล่าสุด' in html


def test_import_page_shows_reconcile_scan_error_line(tmp_db):
    """FIX 6: scan_reconcile's isolated failure ({'error': ...}) must render
    as its OWN distinct line, not silently disappear and not be mistaken for
    a class-count of zero. Asserted on the element (its class marker), not a
    bare Thai substring — a substring match on 'ตรวจสอบบิลหายจาก Express'
    would also match the healthy summary line."""
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
        "VALUES ('express-dbf-upload', 0, 0, ?)",
        (json.dumps({'bsn': {'ok': True, 'summary': 'นำเข้าสำเร็จ',
                             'reconcile': {'error': 'db locked'}}},
                    ensure_ascii=False),))
    conn.commit()
    conn.close()
    html = _client().get('/import-express-dbf').get_data(as_text=True)
    assert 'reconcile-scan-error' in html
    assert 'db locked' in html
    assert 'การนำเข้าปกติไม่กระทบ' in html


def test_import_page_shows_reconcile_reappeared_count(tmp_db):
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.execute(
        "INSERT INTO import_log (filename, rows_imported, rows_skipped, notes) "
        "VALUES ('express-dbf-upload', 0, 0, ?)",
        (json.dumps({'bsn': {'ok': True, 'summary': 'นำเข้าสำเร็จ',
                             'reconcile': {'deleted': 0, 'out_of_scope': 0,
                                          'parse_gap': 0, 'date_moved': 0,
                                          'data_gap': 0, 'reappeared': 3}}},
                    ensure_ascii=False),))
    conn.commit()
    conn.close()
    html = _client().get('/import-express-dbf').get_data(as_text=True)
    assert 'กลับมาแล้ว 3' in html
    assert 'reconcile-scan-error' not in html


def test_both_books_get_the_same_snapshot_date(tmp_db, monkeypatch):
    """One upload, one as-of date. The VAT half runs in a detached subprocess that
    starts minutes later and can cross midnight, so each side calling date.today()
    for itself is how the two books end up stamped on different days (Codex P2)."""
    kinds = {'bsn5657': 'bsn', 'xp5': 'vat'}
    monkeypatch.setattr(bsn, '_classify_dataset',
                        lambda d: kinds[os.path.basename(d)])
    seen = {}

    def _bsn(*a, **k):
        seen['bsn'] = k.get('snapshot_date')
        return _fake_per_type(1)
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf', _bsn)
    monkeypatch.setattr(bsn, '_spawn_vat_rebuild',
                        lambda d, rid, sd=None: seen.__setitem__('vat', sd))

    r = _upload(_client(), [('bsn5657/ARTRN.DBF', b'x'), ('xp5/ARTRN.DBF', b'x')])

    assert r.status_code == 302
    assert seen['bsn'] is not None, 'the route must decide the date, not the importer'
    assert seen['vat'] == seen['bsn']


def test_vat_rebuild_is_launched_with_the_snapshot_date_argument(tmp_db, monkeypatch):
    """The date has to survive as far as the subprocess argv — a value the route
    computes and then drops on the floor looks identical from inside the route."""
    kinds = {'bsn5657': 'bsn', 'xp5': 'vat'}
    monkeypatch.setattr(bsn, '_classify_dataset',
                        lambda d: kinds[os.path.basename(d)])
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: _fake_per_type(1))
    argv = {}

    class _Proc:
        def poll(self):
            return 0

    def _popen(cmd, **kw):
        argv['cmd'] = cmd
        return _Proc()
    monkeypatch.setattr(bsn.subprocess, 'Popen', _popen)

    _upload(_client(), [('bsn5657/ARTRN.DBF', b'x'), ('xp5/ARTRN.DBF', b'x')])

    assert '--snapshot-date' in argv['cmd']
    assert argv['cmd'][argv['cmd'].index('--snapshot-date') + 1].count('-') == 2


# ── stale-zip guard (Phase 1: the export stamp) ─────────────────────────────
#
# Re-uploading an OLD zip is not merely a no-op. Three separate things act on
# "what the file says is true right now":
#   * models/payments.py DELETEs paid_invoices links a newer receipt no longer
#     lists — an older ARRCPIT erases links added since (money path),
#   * scan_reconcile flags Sendy docs absent from the file as vanished,
#   * the outstanding snapshots replace the whole (entity, date).
# So the whole upload is refused, not just the snapshot half.

import datetime as _dt


def _zip_bytes_dated(entries, when):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries:
            zi = zipfile.ZipInfo(name, date_time=when)
            zf.writestr(zi, data)
    buf.seek(0)
    return buf


def _upload_dated(client, entries, when):
    return client.post('/import-express-dbf/upload',
                       data={'file': (_zip_bytes_dated(entries, when), 'daily.zip')},
                       content_type='multipart/form-data',
                       follow_redirects=False)


def _set_snapshot(db_path, date_iso):
    """Force the stored-snapshot state this test needs, rather than inheriting
    whatever the cloned live DB happens to hold. Both tables, because the guard
    takes MAX across them — seeding only AR would leave a stale AP date able to
    decide the outcome. batch_id is NOT NULL, so a log row comes first."""
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM express_ar_outstanding WHERE entity='BSN'")
    conn.execute("DELETE FROM express_ap_outstanding WHERE entity='BSN'")
    batch = conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, status) "
        "VALUES ('ar_snapshot', 'test-fixture', 'imported')").lastrowid
    conn.execute(
        "INSERT INTO express_ar_outstanding (batch_id, entity, snapshot_date_iso, doc_no, "
        " customer_code, customer_name, bill_amount, paid_amount, outstanding_amount,"
        " is_anomalous, has_warning, doc_date_iso) "
        "VALUES (?, 'BSN', ?, 'IV1', 'C1', 'x', 10, 0, 10, 0, 0, '2026-01-01')",
        (batch, date_iso))
    conn.commit()
    conn.close()


def test_zip_export_datetime_is_the_newest_dbf_member():
    """The zip already carries each member's mtime, and Express only rewrites
    the tables it touched — so the export time is the MAX across members, not
    any single table's."""
    with zipfile.ZipFile(_zip_bytes_dated(
            [('b/ARTRN.DBF', b'x'), ('b/ARTRNRM.DBF', b'y')], (2026, 8, 4, 16, 32, 0))) as zf:
        old = bsn._zip_export_datetime(zf)
    assert old == _dt.datetime(2026, 8, 4, 16, 32, 0)


def test_older_zip_is_refused_and_imports_nothing(tmp_db, monkeypatch):
    import config
    _set_snapshot(config.DATABASE_PATH, '2026-08-17')
    called = []
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: called.append(1) or _fake_per_type())

    r = _upload_dated(_client(), [('data/ARTRN.DBF', b'x')], (2026, 8, 15, 16, 50, 0))

    assert r.status_code == 302
    assert called == [], 'a stale zip must not reach the importer at all'


def test_same_day_reupload_is_still_accepted(tmp_db, monkeypatch):
    """The team re-uploads the same day's zip whenever something looked wrong.
    The comparison is strictly-older, so that recovery path keeps working."""
    import config
    _set_snapshot(config.DATABASE_PATH, '2026-08-17')
    called = []
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: called.append(1) or _fake_per_type())

    r = _upload_dated(_client(), [('data/ARTRN.DBF', b'x')], (2026, 8, 17, 9, 0, 0))

    assert r.status_code == 302
    assert called == [1]


def test_stale_zip_can_be_forced_through_deliberately(tmp_db, monkeypatch):
    import config
    _set_snapshot(config.DATABASE_PATH, '2026-08-17')
    called = []
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: called.append(1) or _fake_per_type())

    r = _client().post('/import-express-dbf/upload',
                       data={'file': (_zip_bytes_dated([('data/ARTRN.DBF', b'x')],
                                                       (2026, 8, 15, 16, 50, 0)), 'daily.zip'),
                             'force_older': '1'},
                       content_type='multipart/form-data', follow_redirects=False)

    assert r.status_code == 302
    assert called == [1], 'an explicit override must still be able to import'


def test_snapshot_date_comes_from_the_zip_not_the_clock(tmp_db, monkeypatch):
    """The whole point: the as-of date describes when Express was exported, not
    when someone got round to uploading it.

    The zip is deliberately dated YESTERDAY. Stamping it with today's date is the
    bug being fixed, and a fixture dated today could not tell the two apart — it
    would pass against `date.today()` just as well."""
    yesterday = _dt.date.today() - _dt.timedelta(days=1)
    _set_snapshot(__import__('config').DATABASE_PATH,
                  (yesterday - _dt.timedelta(days=7)).isoformat())
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    seen = {}

    def _cap(*a, **k):
        seen['date'] = k.get('snapshot_date')
        return _fake_per_type()
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf', _cap)

    _upload_dated(_client(), [('data/ARTRN.DBF', b'x')],
                  (yesterday.year, yesterday.month, yesterday.day, 16, 50, 0))

    assert seen['date'] == yesterday.isoformat()
    assert seen['date'] != _dt.date.today().isoformat()


def test_upload_records_the_file_it_actually_imported(tmp_db, monkeypatch):
    """import_log used to store the constant 'express-dbf-upload', so nothing
    could answer "was today's upload today's export?" after the fact."""
    import config
    monkeypatch.setattr(bsn, '_classify_dataset', lambda d: 'missing')
    monkeypatch.setattr(bsn.import_router, 'commit_express_dbf',
                        lambda *a, **k: _fake_per_type())

    _upload_dated(_client(), [('data/ARTRN.DBF', b'x')], (2026, 8, 17, 16, 50, 0))

    conn = sqlite3.connect(config.DATABASE_PATH)
    notes = json.loads(conn.execute(
        "SELECT notes FROM import_log WHERE filename='express-dbf-upload' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    conn.close()
    up = notes['upload']
    assert up['filename'] == 'daily.zip'
    assert up['bytes'] > 0
    assert len(up['sha256']) == 64
    assert up['export_at'].startswith('2026-08-17')
