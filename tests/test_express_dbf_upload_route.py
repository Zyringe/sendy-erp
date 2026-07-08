"""Route tests for /import-express-dbf (GET) + /import-express-dbf/upload
(POST) — projects/express-integration/plan.md Phase 2.

The upload POST is the team's non-interactive end-of-day script: no browser
session ever accompanies it, so it is gated by EXPRESS_UPLOAD_TOKEN instead
(same precedent as /bootstrap/upload-db) and exempted from both the login
gate (access_control.py) and CSRF (app.py's csrf.exempt() call).

Reuses the DBF-row fixture builders from test_express_dbf_source.py so the
"real adapters, fake open_table()" test exercises the SAME
import_router.commit_express_dbf() path Phase 1's own wiring test does —
entered here via a real zip upload over HTTP instead of a direct call.
"""
import datetime
import os
import sqlite3
import zipfile

import pytest

# Row-fixture builders duplicated (not imported) from test_express_dbf_source.py —
# pytest only adds a test module's own directory to sys.path when THAT module
# is collected, so `from test_express_dbf_source import ...` only works when
# both files happen to be collected together. Keep these tiny copies in sync
# with that file's field shapes if MAPPING.md's traps ever change.

def _artrn(docnum, rectyp, *, cuscod='C001', flgvat=0, docdat=None, youref=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'CUSCOD': cuscod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1), 'YOUREF': youref,
    }


def _aptrn(docnum, rectyp, *, supcod='S001', flgvat=0, docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': rectyp, 'SUPCOD': supcod, 'FLGVAT': flgvat,
        'DOCDAT': docdat or datetime.date(2026, 4, 1),
    }


def _stcrd(docnum, seqnum, *, stkcod='804', stkdes='name', qty=2.0, unit='ตัว',
           unitpr=45.0, disc='', trnval=90.0, netval=90.0, rdocnum=''):
    return {
        'DOCNUM': docnum, 'SEQNUM': seqnum, 'STKCOD': stkcod, 'STKDES': stkdes,
        'TRNQTY': qty, 'TQUCOD': unit, 'UNITPR': unitpr, 'DISC': disc,
        'TRNVAL': trnval, 'NETVAL': netval, 'RDOCNUM': rdocnum,
    }


def _artrn_re(docnum, *, cuscod='C001', slmcod='06', docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': '9', 'CUSCOD': cuscod, 'SLMCOD': slmcod,
        'DOCDAT': docdat or datetime.date(2026, 5, 1),
    }


def _arrcpit(rcpnum, docnum, rectyp, rcvamt):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'RECTYP': rectyp, 'RCVAMT': rcvamt}


def _aptrn_ps(docnum, *, supcod='S001', rcvamt=0.0, docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': '9', 'SUPCOD': supcod, 'RCVAMT': rcvamt,
        'DOCDAT': docdat or datetime.date(2026, 5, 1),
    }


def _aprcpit(rcpnum, docnum, payamt):
    return {'RCPNUM': rcpnum, 'DOCNUM': docnum, 'PAYAMT': payamt}


def _artrn_sr(docnum, *, cuscod='C001', sonum=None, total=0.0, docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': '5', 'CUSCOD': cuscod, 'SONUM': sonum,
        'TOTAL': total, 'DOCDAT': docdat or datetime.date(2026, 2, 1),
    }


def _aptrn_gr(docnum, *, supcod='S001', docdat=None):
    return {
        'DOCNUM': docnum, 'RECTYP': '5', 'SUPCOD': supcod,
        'DOCDAT': docdat or datetime.date(2024, 6, 1),
    }


def _login(client, role='admin', user_id=1):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def client(tmp_db):
    # tmp_db (a live-DB clone, not empty_db) already carries the seeded
    # 'BSN' companies row that import_express._company_id() requires — no
    # extra seeding needed here, unlike test_express_dbf_source.py's
    # empty_db-based tests.
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


