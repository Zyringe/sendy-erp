"""Cashbook Phase 2 render tests — the month-scoped dashboard template.

Phase 1 (test_cashbook_month_scope.py) only checked the kwargs the route
computes (render_template was monkeypatched out, so the template was never
actually rendered). This file is the first to render the real HTML through
the templating engine — the realistic failure mode for a template-only
phase is a Jinja error (e.g. an `|first` on an unmatched selectattr, a
missing context key), not a URL-map/500 issue.

Covers: the month `<select>` picker, the card-3 label swap (สุทธิเดือนนี้ vs
คงเหลือ), the per-category overspend ▲ badge + "ใหม่" chip + the รายจ่ายรวม
roll-up badge, and the "เดือนยังไม่จบ" MTD tag — across explicit month,
ทั้งหมด (all-time), no-param (default), and current-month entry points.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from datetime import date

import pytest

from tests.test_cashbook_month_scope import (
    _seed_accounts, _insert_txns, _seed_overspend_scenario, _seed_via_path,
)


@pytest.fixture
def admin_client(tmp_db):
    """Same pattern as test_bp_cashbook_routes.py: admin session pre-populated
    so the cashbook before_request gate (staff-blocked) doesn't 403 us."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _seed_current_month_scenario(conn):
    """A single expense txn dated today, so the real current calendar month
    has data and `is_current_month` resolves True for whatever date the
    test suite actually runs on."""
    _seed_accounts(conn)
    today_str = date.today().strftime('%Y-%m-%d')
    _insert_txns(conn, [(1, today_str, 'expense', 'ค่าไฟ', None, 100.0)])


def test_dashboard_month_mode_renders_net_label_and_overspend_badges(admin_client, tmp_db):
    """Feb 2026 (seeded by _seed_overspend_scenario): ค่าไฟ 8000->10000
    (+25%/+2000) is flagged, ค่าเช่า is new (absent in Jan). Both the
    picker and the two badge shapes must render without a Jinja error."""
    _seed_via_path(tmp_db, _seed_overspend_scenario)
    resp = admin_client.get('/cashbook/?month=2026-02')
    assert resp.status_code == 200, resp.data[:1000]
    html = resp.data.decode('utf-8')

    assert 'name="month"' in html
    assert 'สุทธิเดือนนี้' in html
    assert '▲25%' in html            # ค่าไฟ flagged-category badge
    assert 'ใหม่' in html            # ค่าเช่า is_new chip
    assert '▲1 หมวดบวม' in html      # รายจ่ายรวม roll-up (only ค่าไฟ flagged)


def test_dashboard_all_time_mode_renders_balance_label_not_net(admin_client, tmp_db):
    _seed_via_path(tmp_db, _seed_overspend_scenario)
    resp = admin_client.get('/cashbook/?month=ทั้งหมด')
    assert resp.status_code == 200, resp.data[:1000]
    html = resp.data.decode('utf-8')

    assert 'name="month"' in html
    assert 'คงเหลือ' in html
    assert 'สุทธิเดือนนี้' not in html
    assert 'เดือนยังไม่จบ' not in html


def test_dashboard_default_month_no_query_param_renders(admin_client, tmp_db):
    """No ?month= at all -> resolves to the most-recent month with data
    (2026-02 for this seed) and still renders cleanly."""
    _seed_via_path(tmp_db, _seed_overspend_scenario)
    resp = admin_client.get('/cashbook/')
    assert resp.status_code == 200, resp.data[:1000]
    html = resp.data.decode('utf-8')
    assert 'name="month"' in html


def test_dashboard_current_month_shows_incomplete_tag(admin_client, tmp_db):
    _seed_via_path(tmp_db, _seed_current_month_scenario)
    month = date.today().strftime('%Y-%m')
    resp = admin_client.get(f'/cashbook/?month={month}')
    assert resp.status_code == 200, resp.data[:1000]
    html = resp.data.decode('utf-8')
    assert 'เดือนยังไม่จบ' in html
    assert 'สุทธิเดือนนี้' in html
