"""Happy-path integration test for the /revenue dashboard route.

Uses tmp_db (live-DB copy) so the template + route + revenue.py + cashflow.py
all execute against real data. Guards against template-variable rename
regressions that unit tests miss.

Sets SKIP_DB_INIT=1 at module load so importing `app` doesn't re-run migrations
against an unrelated file (the route reads config.DATABASE_PATH at request
time, which the tmp_db fixture has already monkeypatched to the temp copy).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def route_db(tmp_db, monkeypatch):
    """Extend tmp_db with DATABASE_PATH patches on modules that captured the
    constant via `from config import DATABASE_PATH` at import time.

    Required for route-level tests that don't pass conn= explicitly.
    """
    import payments_alloc, cashflow, revenue
    for mod in (payments_alloc, cashflow, revenue):
        monkeypatch.setattr(mod, 'DATABASE_PATH', tmp_db, raising=True)
    return tmp_db


def _client_as_admin():
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'admin'
        s['username'] = 'test-admin'
    return c


def test_revenue_route_renders_200_with_key_markers(route_db):
    c = _client_as_admin()
    r = c.get('/revenue')
    assert r.status_code == 200, r.data[:500]

    body = r.data.decode('utf-8', errors='replace')
    # Page chrome
    assert 'รายได้ (BSN)' in body
    # KPI card labels
    assert 'รายได้รวมช่วงนี้' in body
    assert 'จำนวนใบกำกับ'   in body
    assert 'ลูกค้าที่ซื้อ'    in body
    # Comparison + breakdown sections
    assert 'Accrual' in body and 'Cash' in body          # accrual-vs-cash card
    assert 'Top 20 ลูกค้า'    in body or 'ลูกค้าที่ทำรายได้สูงสุด' in body
    assert 'Top 10 แบรนด์'   in body or 'แบรนด์ที่ทำรายได้สูงสุด'  in body


def test_revenue_route_period_filter_applied(route_db):
    c = _client_as_admin()
    r = c.get('/revenue?from=2026-01&to=2026-01')
    assert r.status_code == 200
    body = r.data.decode('utf-8', errors='replace')
    # Disclaimer/footer reflects the filtered range
    assert '2026-01-01' in body
    assert '2026-01-31' in body


def test_revenue_route_blocks_non_admin_manager(route_db):
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'staff'
        s['username'] = 'test-staff'
    r = c.get('/revenue', follow_redirects=False)
    # Same gating as /cashflow — bounce to dashboard (302)
    assert r.status_code in (302, 303)


# ── /revenue/unmapped drill-down ─────────────────────────────────────────────

def test_revenue_unmapped_route_renders(route_db):
    c = _client_as_admin()
    r = c.get('/revenue/unmapped')
    assert r.status_code == 200
    body = r.data.decode('utf-8', errors='replace')
    assert 'ไม่ระบุแบรนด์' in body or 'ยังไม่ได้ map' in body
    assert 'unmapped' in body or 'no brand' in body  # source badges


def test_revenue_unmapped_blocks_non_admin_manager(route_db):
    from app import app
    c = app.test_client()
    with c.session_transaction() as s:
        s['role'] = 'staff'
    r = c.get('/revenue/unmapped', follow_redirects=False)
    assert r.status_code in (302, 303)


def test_revenue_unmapped_respects_limit_param(route_db):
    c = _client_as_admin()
    r = c.get('/revenue/unmapped?limit=5')
    assert r.status_code == 200
    body = r.data.decode('utf-8', errors='replace')
    assert 'value="5"' in body  # limit input prepopulated


def test_revenue_unmapped_tolerates_malformed_to_param(route_db):
    """A malformed ?to= param must NOT 500 — same defensive behavior as
    /revenue. _month_end() now falls back instead of raising ValueError."""
    c = _client_as_admin()
    for bad in ('foobar', '2026-13', 'NaN-99', ''):
        r = c.get(f'/revenue/unmapped?from=2026-01&to={bad}')
        assert r.status_code == 200, (
            f"to={bad!r} returned {r.status_code} — should not 500"
        )
