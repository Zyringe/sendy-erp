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


# ── pair-aware delete: deleting one half must not silently orphan the other ────

def _seed_pair(tmp_db, pack_pid, loose_pid, direction='both'):
    """Insert two fresh products + a pack/unpack pair on the temp DB; return the
    (pack_formula_id, loose_formula_id) — loose_formula_id is None for one-way."""
    import models
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                 (pack_pid, f"TESTPACK{pack_pid}", "แผง"))
    conn.execute("INSERT INTO products (id, product_name, unit_type, is_active) VALUES (?,?,?,1)",
                 (loose_pid, f"TESTLOOSE{loose_pid}", "ตัว"))
    conn.commit(); conn.close()
    models.upsert_pack_unpack_pair(pack_pid, loose_pid, 2, direction)
    conn = sqlite3.connect(tmp_db)
    rows = conn.execute(
        "SELECT id, output_product_id FROM conversion_formulas "
        "WHERE output_product_id IN (?,?) AND is_active=1", (pack_pid, loose_pid)).fetchall()
    conn.close()
    pack_fid  = next((r[0] for r in rows if r[1] == pack_pid), None)
    loose_fid = next((r[0] for r in rows if r[1] == loose_pid), None)
    return pack_fid, loose_fid


def test_delete_both_removes_pair(admin_client, tmp_db):
    pack_fid, unpack_fid = _seed_pair(tmp_db, 900001, 900002)
    resp = admin_client.post(f'/conversions/{unpack_fid}/delete',
                             data={'delete_partner': '1'}, follow_redirects=False)
    assert resp.status_code == 302
    conn = sqlite3.connect(tmp_db)
    n  = conn.execute("SELECT COUNT(*) FROM conversion_formulas WHERE id IN (?,?)",
                      (pack_fid, unpack_fid)).fetchone()[0]
    ni = conn.execute("SELECT COUNT(*) FROM conversion_formula_inputs WHERE formula_id IN (?,?)",
                      (pack_fid, unpack_fid)).fetchone()[0]
    conn.close()
    assert n == 0 and ni == 0                                # both formulas + inputs gone


def test_delete_only_this_keeps_partner(admin_client, tmp_db):
    pack_fid, unpack_fid = _seed_pair(tmp_db, 900011, 900012)
    resp = admin_client.post(f'/conversions/{unpack_fid}/delete', data={}, follow_redirects=False)
    assert resp.status_code == 302
    conn = sqlite3.connect(tmp_db)
    has_unpack = conn.execute("SELECT COUNT(*) FROM conversion_formulas WHERE id=?", (unpack_fid,)).fetchone()[0]
    has_pack   = conn.execute("SELECT COUNT(*) FROM conversion_formulas WHERE id=?", (pack_fid,)).fetchone()[0]
    conn.close()
    assert has_unpack == 0 and has_pack == 1                 # partner survives when flag omitted


def test_delete_no_partner_unchanged(admin_client, tmp_db):
    pack_fid, none_fid = _seed_pair(tmp_db, 900021, 900022, direction='pack')
    assert none_fid is None                                  # one-way: no loose formula
    resp = admin_client.post(f'/conversions/{pack_fid}/delete',
                             data={'delete_partner': '1'}, follow_redirects=False)
    assert resp.status_code == 302
    conn = sqlite3.connect(tmp_db)
    n = conn.execute("SELECT COUNT(*) FROM conversion_formulas WHERE id=?", (pack_fid,)).fetchone()[0]
    conn.close()
    assert n == 0                                            # deletes fine, no error


def test_delete_both_atomic(admin_client, tmp_db):
    pack_fid, unpack_fid = _seed_pair(tmp_db, 900031, 900032)
    conn = sqlite3.connect(tmp_db)
    before = conn.execute("SELECT COUNT(*) FROM conversion_formulas").fetchone()[0]
    conn.close()
    admin_client.post(f'/conversions/{pack_fid}/delete', data={'delete_partner': '1'})
    conn = sqlite3.connect(tmp_db)
    after = conn.execute("SELECT COUNT(*) FROM conversion_formulas").fetchone()[0]
    conn.close()
    assert before - after == 2                               # exactly two rows, one request


def test_list_shows_oneway_badge(admin_client, tmp_db):
    _seed_pair(tmp_db, 900041, 900042, direction='unpack')   # one-way [แกะ] only
    resp = admin_client.get('/conversions')
    assert resp.status_code == 200
    assert 'ทิศเดียว'.encode() in resp.data                  # one-way indicator renders