# The route calls commit_express_dbf() with its default since_days=60, so
# every header row below must be recent (relative to today, not a fixed
# date) or the recency filter (see test_express_dbf_source.py) would drop
# it before it ever reaches these assertions.
_RECENT = datetime.date.today() - datetime.timedelta(days=5)


def _fake_tables():
    """One row per Phase-0 RECTYP scope, same shape as
    test_express_dbf_source.py's test_commit_express_dbf_wires_all_six_types
    (fresh doc numbers so this file's assertions don't depend on that one)."""
    return {
        'ARTRN': [
            _artrn('IV7000931', '3', cuscod='C900', docdat=_RECENT),
            _artrn_re('RE7000400', cuscod='C900', docdat=_RECENT),
            _artrn_sr('SR7000010', cuscod='C900', sonum='IV7000931', total=100.0, docdat=_RECENT),
        ],
        'APTRN': [
            _aptrn('RR7000079', '3', supcod='S900', docdat=_RECENT),
            _aptrn_ps('PS0900000', supcod='S900', rcvamt=50.0, docdat=_RECENT),
            _aptrn_gr('GR7000002', supcod='S900', docdat=_RECENT),
        ],
        'STCRD': [
            _stcrd('IV7000931', 1, stkcod='route-sale', qty=1.0, unitpr=45.0, trnval=45.0, netval=45.0),
            _stcrd('RR7000079', 1, stkcod='route-purch', qty=10.0, unitpr=4.80, trnval=48.0, netval=48.0),
            _stcrd('GR7000002', 1, stkcod='route-purch', qty=1.0, unitpr=48.0, trnval=48.0, netval=48.0),
        ],
        'ARMAS': [{'CUSCOD': 'C900', 'CUSNAM': 'ลูกค้าทดสอบ route'}],
        'APMAS': [{'SUPCOD': 'S900', 'SUPNAM': 'ซัพพลายเออร์ทดสอบ route'}],
        'ARTRNRM': [],
        'ARRCPIT': [_arrcpit('RE7000400', 'IV7000931', '3', 45.0)],
        'APRCPIT': [_aprcpit('PS0900000', 'RR7000079', 48.0)],
    }


def _make_zip(tmp_path, names=None):
    """A zip of placeholder .DBF files. The route's own unzip + dataset-dir
    discovery run for real; the CONTENTS are never read because
    express_dbf_source.open_table is monkeypatched in the tests that need
    it — same real-adapter/fake-IO split as test_express_dbf_source.py."""
    zpath = tmp_path / 'upload.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for name in (names or _fake_tables()):
            zf.writestr(f'{name}.DBF', b'placeholder')
    return str(zpath)


def _patch_open_table(monkeypatch):
    import express_dbf_source as eds
    fake_tables = _fake_tables()
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: fake_tables[name])


# ── GET /import-express-dbf — normal login gate ─────────────────────────────

def test_get_page_requires_login(client):
    resp = client.get('/import-express-dbf', follow_redirects=False)
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')


def test_get_page_renders_for_logged_in_user(client):
    _login(client)
    resp = client.get('/import-express-dbf')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'action="/import-express-dbf/upload"' in html
    assert 'name="token"' in html
    assert 'name="file"' in html


# ── Nav wiring (3 surfaces) ──────────────────────────────────────────────────

def test_endpoint_module_and_nav_present(client):
    from access_control import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('bsn.express_dbf_import') == 'data'

    _login(client)
    html = client.get('/import-express-dbf').get_data(as_text=True)
    assert html.count('href="/import-express-dbf"') >= 2, \
        "express-dbf-import link missing from a nav (sidebar or mobile drawer)"


# ── POST /import-express-dbf/upload — auth / input guards ──────────────────

def test_upload_404_when_token_not_configured(client, tmp_path, monkeypatch):
    monkeypatch.delenv('EXPRESS_UPLOAD_TOKEN', raising=False)
    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'whatever', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 404


