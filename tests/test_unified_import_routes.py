"""Route-level integration tests for the unified /import box.

Exercises the full wiring (detect → preview → stage → confirm) against a
live-DB copy via tmp_db. Uses a synthetic cp874 payments_in report (header
only) so detection fires and preview/commit run without needing the flash
drive or real data.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from io import BytesIO

import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _payments_in_file():
    """Minimal cp874 การรับชำระหนี้ report (header only → detects, 0 records)."""
    body = (
        '"(BSN)บจก.บุญสวัสดิ์นำชัย                หน้า   :        1"\n'
        '"  รายงานการรับชำระหนี้ เรียงตามวันที่ของใบเสร็จ"\n'
        '"วันที่จาก   1 ม.ค. 2567  ถึง  31 ธ.ค. 2569"\n'
        '">>>> จบรายงาน <<<<"\n'
    ).encode('cp874')
    return BytesIO(body)


def test_import_get_renders_drop_zone(admin_client):
    resp = admin_client.get('/import-data')
    assert resp.status_code == 200
    assert 'ลากไฟล์' in resp.data.decode('utf-8')


def test_import_post_detects_and_previews(admin_client):
    resp = admin_client.post(
        '/import-data',
        data={'files': (_payments_in_file(), 'การรับชำระหนี้_x.csv')},
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200
    body = resp.data.decode('utf-8')
    assert 'ตรวจสอบก่อนยืนยัน' in body            # preview page rendered
    assert 'การรับชำระหนี้' in body               # detected label shown
    # the staged session carries the detected type for confirm
    with admin_client.session_transaction() as sess:
        stage = sess.get('import_stage')
        assert stage and stage['rows'][0]['detected'] == 'payments_in'


def test_import_confirm_commits(admin_client):
    # stage first
    admin_client.post(
        '/import-data',
        data={'files': (_payments_in_file(), 'การรับชำระหนี้_x.csv')},
        content_type='multipart/form-data',
    )
    with admin_client.session_transaction() as sess:
        token = sess['import_stage']['token']
    resp = admin_client.post(
        '/import-data/confirm',
        data={'token': token, 'type_0': 'payments_in'},
    )
    assert resp.status_code == 200
    assert 'ผลการนำเข้า' in resp.data.decode('utf-8')   # results page
    # staging cleared after confirm
    with admin_client.session_transaction() as sess:
        assert sess.get('import_stage') is None


def test_manager_can_post_and_confirm(tmp_db):
    """Regression: managers must reach the route (the global _MANAGER_POST_OK
    gate fires before the route body — endpoints must be whitelisted)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = 'mgr'
        sess['role'] = 'manager'
    resp = c.post('/import-data',
                  data={'files': (_payments_in_file(), 'การรับชำระหนี้_x.csv')},
                  content_type='multipart/form-data')
    assert resp.status_code == 200, 'manager POST must reach the route, not 302 to /'
    assert 'ตรวจสอบก่อนยืนยัน' in resp.data.decode('utf-8')
    with c.session_transaction() as sess:
        token = sess['import_stage']['token']
    resp2 = c.post('/import-data/confirm', data={'token': token, 'type_0': 'payments_in'})
    assert resp2.status_code == 200, 'manager confirm POST must reach the route'


def test_staff_can_access_and_post(tmp_db):
    """Staff may now use the unified import (Put enabled it 2026-06-03):
    GET renders the drop zone and POST reaches the preview, not a 302 to /."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 3
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    resp = c.get('/import-data')
    assert resp.status_code == 200
    assert 'ลากไฟล์' in resp.data.decode('utf-8')
    resp2 = c.post('/import-data',
                   data={'files': (_payments_in_file(), 'การรับชำระหนี้_x.csv')},
                   content_type='multipart/form-data')
    assert resp2.status_code == 200, 'staff POST must reach the route, not 302 to /'
    assert 'ตรวจสอบก่อนยืนยัน' in resp2.data.decode('utf-8')
    # Option B (Put, 2026-06-03): staff confirms/commits themselves; review is
    # done after the fact by manager/admin, not gated before commit.
    with c.session_transaction() as sess:
        token = sess['import_stage']['token']
    resp3 = c.post('/import-data/confirm', data={'token': token, 'type_0': 'payments_in'})
    assert resp3.status_code == 200, 'staff confirm POST must reach the route, not 302 to /'
    assert 'ผลการนำเข้า' in resp3.data.decode('utf-8')
