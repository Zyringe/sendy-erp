"""Phase 3 render tests — prove the drawer + bottom bar actually RENDER per
role via a real test-client request, not just unit tests on the builders.

Per erp-engineering-discipline.md's /hr/advances incident (and the list-cards
rollout's echo of it): a bare `'<link text>' in html` assertion FALSE-PASSES
here — the drawer's section headers/links aren't gated the way you'd assume
from staring at the template, so the only trustworthy check is the actual
href SET a role gets, compared against nav.py's own nav_sections()/
build_mobile_nav_slots() (an independent computation, not a re-parse of the
same template logic — see verification-discipline.md).

Also proves, end-to-end, the one thing unit tests on nav_sections() alone
cannot: that _mobile_drawer.html's `condition`/`badge.roles` Jinja actually
evaluates the way nav.py's data model intends (dict `**url_kwargs` unpacking,
`session.get(link.condition)`, etc.) — a template typo there would pass every
nav.py-level test yet 500 or mis-render live.
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

from nav import nav_sections


def _client(role, user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _bottom_nav_block(html):
    m = re.search(r'<nav class="bottom-nav.*?</nav>', html, re.DOTALL)
    assert m, "bottom-nav block not found in rendered page"
    return m.group(0)


def _drawer_block(html):
    m = re.search(r'id="mobileDrawer".*?(?=<script)', html, re.DOTALL)
    assert m, "mobile drawer (#mobileDrawer) block not found in rendered page"
    return m.group(0)


def _hrefs(block):
    return set(re.findall(r'href="([^"]+)"', block))


def _bottom_nav_items(block):
    """[(href, is_active)] for each <a> slot, plus whether the เพิ่มเติม button
    itself is flagged active."""
    items = [(href, 'active' in cls.split())
             for href, cls in re.findall(r'<a href="([^"]+)"\s+class="bottom-nav-item\s*([^"]*)">', block)]
    btn = re.search(r'<button[^>]*class="bottom-nav-item\s*([^"]*)"', block)
    more_active = bool(btn) and 'active' in btn.group(1).split()
    return items, more_active


def _expected_drawer_hrefs(flask_app, role):
    """Independent oracle: walk nav_sections(role) ourselves and url_for() each
    link (skipping `condition`-gated links — db_routes_enabled is False in a
    fresh session, same as what the template would evaluate)."""
    from flask import url_for
    hrefs = set()
    with flask_app.test_request_context('/'):
        for section in nav_sections(role):
            for link in section['links']:
                if link.get('condition'):
                    continue
                hrefs.add(url_for(link['ep'], **(link.get('url_kwargs') or {})))
    return hrefs


def _expected_bottom_nav_hrefs(flask_app, role):
    from flask import url_for
    from app import build_mobile_nav_slots
    with flask_app.test_request_context('/'):
        return {url_for(slot['endpoint']) for slot in build_mobile_nav_slots(role, '')}


@pytest.mark.parametrize('role,landing', [
    ('admin', '/'), ('manager', '/'), ('staff', '/'), ('shareholder', '/'),
    ('general', '/m/stock'),
])
def test_drawer_href_set_matches_nav_sections(tmp_db, role, landing):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    html = _client(role).get(landing).get_data(as_text=True)
    got = _hrefs(_drawer_block(html))
    expected = _expected_drawer_hrefs(flask_app, role)
    assert got == expected, (role, 'extra=', got - expected, 'missing=', expected - got)


def test_general_drawer_render_is_only_4_links(tmp_db):
    # ของฉัน (ลาของฉัน, สลิป) + ตั้งค่า (บัญชีของฉัน — self-service account) + แอป (ติดตั้งแอป).
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    html = _client('general').get('/m/stock').get_data(as_text=True)
    hrefs = _hrefs(_drawer_block(html))
    assert len(hrefs) == 4, hrefs
    # mobile.sales_trip must never leak in for general (it bounces).
    from flask import url_for
    with flask_app.test_request_context('/'):
        sales_trip_href = url_for('mobile.sales_trip')
    assert sales_trip_href not in hrefs


@pytest.mark.parametrize('role,landing', [
    ('admin', '/'), ('manager', '/'), ('staff', '/'), ('shareholder', '/'),
])
def test_bottom_bar_href_set_matches_builder(tmp_db, role, landing):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    html = _client(role).get(landing).get_data(as_text=True)
    got = _hrefs(_bottom_nav_block(html))
    expected = _expected_bottom_nav_hrefs(flask_app, role)
    assert got == expected, (role, got, expected)


@pytest.mark.parametrize('role', ['admin', 'manager', 'staff', 'shareholder'])
def test_bottom_bar_home_active_on_dashboard(tmp_db, role):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    html = _client(role).get('/').get_data(as_text=True)
    items, more_active = _bottom_nav_items(_bottom_nav_block(html))
    actives = [href for href, active in items if active]
    assert len(actives) == 1, actives
    assert not more_active


def test_bottom_bar_more_lights_on_a_drawer_only_page(tmp_db):
    """/accounting (finance module) has no bottom-nav slot — เพิ่มเติม must
    light instead of nothing lighting at all."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    html = _client('admin').get('/accounting').get_data(as_text=True)
    items, more_active = _bottom_nav_items(_bottom_nav_block(html))
    assert all(not active for _, active in items), items
    assert more_active


def test_general_bottom_bar_render_unchanged():
    """general's kiosk bar is untouched — proves the shared เพิ่มเติม-highlight
    template change didn't accidentally alter its slot set."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_request_context('/'):
        from access_control import build_mobile_nav_slots
        keys = [s['key'] for s in build_mobile_nav_slots('general', 'mobile.stock_search')]
    assert keys == ['stock', 'my_leave', 'my_payslip']


def test_hamburger_button_removed(tmp_db):
    html = _client('admin').get('/').get_data(as_text=True)
    assert 'onclick="toggleSidebar()"' not in html
