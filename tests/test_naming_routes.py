"""Route-level tests for the Master Naming blueprint (/naming).

Exercises the booted blueprint via the Flask test client — catches blueprint
registration, Jinja render errors, the JSON preview/apply endpoints, and the
manager-only auth gate (things the pure-engine unit tests can't see).
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


def _seed(empty_db):
    """ZZA & ZZB share a Thai word (AB/JBB analogue); one ZZA product has it,
    one ZZA product doesn't, one ZZB product has it (must stay out of scope)."""
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        "INSERT INTO color_finish_codes(code, name_th) VALUES "
        "('ZZA','สีทดสอบรวม'),('ZZB','สีทดสอบรวม');"
    )

    def add(name, color, sku):
        return conn.execute(
            "INSERT INTO products(product_name, color_code, sku_code) VALUES (?,?,?)",
            (name, color, sku),
        ).lastrowid

    ids = {
        "a_word": add("ทดสอบเอ สีทดสอบรวม (ZZA) (แผง)", "ZZA", "ZZTEST-A1"),
        "a_noword": add("ทดสอบเอไม่มีคำ #ZZA", "ZZA", "ZZTEST-A2"),
        "b_word": add("ทดสอบบี สีทดสอบรวม (ZZB) (แผง)", "ZZB", "ZZTEST-B1"),
    }
    conn.commit()
    conn.close()
    return ids


@pytest.fixture
def manager_client(empty_db):
    ids = _seed(empty_db)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c, ids


# ── page render ───────────────────────────────────────────────────────────────

def test_workbench_renders_and_search_filters(manager_client):
    c, ids = manager_client
    body = c.get('/naming?tab=workbench&q=ทดสอบเอ').get_data(as_text=True)
    assert 'รายการสินค้า' in body          # tab label rendered
    assert 'ทดสอบเอ สีทดสอบรวม (ZZA)' in body   # search filter surfaced the seeded row


def test_dictionaries_renders_colors_and_brands(manager_client):
    c, _ = manager_client
    body = c.get('/naming?tab=dictionaries').get_data(as_text=True)
    assert 'พจนานุกรม' in body
    assert 'ZZA' in body                   # seeded color code listed
    assert 'previewColor' in body          # cascade JS wired


# ── JSON cascade API ──────────────────────────────────────────────────────────

def test_color_preview_scopes_by_code(manager_client):
    c, ids = manager_client
    r = c.post('/naming/dict/color/preview',
               json={'key': 'ZZA', 'target': 'สีทดสอบใหม่'})
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True
    assert {a['id'] for a in d['affected']} == {ids['a_word']}
    assert {s['id']: s['reason'] for s in d['skipped']} == {ids['a_noword']: 'word_absent'}
    # ZZB product (shares the word, different code) is absent from both lists.
    assert ids['b_word'] not in {a['id'] for a in d['affected']}


def test_color_apply_renames_through_route(manager_client, empty_db):
    """End-to-end through the HTTP route: preview gives the count, apply renames
    the scoped product. Robust now that the route reads database.DATABASE_PATH
    (the same source get_connection uses) — see naming.py::_db_path."""
    c, ids = manager_client
    pv = c.post('/naming/dict/color/preview',
                json={'key': 'ZZA', 'target': 'สีทดสอบใหม่'}).get_json()
    n = len(pv['affected'])
    assert n == 1
    r = c.post('/naming/dict/color/apply',
               json={'key': 'ZZA', 'target': 'สีทดสอบใหม่', 'expected_count': n})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['applied'] == 1

    conn = sqlite3.connect(empty_db)
    name_a = conn.execute("SELECT product_name FROM products WHERE id=?",
                          (ids['a_word'],)).fetchone()[0]
    conn.close()
    assert name_a == "ทดสอบเอ สีทดสอบใหม่ (ZZA) (แผง)"


def test_apply_count_mismatch_returns_409(manager_client):
    c, _ = manager_client
    r = c.post('/naming/dict/color/apply',
               json={'key': 'ZZA', 'target': 'สีทดสอบใหม่', 'expected_count': 9})
    assert r.status_code == 409
    assert r.get_json()['ok'] is False


def test_unknown_kind_returns_400(manager_client):
    c, _ = manager_client
    r = c.post('/naming/dict/banana/preview', json={'key': 'X', 'target': 'Y'})
    assert r.status_code == 400


# ── Tab 1 inline editor ───────────────────────────────────────────────────────

def test_workbench_renders_inline_editor(manager_client):
    c, _ = manager_client
    body = c.get('/naming?tab=workbench').get_data(as_text=True)
    assert 'onclick="openEditor(this)"' in body   # per-row edit button
    assert 'id="editModal"' in body               # editor modal
    assert 'id="ed-color"' in body                # color dropdown present


def test_product_preview_name_route(manager_client, empty_db):
    conn = sqlite3.connect(empty_db)
    conn.execute("PRAGMA foreign_keys=ON")
    bid = conn.execute(
        "INSERT INTO brands(code, name, name_th, short_code) "
        "VALUES ('zb','ZB','แซด','ZB')"
    ).lastrowid
    conn.execute("INSERT INTO color_finish_codes(code, name_th) VALUES ('ZC','สีแซด')")
    conn.commit()
    conn.close()
    c, _ = manager_client
    r = c.post('/naming/product/preview-name',
               json={'brand_id': bid, 'sub_category': 'กลอน', 'model': '#9',
                     'color_code': 'ZC'})
    assert r.status_code == 200
    assert r.get_json()['name'] == 'กลอน ZB #9 สีแซด (ZC)'


def test_product_save_missing_returns_404(manager_client):
    c, _ = manager_client
    r = c.post('/naming/product/999999/save', json={'color_code': None})
    assert r.status_code == 404
    assert r.get_json()['ok'] is False


# ── auth gate ─────────────────────────────────────────────────────────────────

def test_staff_cannot_access_naming(empty_db):
    _seed(empty_db)
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 2
        sess['username'] = 'test-staff'
        sess['role'] = 'staff'
    # GET is blocked (redirect to dashboard), not rendered.
    r = c.get('/naming')
    assert r.status_code == 302
    assert '/login' not in r.headers.get('Location', '')   # blocked to dashboard, not logged out
