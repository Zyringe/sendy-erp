"""Upload/Download DB unlock must be SESSION-scoped, not process-global.

Regression for the Railway "usually Forbidden" bug. DB_ROUTES_ENABLED lived in
app.config (per gunicorn worker process). With `-w 2`, clicking the เปิด toggle
armed only the one worker that handled the POST; ~50% of follow-up requests
load-balanced to the still-locked worker -> abort(403) -> the verbatim Werkzeug
"Forbidden ... read-protected or not readable by the server" page.

The fix moves the unlock into the signed-cookie session, which already travels
with every request and is coherent across workers (same mechanism that keeps
login working under -w 2). These tests run in ONE process, so they can't
reproduce the worker split directly; instead they pin the behavioral contract
that makes the fix multi-worker-safe: the unlock lives in the session, and one
session's toggle must NOT arm a different session (the exact opposite of the
old process-global leak), and the toggle must not mutate app.config.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def _login_as(client, role='admin'):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = role
        sess['role'] = role


def _arm(client):
    """Click the เปิด/ปิด Upload/Download DB toggle."""
    return client.post('/admin/toggle-db-routes')


def test_download_db_forbidden_when_not_armed(client):
    _login_as(client, 'admin')
    assert client.get('/admin/download-db').status_code == 403


def test_download_db_ok_after_arming_in_same_session(client):
    _login_as(client, 'admin')
    _arm(client)
    assert client.get('/admin/download-db').status_code == 200
    # the unlock is carried in the session, not in process-global config
    with client.session_transaction() as sess:
        assert sess.get('db_routes_enabled') is True


def test_upload_db_get_forbidden_when_not_armed(client):
    _login_as(client, 'admin')
    assert client.get('/admin/upload-db').status_code == 403


def test_upload_db_get_ok_after_arming(client):
    _login_as(client, 'admin')
    _arm(client)
    assert client.get('/admin/upload-db').status_code == 200


def test_unlock_is_not_a_process_global_leak(client):
    """THE multi-worker regression. Arming in session A must NOT arm a fresh
    session B. On the old app.config implementation B saw 200 (the global flag
    leaked across every request / worker). With session scoping B stays 403."""
    from app import app as flask_app

    _login_as(client, 'admin')
    _arm(client)
    assert client.get('/admin/download-db').status_code == 200

    # a brand-new client = a fresh session/cookie; admin, but never armed
    with flask_app.test_client() as client_b:
        with client_b.session_transaction() as sess:
            sess['user_id'] = 2
            sess['username'] = 'admin2'
            sess['role'] = 'admin'
        assert client_b.get('/admin/download-db').status_code == 403

    # arming must not have mutated process-global config (the root defect)
    assert flask_app.config.get('DB_ROUTES_ENABLED', False) is False


def test_disarm_relocks(client):
    _login_as(client, 'admin')
    _arm(client)
    assert client.get('/admin/download-db').status_code == 200
    _arm(client)  # toggle off
    assert client.get('/admin/download-db').status_code == 403


def test_non_admin_cannot_arm_or_download(client):
    _login_as(client, 'manager')
    client.post('/admin/toggle-db-routes')  # blocked (POST-gate redirect or 403)
    with client.session_transaction() as sess:
        assert not sess.get('db_routes_enabled')
    assert client.get('/admin/download-db').status_code == 403
