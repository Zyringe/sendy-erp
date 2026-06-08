"""Route-level tests for the /conversions pages (app.py).

Renders against a live-DB clone (tmp_db) which carries the 110 pack/unpack
formulas, so the buildable column + url_for('conversion_pair') + the run-page
write-off block all execute on a real page.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _two_active_pids(tmp_db):
    rows = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM products WHERE is_active=1 ORDER BY id DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        pytest.skip("need 2 active products")
    return rows[0][0], rows[1][0]


def _first_active_formula(tmp_db):
    r = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM conversion_formulas WHERE is_active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if r is None:
        pytest.skip("no active formula in clone")
    return r[0]


def test_conversions_list_renders(admin_client):
    # exercises the buildable column + url_for('conversion_pair') in the list
    resp = admin_client.get('/conversions')
    assert resp.status_code == 200, resp.data[:500]
    assert 'จับคู่แพ็ค-ตัวหลวม'.encode() in resp.data


def test_conversion_pair_form_renders(admin_client):
    resp = admin_client.get('/conversions/pair')
    assert resp.status_code == 200, resp.data[:500]


def test_conversion_run_page_has_writeoff(admin_client, tmp_db):
    fid = _first_active_formula(tmp_db)
    resp = admin_client.get(f'/conversions/{fid}/run')
    assert resp.status_code == 200, resp.data[:500]
    assert 'ของเสีย'.encode() in resp.data            # write-off block present


def test_conversion_pair_post_creates_pair(admin_client, tmp_db):
    pack_id, loose_id = _two_active_pids(tmp_db)
    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pack_id), 'loose_id': str(loose_id),
        'ratio': '2', 'direction': 'both', 'note': 'route-test',
    }, follow_redirects=False)
    assert resp.status_code == 302
    # a pack formula (output=pack_id) now exists
    n = sqlite3.connect(tmp_db).execute(
        "SELECT COUNT(*) FROM conversion_formulas WHERE output_product_id=? AND is_active=1", (pack_id,)
    ).fetchone()[0]
    assert n >= 1


def test_conversion_pair_post_rejects_same_product(admin_client, tmp_db):
    pid, _ = _two_active_pids(tmp_db)
    resp = admin_client.post('/conversions/pair', data={
        'pack_id': str(pid), 'loose_id': str(pid), 'ratio': '2', 'direction': 'both',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert 'ต้องไม่ใช่ตัวเดียวกัน'.encode() in resp.data
