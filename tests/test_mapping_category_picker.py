"""Render-guard for the /mapping searchable-dropdown (combo) component (2026-06-15).

All pick-list fields on both the approve form and the Suggest modal use one
reusable type-to-search combo fed by COMBO_OPTS (Category, Brand, Color,
Packaging, Unit type, Condition). These tests assert the route renders (no
Jinja error in the combo data/JS) and that the combo markup reaches both forms.

The two forms only render when there is work to show, so we seed a pending
mapping (→ Suggest modal) and a pending suggestion (→ approve form) first.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def manager_client(tmp_db):
    # Seed one pending mapping (surfaces the Suggest modal w/ Card B combos)
    # and one pending suggestion (surfaces the approve-form combos).
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id, bsn_unit, is_ignored) "
        "VALUES ('ZZTEST01', 'combo render test', NULL, '', 0)"
    )
    conn.execute(
        "INSERT INTO pending_product_suggestions (bsn_code, bsn_name, status, created_at) "
        "VALUES ('ZZTEST02', 'combo suggestion test', 'pending', datetime('now'))"
    )
    conn.commit()
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c


def test_mapping_tab_renders_combo(manager_client):
    """The ผูกรหัส (Suggest modal) tab renders the combo infra + Card B combos."""
    body = manager_client.get('/mapping').get_data(as_text=True)
    assert 'COMBO_OPTS' in body
    assert 'function initCombo' in body
    for key in ('categories:', 'brands:', 'colorcodes:', 'packaging:', 'units:', 'conditions:'):
        assert key in body, f'missing COMBO_OPTS.{key}'
    assert 'chemical' in body          # category option set populated (code is the hint, ASCII)
    # Card B fields are combos wired to their value hiddens
    assert 'data-opts="categories"' in body
    assert 'id="sm-cat-id"' in body
    assert 'data-opts="brands"' in body
    assert 'data-opts="packaging"' in body
    assert 'data-opts="units"' in body


def test_suggestions_tab_renders_combo(manager_client):
    """The approve-SKU tab renders the same combos (per-row)."""
    resp = manager_client.get('/mapping?tab=suggestions')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'COMBO_OPTS' in body
    assert 'class="combo"' in body
    assert 'data-opts="categories"' in body
    assert 'data-opts="brands"' in body


def test_old_datalist_is_gone(manager_client):
    """The #146 datalist approach was replaced — it must not linger."""
    body = manager_client.get('/mapping?tab=suggestions').get_data(as_text=True)
    assert 'cat-datalist' not in body
    assert 'CAT_BY_NAME' not in body
