"""Dedicated badge-rendering tests for the Phase 4 desktop sidebar port.

tests/test_nav_snapshot.py's snapshots deliberately SKIP badge <span>s when
parsing (their counts are live DB values, so folding them into a snapshot would
make it non-deterministic — see that file's `_SidebarParser` docstring). That
means a badge regression (wrong role gate, wrong count, wrong CSS class) passes
snapshots A and B silently. These tests are the independent check that badges
were ported faithfully: role-gated visibility (bsn.mapping's
pending_suggestions_count, base.html:268's `is_manager` = admin/manager,
EXCLUDING shareholder) and the un-gated alert/suspicious badges.

Counts are monkeypatched to fixed nonzero values rather than relying on
whatever the live-DB copy happens to hold — a real count of 0 would hide the
badge regardless of role and mask a broken role gate.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


def _client(role, user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


@pytest.fixture
def fixed_counts(monkeypatch):
    """Pin alert_count=5, suspicious_count=7, pending_suggestions_count=3 —
    deterministic, nonzero, and distinguishable from each other in assertions."""
    import access_control
    monkeypatch.setattr(access_control.models, 'count_stock_alerts', lambda: 5)
    monkeypatch.setattr(access_control.rr, 'suspicious_count', lambda **kwargs: 7)
    monkeypatch.setattr(access_control.models, 'count_pending_suggestions', lambda: 3)


def test_alert_and_suspicious_badges_render_ungated(tmp_db, fixed_counts):
    """alert_count/suspicious_count have no role gate in base.html — any
    logged-in role sees them, styled `badge-count` (not bsn.mapping's danger
    pill)."""
    html = _client('admin').get('/').get_data(as_text=True)
    assert '<span class="badge-count">5</span>' in html   # แจ้งเตือน
    assert '<span class="badge-count">7</span>' in html   # ตรวจบิล


@pytest.mark.parametrize('role,should_show', [
    ('admin', True), ('manager', True), ('shareholder', False), ('staff', False),
])
def test_pending_suggestions_badge_role_gated(tmp_db, fixed_counts, role, should_show):
    """base.html:268 — `{% if is_manager and pending_suggestions_count %}` —
    is_manager there means admin/manager ONLY (excludes shareholder, the exact
    landmine nav.py's badge.roles model exists to prevent leaking)."""
    html = _client(role).get('/mapping').get_data(as_text=True)
    has_badge = '<span class="badge bg-danger ms-auto">3</span>' in html
    assert has_badge == should_show, (role, html.count('pending') if not has_badge else 'present')


def test_pending_suggestions_badge_zero_count_never_shows(tmp_db, monkeypatch):
    import access_control
    monkeypatch.setattr(access_control.models, 'count_pending_suggestions', lambda: 0)
    html = _client('admin').get('/mapping').get_data(as_text=True)
    assert 'badge bg-danger ms-auto' not in html
