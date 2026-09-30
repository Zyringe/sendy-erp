"""แก้หน่วยบรรทัด (#692) as the existing pages see it: the daily-zip upload
route re-scans a retired correction's document, and the retirement reaches
/alerts."""
import datetime
import zipfile

import pytest

from tests import unit_correction_scenario as sc

LINE = 'IV6900001-1'
STALE = '2000-01-01 00:00:00'


@pytest.fixture
def client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'test-admin'
            sess['role'] = 'admin'
        yield c


# The upload route keeps its 60-day window, so the document must be recent.
_DAY = datetime.date.today() - datetime.timedelta(days=5)


def _book(qty, unit):
    return sc.Book().sale('IV6900001', [(1, sc.CODE, qty, unit, 49.0)], _DAY)


@pytest.fixture
def corrected(empty_db, monkeypatch):
    import line_unit_correction as luc
    sc.seed_company(empty_db)
    sc.seed_product(empty_db)
    sc.run_zip(monkeypatch, _book(2.0, 'โหล'))
    c = sc.conn(empty_db)
    luc.apply(c, LINE, sc.CODE, 'หลอด', 'hold', sc.REASON, 'put')
    c.close()
    return empty_db


def _upload(client, tmp_path, monkeypatch, book):
    import express_dbf_source as eds
    tables = book.tables()
    monkeypatch.setattr(eds, 'open_table', lambda dataset_dir, name: tables[name])
    zpath = tmp_path / 'upload.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for name in tables:
            zf.writestr(f'{name}.DBF', b'placeholder')
    with open(zpath, 'rb') as f:
        return client.post('/import-express-dbf/upload',
                           data={'file': (f, 'upload.zip')},
                           content_type='multipart/form-data', follow_redirects=True)


def test_the_upload_route_rescans_the_document_of_a_retired_correction(
        corrected, client, tmp_path, monkeypatch):
    c = sc.raw(corrected)
    c.execute("INSERT OR REPLACE INTO txn_review_docs (doc_base, date_iso, line_count,"
              " flag_count, scanned_at) VALUES ('IV6900001', ?, 1, 1, ?)",
              (_DAY.isoformat(), STALE))
    c.commit()
    c.close()

    resp = _upload(client, tmp_path, monkeypatch, _book(2.0, 'หลอด'))

    assert resp.status_code == 200
    correction, = sc.corrections(corrected)
    assert (correction['status'], correction['end_cause']) == ('retired', 'express_agrees')
    c = sc.raw(corrected)
    row = c.execute("SELECT scanned_at FROM txn_review_docs"
                    " WHERE doc_base='IV6900001'").fetchone()
    c.close()
    assert row is None or row['scanned_at'] != STALE


def test_a_retired_correction_is_shown_on_the_alerts_page(
        corrected, client, monkeypatch):
    sc.run_zip(monkeypatch, _book(3.0, 'โหล'))

    html = client.get('/alerts').get_data(as_text=True)

    assert 'การแก้หน่วยบรรทัด IV6900001-1 (โหล → หลอด) สิ้นสุดแล้ว' in html
    assert 'Express แก้บรรทัดนี้ ระบบจึงใช้ค่าของ Express' in html


@pytest.mark.parametrize('url,form', [
    ('/mapping/split-save', {'bsn_code': sc.CODE, 'bsn_unit': 'โหล'}),
    ('/unit-conversions/dismiss', {'bsn_unit': 'แพ็ค'}),
], ids=['split-save', 'dismiss'])
def test_a_guarded_page_shows_the_refusal_instead_of_failing(corrected, client, url, form):
    other = sc.seed_product(corrected, 'OTHER', name='อื่น')
    pid = sc.sales_row(corrected, LINE)['product_id']
    target = other if url == '/mapping/split-save' else pid
    before = sc.written_state(corrected)

    resp = client.post(url, data=dict(form, product_id=target), follow_redirects=True)

    assert resp.status_code == 200
    assert 'ยกเลิกการแก้หน่วยบรรทัดก่อน (IV6900001-1)' in resp.get_data(as_text=True)
    assert sc.written_state(corrected) == before
