"""Integration tests for /cashflow (Phase 2 finance revamp, R1 — /cashflow
becomes pure "เงินเข้า", /ar owns all AR).

Uses tmp_db so route + cashflow.py + payments_alloc.py + template all run
against a real schema copy. Guards against template-variable rename
regressions that unit tests miss.

The customer-credit-balance section and the full AR-aging breakdown table
moved to /ar (see tests/test_ar_page.py) — /cashflow now only teases a
compact AR headline linking there, plus the merged Accrual/Cash/Gap table
(cashflow.cash_vs_revenue_by_month, replacing the two separate monthly
tables it used to render).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def route_db(tmp_db, monkeypatch):
    """Patch DATABASE_PATH on modules that captured it via
    `from config import DATABASE_PATH` at import time."""
    import payments_alloc, cashflow
    for mod in (payments_alloc, cashflow):
        monkeypatch.setattr(mod, 'DATABASE_PATH', tmp_db, raising=True)
    return tmp_db


def _client_as_admin():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'test-admin'
    return c


def _client_as_staff():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'staff'
        s['username'] = 'test-staff'
    return c


def test_cashflow_route_staff_role_blocked(route_db):
    c = _client_as_staff()
    resp = c.get('/cashflow', follow_redirects=False)
    # Existing admin/manager gate — must still 302 to /, never leaking
    # the page content.
    assert resp.status_code == 302


def test_cashflow_route_no_longer_shows_credit_section(route_db):
    """R1: the customer-credit-balance section moved to /ar?tab=reconcile."""
    c = _client_as_admin()
    resp = c.get('/cashflow')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'ยอดเครดิตลูกค้าค้างคืน' not in body, (
        "Customer-credit-balance section must no longer render on /cashflow "
        "— it moved to /ar?tab=reconcile."
    )


def test_cashflow_route_no_longer_shows_full_aging_table(route_db):
    """R1: the full AR-aging breakdown table moved to /ar; /cashflow keeps
    only a compact headline (total + as_of), not the per-bucket table."""
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    assert 'AR Aging — ยอดค้างจำแนกตามอายุ' not in body
    assert 'ช่วงอายุ (วัน)' not in body  # bucket-table column header


def test_cashflow_route_ar_headline_links_to_ar(route_db):
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    assert 'href="/ar"' in body, "Compact AR headline card must link to /ar"


def test_cashflow_route_shows_merged_accrual_cash_table(route_db):
    """R1: the two separate monthly tables (cash-in / accrual revenue) are
    replaced by ONE merged table with a gap column."""
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    assert 'เงินเข้า (Cash) เทียบรายได้ (Accrual) รายเดือน' in body
    assert 'ส่วนต่าง' in body
    # The old standalone "รายได้ตามเกณฑ์คงค้าง (Accrual) รายเดือน" card is gone
    # (superseded by the merged table above).
    assert 'รายได้ตามเกณฑ์คงค้าง (Accrual) รายเดือน' not in body


def test_cashflow_route_keeps_cash_in_bar_chart(route_db):
    """Plan explicitly says: keep the CSS bar chart on cash-in."""
    c = _client_as_admin()
    resp = c.get('/cashflow')
    body = resp.get_data(as_text=True)
    # The bar chart's distinctive inline-style bar div (cash-in green bar).
    assert 'background:#2e7d3a; border-radius:3px; height:100%' in body


# ── /cashflow serves the same authoritative snapshot → same freshness contract

def test_cashflow_page_warns_when_ar_snapshot_is_stale(route_db):
    import sqlite3
    conn = sqlite3.connect(route_db)
    changed = conn.execute(
        "UPDATE express_ar_outstanding SET snapshot_date_iso='2020-01-02'"
        " WHERE entity='BSN'").rowcount
    conn.commit()
    conn.close()
    assert changed > 0, 'live-clone fixture has no BSN AR rows to date-stamp'

    resp = _client_as_admin().get('/cashflow')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert 'ข้อมูลลูกหนี้เก่าเกิน 1 วัน' in body
    assert '2020-01-02' in body
    # the footnote must not claim the snapshot is today's number
    assert 'คำนวณ ณ วันปัจจุบัน' not in body


def test_cashflow_page_does_not_warn_when_ar_snapshot_is_fresh(route_db):
    """Control: the /cashflow banner is conditional, not decoration."""
    import sqlite3
    from datetime import date
    conn = sqlite3.connect(route_db)
    changed = conn.execute(
        "UPDATE express_ar_outstanding SET snapshot_date_iso=? WHERE entity='BSN'",
        (date.today().isoformat(),)).rowcount
    conn.commit()
    conn.close()
    assert changed > 0

    body = _client_as_admin().get('/cashflow').get_data(as_text=True)
    assert 'ข้อมูลลูกหนี้เก่าเกิน 1 วัน' not in body