def test_upload_403_on_wrong_token(client, tmp_path, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'wrong-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 403
    assert resp.get_json()['ok'] is False


def test_upload_400_on_missing_file(client, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    resp = client.post('/import-express-dbf/upload', data={'token': 'right-token'},
                       content_type='multipart/form-data')
    assert resp.status_code == 400
    assert 'file' in resp.get_json()['error']


def test_upload_400_on_non_zip_filename(client, tmp_path, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    p = tmp_path / 'notazip.txt'
    p.write_text('hello')
    with open(p, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'notazip.txt')},
                           content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_400_on_corrupt_zip_bytes(client, tmp_path, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    p = tmp_path / 'fake.zip'
    p.write_bytes(b'not actually a zip file')
    with open(p, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'fake.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 400


def test_upload_400_when_artrn_missing_from_zip(client, tmp_path, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    zpath = _make_zip(tmp_path, names=['APTRN', 'STCRD'])   # no ARTRN.DBF
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 400
    assert 'ARTRN' in resp.get_json()['error']


def test_upload_413_when_over_scoped_size_cap(client, tmp_path, monkeypatch):
    import blueprints.bsn as bsn_mod
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    monkeypatch.setattr(bsn_mod, '_EXPRESS_DBF_MAX_UPLOAD_BYTES', 10)
    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 413


# ── POST /import-express-dbf/upload — no session required (skip-list) ──────

def test_upload_reaches_view_with_zero_session_state(client, tmp_path, monkeypatch):
    """A request with NO session cookie at all must reach the view logic
    (proves the require_login skip-list wiring) rather than 302-ing to
    /login. Wrong token still 403s — that's the view's OWN gate, not the
    login gate — but a 403 (not a 302-to-/login) is exactly the proof."""
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'wrong', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data',
                           follow_redirects=False)
    assert resp.status_code == 403, \
        f"expected the view's own 403, got a redirect (login gate not skipped?): {resp.status_code}"


def test_upload_exempt_from_csrf_even_when_csrf_enabled(tmp_db, tmp_path, monkeypatch):
    """Re-enable CSRF app-wide (like test_csrf_protection.py's csrf_client)
    and confirm the upload route still isn't blocked by it — no session,
    no csrf_token field, and it must NOT get the raw-CSRF 400."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        c = flask_app.test_client()
        monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
        zpath = _make_zip(tmp_path)
        with open(zpath, 'rb') as f:
            resp = c.post('/import-express-dbf/upload',
                          data={'token': 'wrong-token', 'file': (f, 'upload.zip')},
                          content_type='multipart/form-data')
        # Must reach the view's own token check (403), never the CSRF 400.
        assert resp.status_code == 403, resp.data[:300]
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = False


# ── POST /import-express-dbf/upload — happy path (real pipeline) ───────────

def test_upload_happy_path_imports_all_six_types(client, tmp_path, monkeypatch):
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    _patch_open_table(monkeypatch)

    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')

    assert resp.status_code == 200, resp.data[:500]
    body = resp.get_json()
    assert body['ok'] is True
    assert body['per_type']['payments_in']['imported'] == 1
    assert body['per_type']['payments_out']['imported'] == 1
    assert body['per_type']['credit_notes_ar']['upserted'] == 1
    assert body['per_type']['credit_notes_ap']['imported'] == 1
    assert body['per_type']['payments_in']['skipped_rectyp'] == [], \
        "no DR-style row in this fixture — the key must still be present, just empty"
    assert body['imported_at']

    import sqlite3
    import config
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    rp = conn.execute(
        "SELECT total FROM received_payments WHERE re_no='RE7000400'").fetchone()
    assert rp['total'] == 45.0
    cna = conn.execute(
        "SELECT credited_amount FROM credit_note_amounts WHERE sr_doc_base='SR7000010'"
    ).fetchone()
    assert cna['credited_amount'] == 100.0
    conn.close()


def test_upload_updates_freshness_badge(client, tmp_path, monkeypatch):
    # tmp_db is a fresh copy of the LIVE dev DB per test — don't assert
    # anything about the "before" state (a future prod->local sync could
    # legitimately carry real express_dbf rows). The invariant this test
    # actually pins is "right after a commit, freshness reads fresh."
    import models
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    _patch_open_table(monkeypatch)

    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 200

    after = models.get_express_dbf_freshness()
    assert after['last_at'] is not None
    assert after['is_stale'] is False


def test_upload_cleans_up_temp_dir(client, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')
    _patch_open_table(monkeypatch)

    created = []
    _orig_mkdtemp = tempfile.mkdtemp

    def _tracked(*a, **kw):
        p = _orig_mkdtemp(*a, **kw)
        created.append(p)
        return p
    monkeypatch.setattr(tempfile, 'mkdtemp', _tracked)

    zpath = _make_zip(tmp_path)
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')
    assert resp.status_code == 200
    assert created, "route never called tempfile.mkdtemp"
    assert not os.path.exists(created[-1]), "temp extraction dir was not cleaned up"


def test_upload_surfaces_dr_skip_end_to_end(client, tmp_path, monkeypatch):
    """A real-shaped ARRCPIT RECTYP='4' ('DR') line — the 1-in-57,024-row
    edge case found during real-data verification — must not 500 the whole
    upload; it shows up in the JSON response instead."""
    monkeypatch.setenv('EXPRESS_UPLOAD_TOKEN', 'right-token')

    import express_dbf_source as eds
    tables = {
        'ARTRN': [_artrn_re('RE7000401', cuscod='C900', docdat=_RECENT)],
        'APTRN': [], 'STCRD': [], 'ARMAS': [], 'APMAS': [], 'ARTRNRM': [],
        'ARRCPIT': [_arrcpit('RE7000401', 'DR0000099', '4', 600.0)],
        'APRCPIT': [],
    }
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: tables[name])

    zpath = _make_zip(tmp_path, names=list(tables))
    with open(zpath, 'rb') as f:
        resp = client.post('/import-express-dbf/upload',
                           data={'token': 'right-token', 'file': (f, 'upload.zip')},
                           content_type='multipart/form-data')

    assert resp.status_code == 200, resp.data[:500]
    body = resp.get_json()
    assert body['ok'] is True
    assert body['per_type']['payments_in']['skipped_rectyp'] == [
        {'re_no': 'RE7000401', 'doc': 'DR0000099', 'rectyp': '4', 'amount': 600.0}
    ]


# ── models.get_express_dbf_freshness ─────────────────────────────────────────

def test_freshness_no_rows_is_stale(empty_db_conn):
    import models
    freshness = models.get_express_dbf_freshness()
    assert freshness['last_at'] is None
    assert freshness['is_stale'] is True


def test_freshness_recent_row_not_stale(empty_db_conn):
    import models
    empty_db_conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, imported_at) "
        "VALUES ('payments_out', 'express_dbf', datetime('now','localtime'))"
    )
    empty_db_conn.commit()
    freshness = models.get_express_dbf_freshness()
    assert freshness['last_at'] is not None
    assert freshness['is_stale'] is False


def test_freshness_old_row_is_stale(empty_db_conn):
    import models
    empty_db_conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, imported_at) "
        "VALUES ('payments_out', 'express_dbf', datetime('now','localtime','-30 hours'))"
    )
    empty_db_conn.commit()
    freshness = models.get_express_dbf_freshness()
    assert freshness['hours_stale'] > 26
    assert freshness['is_stale'] is True


def test_freshness_ignores_other_source_filenames(empty_db_conn):
    """A row from the text-report path (source_filename = the real
    filename, not 'express_dbf') must not count as a DBF-direct import."""
    import models
    empty_db_conn.execute(
        "INSERT INTO express_import_log (file_type, source_filename, imported_at) "
        "VALUES ('payments_out', 'some_report.txt', datetime('now','localtime'))"
    )
    empty_db_conn.commit()
    freshness = models.get_express_dbf_freshness()
    assert freshness['last_at'] is None
    assert freshness['is_stale'] is True
