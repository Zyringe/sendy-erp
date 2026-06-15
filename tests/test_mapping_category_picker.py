"""Render-guard for the /mapping category picker (2026-06-15).

The category field on both the approve form and the Suggest modal moved from a
free-text input to a type-to-search datalist fed by the `categories` master.
These tests assert the route renders (no Jinja error in the datalist / JS
name→id map) and that real categories reach the page.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest


@pytest.fixture
def manager_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c


def test_mapping_tab_renders_category_datalist(manager_client):
    """The ผูกรหัส (Suggest modal) tab must render the shared datalist and the
    CAT_BY_NAME map without a template error."""
    resp = manager_client.get('/mapping')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="cat-datalist"' in body
    assert 'CAT_BY_NAME' in body
    # a known seeded category must appear as a datalist option value
    assert 'สารเคมี / น้ำยา / โซดาไฟ' in body


def test_suggestions_tab_renders_category_datalist(manager_client):
    """The approve-SKU tab must also render the picker."""
    resp = manager_client.get('/mapping?tab=suggestions')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="cat-datalist"' in body
    assert 'list="cat-datalist"' in body  # the sug-cat input is wired to it
