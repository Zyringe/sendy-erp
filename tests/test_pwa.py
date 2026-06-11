"""
PWA shell smoke tests (Phase P1).

Checks:
- /sw.js  : 200, Content-Type application/javascript
- /static/manifest.json : 200, valid JSON, required keys present
- /help/install : 200 without auth (public endpoint)
- /healthz : 200 (regression)

All four endpoints MUST be reachable without a login session because the
browser fetches the SW and manifest outside the user session, and we want
the install page accessible before login.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import json
import pytest


@pytest.fixture
def anon_client(tmp_db):
    """Flask test client with NO session (anonymous / unauthenticated)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app.test_client()


@pytest.fixture
def auth_client(tmp_db):
    """Flask test client with an admin session."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


# ── /sw.js ────────────────────────────────────────────────────────────────────

def test_sw_js_200(anon_client):
    """Service worker reachable without login (browser fetches outside session)."""
    resp = anon_client.get('/sw.js')
    assert resp.status_code == 200, resp.data[:300]


def test_sw_js_content_type(anon_client):
    """Must be served as JavaScript so the browser registers it correctly."""
    resp = anon_client.get('/sw.js')
    assert 'javascript' in resp.content_type


def test_sw_js_has_cache_const(anon_client):
    """SW must define a versioned CACHE constant (needed for stale cache eviction)."""
    resp = anon_client.get('/sw.js')
    assert b"const CACHE" in resp.data or b"var CACHE" in resp.data


# ── /static/manifest.json ─────────────────────────────────────────────────────

def test_manifest_200(anon_client):
    """Manifest reachable via static route."""
    resp = anon_client.get('/static/manifest.json')
    assert resp.status_code == 200, resp.data[:300]


def test_manifest_valid_json(anon_client):
    """Manifest must parse as valid JSON."""
    resp = anon_client.get('/static/manifest.json')
    data = json.loads(resp.data)
    assert isinstance(data, dict)


def test_manifest_required_keys(anon_client):
    """PWA manifest must contain the keys required for installability."""
    resp = anon_client.get('/static/manifest.json')
    data = json.loads(resp.data)
    for key in ('name', 'short_name', 'start_url', 'display', 'icons'):
        assert key in data, f"manifest missing required key: {key}"


def test_manifest_has_maskable_icon(anon_client):
    """Manifest must include a maskable icon (for Android adaptive icons)."""
    resp = anon_client.get('/static/manifest.json')
    data = json.loads(resp.data)
    purposes = [icon.get('purpose', '') for icon in data.get('icons', [])]
    assert any('maskable' in p for p in purposes), \
        f"no maskable icon found; purposes={purposes}"


# ── /help/install ─────────────────────────────────────────────────────────────

def test_help_install_200_anon(anon_client):
    """/help/install reachable without login (should be shown before install)."""
    resp = anon_client.get('/help/install')
    assert resp.status_code == 200, resp.data[:300]


def test_help_install_200_auth(auth_client):
    """/help/install also works when logged in."""
    resp = auth_client.get('/help/install')
    assert resp.status_code == 200, resp.data[:300]


def test_help_install_contains_thai_steps(anon_client):
    """Install page must contain Thai instructions for Android and iPhone."""
    resp = anon_client.get('/help/install')
    body = resp.data.decode('utf-8')
    assert 'Android' in body
    assert 'iPhone' in body or 'Safari' in body


# ── /healthz (regression) ─────────────────────────────────────────────────────

def test_healthz_200(anon_client):
    """Healthcheck must stay 200 (Railway probe)."""
    resp = anon_client.get('/healthz')
    assert resp.status_code == 200
