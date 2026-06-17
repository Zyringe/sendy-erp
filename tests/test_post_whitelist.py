"""Tests for the POST permission gate in app.py.

Verifies:
- staff cannot POST to endpoints outside _STAFF_POST_OK (redirect → dashboard)
- manager cannot POST to endpoints outside _MANAGER_POST_OK (redirect → dashboard)
- staff cannot GET /hr/* or /cashbook/* (separate gate)
- _STAFF_POST_OK is a subset of _MANAGER_POST_OK (inheritance invariant)
- Every endpoint name in either whitelist exists in app.url_map (typo guard)
"""
import os

# Mirror the bootstrap pattern from tests/test_bp_products_routes.py — must be
# set BEFORE any test imports `app` (the app module runs init_db at import
# time unless this is set).
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

# NOTE: `from app import …` is deferred into the fixture so that importing
# this test module during pytest collection does NOT eagerly import
# `commission` (and friends) with the live config.DATABASE_PATH bound.
# Other test modules monkeypatch config.DATABASE_PATH inside their fixtures;
# they assume `commission` hasn't already snapshotted the live path via
# `from config import DATABASE_PATH`. Importing `app` at collection time
# would break that assumption and cause cross-test ordering failures.


@pytest.fixture
def client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def whitelist_consts():
    """Returns (flask_app, _STAFF_POST_OK, _MANAGER_POST_OK, dashboard_url)."""
    from app import app as flask_app, _STAFF_POST_OK, _MANAGER_POST_OK
    from flask import url_for
    with flask_app.test_request_context():
        dashboard_url = url_for('dashboard')
    return flask_app, _STAFF_POST_OK, _MANAGER_POST_OK, dashboard_url


def _login_as(client, role: str):
    with client.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = role
        sess['role']     = role


def _redirects_to_dashboard(resp, dashboard_url: str) -> bool:
    """The permission-denied response is a 302 whose Location header equals
    the dashboard URL (currently '/'). Tolerate both relative and absolute
    Location values."""
    loc = resp.headers.get('Location') or ''
    return resp.status_code == 302 and (loc == dashboard_url or loc.endswith(dashboard_url))


def test_staff_blocked_from_admin_post(client, whitelist_consts):
    """Staff POSTing to an admin-only endpoint must redirect, not execute.

    /users/new (endpoint=user_new) is admin-only — NOT in either whitelist.
    The before_request gate fires before the route's inline abort(403),
    so we expect the dashboard redirect, not 403.
    """
    _, _, _, dashboard_url = whitelist_consts
    _login_as(client, 'staff')
    resp = client.post('/users/new', data={}, follow_redirects=False)
    assert resp.status_code in (302, 403)
    if resp.status_code == 302:
        assert _redirects_to_dashboard(resp, dashboard_url), (
            f"Expected redirect to dashboard, got Location={resp.headers.get('Location')!r}"
        )


def test_manager_blocked_from_admin_post(client, whitelist_consts):
    """Manager POSTing to /users/new (admin-only) must redirect, not execute."""
    _, _, _, dashboard_url = whitelist_consts
    _login_as(client, 'manager')
    resp = client.post('/users/new', data={}, follow_redirects=False)
    assert resp.status_code in (302, 403)
    if resp.status_code == 302:
        assert _redirects_to_dashboard(resp, dashboard_url), (
            f"Expected redirect to dashboard, got Location={resp.headers.get('Location')!r}"
        )


def test_manager_allowed_for_manager_endpoint(client, whitelist_consts):
    """Manager POST to a route in _MANAGER_POST_OK must NOT be permission-blocked.

    The route may still return 400 / 500 due to empty body, but we only care
    that the permission middleware doesn't redirect to the dashboard.
    """
    _, _, _, dashboard_url = whitelist_consts
    _login_as(client, 'manager')
    # unified_import (/import-data) is in _STAFF_POST_OK ⊆ _MANAGER_POST_OK.
    resp = client.post('/import-data', data={}, follow_redirects=False)
    if resp.status_code == 302:
        assert not _redirects_to_dashboard(resp, dashboard_url), (
            "Manager POST to whitelisted endpoint should not redirect to dashboard"
        )


