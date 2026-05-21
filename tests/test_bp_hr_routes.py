"""Route-level integration tests for bp_hr.

Uses tmp_db so route + models + templates execute against a live-DB clone
and never touch the real DB. Logs in as admin via session pre-population
because the hr before_request middleware in app.py blocks staff entirely
from hr.* endpoints.

Covers 3 GET endpoints: dashboard, employee detail, and leave list.
Payroll-list is also a candidate but the dashboard already exercises the
hrq.get_payroll_runs() call path, so leave_list (different hrq query
surface) gives broader coverage.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated. tmp_db
    must be pulled in first so config.DATABASE_PATH is monkeypatched
    before `from app import app` runs."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _first_employee_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM employees ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No employees in live DB clone")
    return row[0]


def test_hr_dashboard_renders(admin_client):
    """Headcount + on-leave + probation-ending + over-quota alerts —
    iterates over all active employees and computes leave balances, so
    this is the broadest hr-module smoke test."""
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200, resp.data[:500]


def test_hr_employee_detail_renders(admin_client, tmp_db):
    """Per-employee card with salary history + leave balance."""
    eid = _first_employee_id(tmp_db)
    resp = admin_client.get(f'/hr/employees/{eid}')
    assert resp.status_code == 200, resp.data[:500]


def test_hr_leave_list_renders(admin_client):
    """Leave-request list with employee/month/type filter dropdowns."""
    resp = admin_client.get('/hr/leave')
    assert resp.status_code == 200, resp.data[:500]


# ── stale-draft banner on /hr/ dashboard ─────────────────────────────────

def test_hr_dashboard_shows_stale_draft_banner(admin_client, tmp_db):
    """Insert a draft payroll run for a past month → banner copy renders.
    Use a year_month that is unambiguously past (2024-01) regardless of
    when the test runs."""
    sqlite3.connect(tmp_db).execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, created_by)
           VALUES ('2024-01', 1, 'draft', '2024-01-31', 1)"""
    ).connection.commit()
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200
    assert b'payroll run' in resp.data and 'draft' in resp.data.decode('utf-8')
    # Banner-specific copy
    assert 'ค้าง draft' in resp.data.decode('utf-8'), \
        "stale-draft banner copy missing from dashboard"


def test_hr_dashboard_no_banner_when_only_current_month_draft(admin_client, tmp_db):
    """A draft for the CURRENT month is normal mid-prep, must NOT trigger
    the banner. Use a date-derived year_month so the test is date-stable."""
    from datetime import date
    this_ym = date.today().strftime("%Y-%m")
    # Clean slate first so live-DB clone state can't pollute
    conn = sqlite3.connect(tmp_db)
    conn.execute("DELETE FROM payroll_runs")
    conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, created_by)
           VALUES (?, 1, 'draft', date('now'), 1)""", (this_ym,)
    )
    conn.commit()
    conn.close()
    resp = admin_client.get('/hr/')
    assert resp.status_code == 200
    assert 'ค้าง draft' not in resp.data.decode('utf-8'), \
        "current-month draft should NOT trigger stale-banner"
