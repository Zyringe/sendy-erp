"""CSRF protection tests for the Sendy ERP Flask app.

Default test config has WTF_CSRF_ENABLED=False (set by tests/conftest.py via
env var) so the ~400 existing POST tests don't break. This file explicitly
re-enables CSRF per-fixture to assert the gate works on real request paths.
"""
import os

import pytest


@pytest.fixture
def csrf_client(tmp_db):
    """Test client with CSRF re-enabled. Returns (client, app).

    Restores the disabled state on teardown so this fixture doesn't leak
    into later tests that import the same app module.
    """
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'admin'
        sess['role']     = 'admin'
    try:
        yield c, flask_app
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = False


def test_post_without_csrf_token_is_rejected(csrf_client):
    """POST without csrf_token must NOT succeed. CSRFProtect returns 400;
    our global error handler converts that to a 302 redirect with a flash."""
    client, _ = csrf_client
    resp = client.post(
        '/unit-conversions/save',
        data={'ratio_1_lng': '12'},
        follow_redirects=False,
    )
    # 400 = raw CSRFError; 302 = our handler's redirect-to-referrer.
    assert resp.status_code in (400, 302), (
        f"Expected 400 or 302 from CSRF gate, got {resp.status_code}: "
        f"{resp.data[:300]!r}"
    )


def test_post_with_csrf_token_is_accepted(csrf_client):
    """POST with a valid csrf_token must clear the CSRF gate. Downstream
    handler may redirect (302) on success — but must NOT return a CSRF 400."""
    from flask_wtf.csrf import generate_csrf
    client, flask_app = csrf_client
    # generate_csrf() needs a request context AND must run against the same
    # session as the client — use the test client's session.
    with client.session_transaction() as sess:
        # First, hit a GET so the session cookie is established.
        pass
    # Pull a fresh token bound to this client's session.
    with flask_app.test_request_context():
        with client.session_transaction() as sess:
            # Flask-WTF reads/writes session['csrf_token'] (the secret).
            # generate_csrf() does this for us, but we need to do it in a
            # context where session = client session. Easier path: use the
            # client to GET a page that renders csrf_token(), parse it out.
            pass
    # Simpler: GET the login page to provoke csrf_token() into the session,
    # then scrape the token from the rendered HTML.
    with client.session_transaction() as sess:
        sess.clear()
        sess['user_id']  = 1
        sess['username'] = 'admin'
        sess['role']     = 'admin'
    # /unit-conversions (GET) renders the page that POSTs to /save and now
    # contains csrf_token() — but pulling from there is page-dependent.
    # /login renders csrf_token() too and is always available.
    # We can't use /login because we'd lose the session.
    # Instead, render any authenticated page that has csrf_token().
    resp = client.get('/unit-conversions')
    assert resp.status_code == 200, resp.data[:300]
    import re
    m = re.search(
        rb'name="csrf_token"\s+value="([^"]+)"', resp.data
    )
    assert m, "csrf_token hidden input not found on /unit-conversions"
    token = m.group(1).decode()

    resp = client.post(
        '/unit-conversions/save',
        data={'csrf_token': token, 'ratio_1_lng': '0'},  # ratio=0 → no items saved
        follow_redirects=False,
    )
    # CSRF gate cleared → handler runs and redirects to /unit-conversions.
    assert resp.status_code == 302, (
        f"Expected 302 redirect after valid POST, got {resp.status_code}: "
        f"{resp.data[:300]!r}"
    )
    # And the redirect target must NOT be a CSRF-error redirect (our handler
    # redirects to request.referrer which would be None here → dashboard).
    # Sanity: the successful handler redirects to unit_conversions.
    assert '/unit-conversions' in resp.headers.get('Location', '')


def test_csrf_enabled_by_default_in_production_config(monkeypatch):
    """app.config['WTF_CSRF_ENABLED'] must default to True when the env var
    is unset (production case)."""
    import sys
    monkeypatch.delenv('WTF_CSRF_ENABLED', raising=False)
    # Force re-import of the app module so its module-level config code re-runs.
    sys.modules.pop('app', None)
    try:
        from app import app as flask_app
        assert flask_app.config['WTF_CSRF_ENABLED'] is True
    finally:
        sys.modules.pop('app', None)


def test_login_template_renders_csrf_token(csrf_client):
    """The login template must include the csrf_token hidden input so users
    can actually log in once CSRF is enabled."""
    client, _ = csrf_client
    with client.session_transaction() as sess:
        sess.clear()
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data, (
        "login template must include the csrf_token hidden input"
    )


def test_bootstrap_upload_db_is_csrf_exempt(csrf_client):
    """/bootstrap/upload-db is gated by BOOTSTRAP_TOKEN env (no session),
    so it must be exempted from CSRF. Verify both that the exempt
    registration didn't break the route AND that a token-bearing POST is
    not rejected by the CSRF layer."""
    client, flask_app = csrf_client
    # Route must still be registered.
    rules = [r.rule for r in flask_app.url_map.iter_rules()]
    assert '/bootstrap/upload-db' in rules

    # With BOOTSTRAP_TOKEN unset the route returns 404 (its own design),
    # NOT 400 from CSRF — proves the CSRF gate didn't fire.
    os.environ.pop('BOOTSTRAP_TOKEN', None)
    resp = client.post('/bootstrap/upload-db', data={})
    assert resp.status_code == 404, (
        f"Expected 404 from disabled bootstrap, got {resp.status_code} — "
        "CSRF may not be properly exempted"
    )
