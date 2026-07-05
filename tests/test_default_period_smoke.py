"""Default-period smoke tests for model readers that compute their own date
range when called with no arguments (get_trade_dashboard, get_accounting_summary).

Why this exists: the Phase-12 models split moved these functions into
models/sales.py and models/accounting.py, whose new module headers dropped
`from datetime import date`. The split's body-verbatim gate (AST hash compare)
cannot see a missing header import, and no existing test exercised the no-args
default-dates branch — the whole suite stayed green while /trade-dashboard
500'd (NameError) in the running app; caught only by an authed-render
spot-check. These tests pin the exact failing paths.

Run: cd sendy_erp && ~/.virtualenvs/erp/bin/pytest tests/test_default_period_smoke.py -q
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models


def _login(client, role='admin', user_id=1):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['username'] = f'test-{role}'
        sess['role'] = role


@pytest.fixture
def client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as c:
        yield c


def test_trade_dashboard_default_period_renders(client):
    """No query args -> get_trade_dashboard(None, None) -> date.today() path."""
    _login(client, 'admin')
    resp = client.get('/trade-dashboard')
    assert resp.status_code == 200


def test_get_trade_dashboard_no_args(tmp_db):
    stats = models.get_trade_dashboard(None, None)
    assert stats is not None


def test_get_accounting_summary_date_from_only(tmp_db):
    """date_from-without-date_to branch fills date_to via date.today().isoformat().

    (The no-args branch only reaches date.today() on an EMPTY sales table —
    with live-DB-copy fixtures the `latest` row short-circuits it — so this
    date_from-only call is the branch that actually pins the missing import.)"""
    summary = models.get_accounting_summary(date_from='2026-01-01')
    assert summary is not None
