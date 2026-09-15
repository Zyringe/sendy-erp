"""#542 — staff is blocked from the whole commission module (every
`commission.*` endpoint, GET and POST), the same way `require_login` already
blocks staff from `hr.*` / `cashbook.*` / `naming.*` (access_control.py).

Also pins that no commission.* link renders for staff in the desktop sidebar
or the mobile drawer — both already render from `nav.py::nav_sections(role)`,
and the only commission.* NAV entry lives in the 'การเงิน' section, which is
`roles={'admin', 'manager', 'shareholder'}` (staff excluded) — so this is a
pin, not a fix, but the issue names it as an explicit acceptance criterion.
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


def _login_as(client, role: str):
    with client.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = role
        sess['display_name'] = f'Test {role.title()}'
        sess['role']     = role


def _dashboard_url():
    from app import app as flask_app
    from flask import url_for
    with flask_app.test_request_context():
        return url_for('dashboard')


def _redirects_to_dashboard(resp, dashboard_url: str) -> bool:
    loc = resp.headers.get('Location') or ''
    return resp.status_code == 302 and (loc == dashboard_url or loc.endswith(dashboard_url))


COMMISSION_GET_ENDPOINTS = [
    '/commission',
    '/commission/payouts',
    '/commission/sp/06',   # "one drilldown" — the before_request gate fires
                            # before the route ever looks up sp_code, so any
                            # value proves the gate (no fixture required).
]


@pytest.mark.parametrize('path', COMMISSION_GET_ENDPOINTS)
def test_staff_blocked_from_commission_get(client, path):
    dashboard_url = _dashboard_url()
    _login_as(client, 'staff')
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 302, f"staff reached {path} — got {resp.status_code} instead of redirect"
    assert _redirects_to_dashboard(resp, dashboard_url), (
        f"expected redirect to dashboard for {path}, got Location={resp.headers.get('Location')!r}"
    )
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    assert any('คอมมิชชั่น' in msg for _cat, msg in flashes), (
        f"expected a commission-specific no-access flash for {path}, got {flashes!r}"
    )


def test_staff_blocked_from_commission_post(client):
    """POST to the admin-only delete-payout endpoint: staff must be redirected
    by the commission.* gate, never reach the route (which would otherwise
    403 via the generic POST-whitelist message, not this specific one)."""
    dashboard_url = _dashboard_url()
    _login_as(client, 'staff')
    resp = client.post('/commission/payout/1/delete', data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert _redirects_to_dashboard(resp, dashboard_url)
    with client.session_transaction() as sess:
        flashes = sess.get('_flashes', [])
    assert any('คอมมิชชั่น' in msg for _cat, msg in flashes)


@pytest.mark.parametrize('role', ['admin', 'manager'])
def test_admin_and_manager_still_reach_commission_dashboard(client, role):
    """Control: the new gate must not over-block roles that already have
    access — admin and manager both open /commission fine today."""
    _login_as(client, role)
    resp = client.get('/commission', follow_redirects=False)
    assert resp.status_code == 200, f"{role} could not open /commission — got {resp.status_code}"


def test_no_commission_link_for_staff_in_sidebar_or_drawer(client):
    """Neither nav surface (base.html's sidebar_sections loop, or
    _mobile_drawer.html's drawer_sections loop) emits a commission.* href for
    staff. Control: the SAME page carries the trade-dashboard link (always
    visible to staff), proving the nav actually rendered rather than the
    whole block being empty for an unrelated reason."""
    _login_as(client, 'staff')
    html = client.get('/', follow_redirects=True).get_data(as_text=True)
    assert 'href="/commission' not in html
    assert 'href="/trade-dashboard"' in html, (
        "control: staff's own nav must still render other links on this page"
    )


def test_commission_link_visible_to_manager_in_sidebar(client):
    """Positive control for the previous test: a role that SHOULD see the
    commission link does — so 'absent for staff' isn't just 'absent for
    everyone' (e.g. a broken NAV entry)."""
    _login_as(client, 'manager')
    html = client.get('/', follow_redirects=True).get_data(as_text=True)
    assert 'href="/commission"' in html
