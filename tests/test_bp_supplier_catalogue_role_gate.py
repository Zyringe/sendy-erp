"""Role-gate tests for bp_supplier_catalogue.

Closes #47 — staff must not be able to reach any supplier_catalogue route
because every view returns procurement-cost-sensitive data
(list_price / net_cash_price).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def client_factory(tmp_db):
    """Factory that returns a Flask test client logged in as a given role."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True

    def _make(role: str):
        c = flask_app.test_client()
        with c.session_transaction() as sess:
            sess['user_id']  = 1
            sess['username'] = f'test-{role}'
            sess['role']     = role
        return c

    return _make


def _redirects_to_dashboard(resp) -> bool:
    loc = resp.headers.get('Location') or ''
    return resp.status_code == 302 and (loc == '/' or loc.endswith('/'))


# Every route in bp_supplier_catalogue — the gate must cover all of them.
_ROUTES_TO_GATE = [
    ('GET',  '/supplier-catalogue/'),
    ('GET',  '/supplier-catalogue/1/purchased'),
    ('GET',  '/supplier-catalogue/1/match'),
    ('GET',  '/supplier-catalogue/1/suggest'),
    ('POST', '/supplier-catalogue/1/mapping/save'),
    ('POST', '/supplier-catalogue/1/mapping/1/delete'),
]


@pytest.mark.parametrize("method,path", _ROUTES_TO_GATE)
def test_staff_blocked_from_supplier_catalogue(client_factory, method, path):
    """Staff role must never reach a bp_supplier_catalogue route — pricing data."""
    c = client_factory('staff')
    resp = c.open(path, method=method, follow_redirects=False)
    assert _redirects_to_dashboard(resp), (
        f"Staff reached {method} {path} — got {resp.status_code} "
        f"{resp.headers.get('Location')!r}"
    )


@pytest.mark.parametrize("method,path", [
    ('GET', '/supplier-catalogue/'),
    ('GET', '/supplier-catalogue/1/suggest'),
])
def test_manager_can_reach_supplier_catalogue(client_factory, method, path):
    """Manager must reach the routes (or hit a downstream 404 / 200) —
    the gate redirects staff, NOT manager."""
    c = client_factory('manager')
    resp = c.open(path, method=method, follow_redirects=False)
    # Anything other than the dashboard redirect is fine. Downstream may
    # 200 / 404 depending on whether supplier_id=1 exists.
    assert not _redirects_to_dashboard(resp), (
        f"Manager wrongly blocked from {method} {path} — got dashboard redirect"
    )
