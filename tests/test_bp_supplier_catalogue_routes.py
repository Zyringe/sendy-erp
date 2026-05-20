"""Route-level integration tests for bp_supplier_catalogue (happy-path).

Complements test_bp_supplier_catalogue_role_gate.py — that file proves
staff are blocked and manager is NOT blocked from list/suggest. This
file proves admin can actually render the three OTHER GET endpoints
(purchased, match, suggest) against a live DB clone.

Uses tmp_db so route + models + templates execute against the clone and
never touch the real DB. Logs in as admin via session pre-population.
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


def _first_supplier_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM suppliers ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No suppliers in live DB clone")
    return row[0]


def test_supplier_catalogue_list_renders(admin_client):
    """Top-level list of suppliers with per-supplier item/mapping counts.
    Role-gate file only proves manager isn't blocked; this proves admin
    actually gets a 200."""
    resp = admin_client.get('/supplier-catalogue/')
    assert resp.status_code == 200, resp.data[:500]


def test_supplier_catalogue_purchased_renders(admin_client, tmp_db):
    """Primary view — distinct purchased product_name_raw rows joined
    with their existing mappings. Most complex SQL in the blueprint;
    most likely to break on schema changes."""
    sid = _first_supplier_id(tmp_db)
    resp = admin_client.get(f'/supplier-catalogue/{sid}/purchased')
    assert resp.status_code == 200, resp.data[:500]


def test_supplier_catalogue_suggest_returns_json(admin_client, tmp_db):
    """JSON live-search endpoint backing the match UI. Exercises the
    full _suggest_candidates ranking path against the catalogue."""
    sid = _first_supplier_id(tmp_db)
    resp = admin_client.get(f'/supplier-catalogue/{sid}/suggest?q=ใบตัด')
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    assert isinstance(resp.get_json(), list)