def test_staff_blocked_from_hr_get(client, whitelist_consts):
    """Staff GET to any hr.* endpoint is blocked by the separate hr gate."""
    _, _, _, dashboard_url = whitelist_consts
    _login_as(client, 'staff')
    resp = client.get('/hr/', follow_redirects=False)
    if resp.status_code == 302:
        assert _redirects_to_dashboard(resp, dashboard_url), (
            f"Expected redirect to dashboard, got Location={resp.headers.get('Location')!r}"
        )
    else:
        pytest.fail(f"Staff reached /hr/ — got {resp.status_code} instead of redirect")


def test_staff_blocked_from_cashbook_get(client, whitelist_consts):
    """Staff GET to any cashbook.* endpoint is blocked by the separate cashbook gate."""
    _, _, _, dashboard_url = whitelist_consts
    _login_as(client, 'staff')
    resp = client.get('/cashbook/', follow_redirects=False)
    if resp.status_code == 302:
        assert _redirects_to_dashboard(resp, dashboard_url), (
            f"Expected redirect to dashboard, got Location={resp.headers.get('Location')!r}"
        )
    else:
        pytest.fail(f"Staff reached /cashbook/ — got {resp.status_code}")


def test_staff_whitelist_subset_of_manager(whitelist_consts):
    """Inheritance invariant: _STAFF_POST_OK ⊆ _MANAGER_POST_OK."""
    _, staff, manager, _ = whitelist_consts
    assert staff.issubset(manager), (
        f"Staff-only endpoints not in manager whitelist: {staff - manager}"
    )


def test_whitelist_endpoints_all_exist(whitelist_consts):
    """Every endpoint name in either whitelist must exist in app.url_map.

    A typo (e.g. 'product.product_location_save' missing the 's' in 'products')
    silently disables the gate for that endpoint. This test catches it.
    """
    flask_app, staff, manager, _ = whitelist_consts
    endpoints = {rule.endpoint for rule in flask_app.url_map.iter_rules()}
    missing_staff = staff - endpoints
    missing_mgr   = manager - endpoints
    assert not missing_staff, f"_STAFF_POST_OK references unknown endpoints: {missing_staff}"
    assert not missing_mgr,   f"_MANAGER_POST_OK references unknown endpoints: {missing_mgr}"


def test_module_first_endpoints_valid_and_data_uses_unified_import():
    """Every module-switcher first_endpoint must be a real endpoint, and the
    'data' module must open /import-data (unified_import) — NOT the retired
    /import-weekly (which the dropdown used to land on)."""
    from app import app as flask_app, _MODULE_DEFS
    endpoints = {r.endpoint for r in flask_app.url_map.iter_rules()}
    for m in _MODULE_DEFS:
        assert m['first_endpoint'] in endpoints, \
            f"module '{m['key']}' first_endpoint {m['first_endpoint']} is not a real endpoint"
    data = next(m for m in _MODULE_DEFS if m['key'] == 'data')
    assert data['first_endpoint'] == 'unified_import', \
        "data module switcher must open /import-data, not the retired /import-weekly"


def test_endpoint_module_keys_all_exist():
    """Every key in _ENDPOINT_MODULE must be a real endpoint. A stale key (e.g.
    'commission_overrides' when the endpoint is 'commission_overrides_list', or
    'audit_log' which has no route) makes the sidebar module-switcher silently
    fall back to the 'overview' highlight on that page. Guards against drift."""
    from app import app as flask_app, _ENDPOINT_MODULE
    endpoints = {rule.endpoint for rule in flask_app.url_map.iter_rules()}
    missing = sorted(k for k in _ENDPOINT_MODULE if k not in endpoints)
    assert not missing, f"_ENDPOINT_MODULE references unknown endpoints: {missing}"
