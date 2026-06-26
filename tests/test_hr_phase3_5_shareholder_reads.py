"""Phase 3.5 — shareholder reads all finance/cost pages, but still writes nothing.

The inline ('admin','manager') gates excluded shareholder (fail-closed). This
opens the GET reads; the POST default-deny (Phase 3) keeps shareholder
write-blocked, which the regression test below asserts.
"""
import sqlite3
import pytest


def _client_as(role, tmp_db):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


# ── shareholder can now READ the finance/cost pages ───────────────────────────
SHAREHOLDER_READS = [
    '/accounting',                  # accounting_summary (cost/GP) — was manager-gated
    '/cashflow',                    # cashflow_dashboard — was manager-gated
    '/revenue',                     # revenue_dashboard — was manager-gated
    # NOTE: /accounting/ar-followup is a redirect stub → /ar (open to all roles),
    # so it's not a useful gate test. The three above prove the sweep.
]

@pytest.mark.parametrize('path', SHAREHOLDER_READS)
def test_shareholder_can_read_finance(path, tmp_db):
    c = _client_as('shareholder', tmp_db)
    assert c.get(path).status_code == 200, f'shareholder GET {path} should be 200'


# ── REGRESSION: shareholder still writes NOTHING (the critical guard) ──────────
SHAREHOLDER_POST_DENIED = [
    '/mapping/save',
    '/stock-adjust',
    '/accounting/ar-followup/log/new',
]

@pytest.mark.parametrize('path', SHAREHOLDER_POST_DENIED)
def test_shareholder_still_cannot_write(path, tmp_db):
    c = _client_as('shareholder', tmp_db)
    r = c.post(path, data={})
    assert r.status_code in (302, 403), f'shareholder POST {path} should be denied, got {r.status_code}'
