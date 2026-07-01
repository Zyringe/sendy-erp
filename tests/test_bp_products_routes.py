"""Route-level integration tests for bp_products.

Covers 5 highest-value endpoints. Uses tmp_db so route + models +
templates all execute against a live-DB copy and never touch the
real DB. Logs in by setting session keys directly — bypasses the
/login flow since auth isn't what these tests are validating.

bp_products is 561 LOC with 20+ CRUD routes and previously had 0
route-level tests. This file establishes coverage for index,
detail, cost-history (admin/manager-gated JSON), pricing, and a
404-path negative case.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated.

    Matches the session keys read by app.py's permission middleware
    (`session.get('role')`, `session.get('user_id')`). tmp_db is
    pulled in so config/database DATABASE_PATH is already
    monkeypatched before `from app import app` runs.
    """
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _first_active_product_id(tmp_db) -> int:
    row = sqlite3.connect(tmp_db).execute(
        "SELECT id FROM products WHERE is_active = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("No active products in live DB clone")
    return row[0]


def test_products_index_renders(admin_client):
    resp = admin_client.get('/products')
    assert resp.status_code == 200, resp.data[:500]


def test_product_detail_renders(admin_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}')
    assert resp.status_code == 200, resp.data[:500]


def test_product_cost_history_returns_json(admin_client, tmp_db):
    """Admin/manager-gated JSON endpoint — guards the WACC history
    surface that finance reports depend on."""
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}/cost-history')
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    body = resp.get_json()
    assert 'wacc' in body
    assert 'history' in body
    assert isinstance(body['history'], list)


def test_product_pricing_renders(admin_client, tmp_db):
    pid = _first_active_product_id(tmp_db)
    resp = admin_client.get(f'/products/{pid}/pricing')
    assert resp.status_code == 200, resp.data[:500]


def test_product_detail_unknown_id_redirects_to_list(admin_client):
    """Unknown id flashes 'ไม่พบสินค้า' and 302-redirects to /products
    (per blueprints/products.py::product_detail). product_pricing on
    the same unknown id would abort(404), so this test is specific to
    the detail route's documented redirect contract."""
    resp = admin_client.get('/products/99999999', follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers.get('Location', '')
    # Tolerate absolute (http://localhost/products) or relative (/products).
    assert location.endswith('/products') or '/products' in location, (
        f"Expected redirect to /products, got Location={location!r}"
    )


def test_product_new_get_renders(admin_client):
    resp = admin_client.get('/products/new')
    assert resp.status_code == 200, resp.data[:500]


def test_product_new_post_creates_via_structured_path(admin_client, tmp_db):
    """P3 regression: /products/new POST now calls
    models.create_structured_product(data, 'manual') instead of the bare
    models.create_product — the created row must carry created_via='manual'
    and a (fallback) sku_code, and the response redirects to the new
    product's detail page."""
    resp = admin_client.post('/products/new', data={
        'product_name': 'pytest route-created product',
        'unit_type': 'ตัว',
        'cost_price': '5.5',
        'base_sell_price': '9.9',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, created_via, sku_code FROM products "
        "WHERE product_name = 'pytest route-created product'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'
    assert row['sku_code'] == f"INT-{row['id']}"
    assert resp.headers['Location'].endswith(f"/products/{row['id']}")


# ── P4: /products/parse-name + structured /products/new form ────────────────

def test_parse_name_returns_structure_for_sample_name(admin_client):
    """GET /products/parse-name reuses bsn_suggest's name-only parser. Exact
    parse values are heuristic and may drift as the parser is tuned — assert
    the response SHAPE (keys + types) and a non-empty proposed_name, not
    specific field values."""
    resp = admin_client.get('/products/parse-name', query_string={
        'name': 'บานพับ 3นิ้ว สแตนเลส'
    })
    assert resp.status_code == 200, resp.data[:500]
    assert resp.is_json
    body = resp.get_json()
    assert isinstance(body.get('parsed'), dict)
    for key in ('category', 'series', 'brand', 'model', 'size', 'color_th',
                'color_code', 'packaging', 'condition', 'pack_variant'):
        assert key in body['parsed'], f'missing parsed key: {key}'
    assert isinstance(body.get('proposed_name'), str)
    assert body['proposed_name']  # non-empty for a real product-like name
    assert 'brand_id' in body
    assert 'color_code' in body


def test_parse_name_blank_returns_empty_shape(admin_client):
    resp = admin_client.get('/products/parse-name', query_string={'name': ''})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {'parsed': {}, 'proposed_name': '', 'brand_id': None, 'color_code': None}


def test_product_new_post_with_spec_fields_creates_structured_row(admin_client, tmp_db):
    """P4: the rebuilt /products/new form posts spec fields (brand/category/
    packaging/series/model/size) through to create_structured_product — the
    created row must carry those spec columns, a REAL structured sku_code
    (not the INT-<id> fallback), and created_via='manual'."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    brand = conn.execute("SELECT id FROM brands WHERE name='Sendai'").fetchone()
    category = conn.execute("SELECT id FROM categories WHERE code='hinge'").fetchone()
    conn.close()
    assert brand and category, 'expected seed brand/category missing from live-DB clone'

    resp = admin_client.post('/products/new', data={
        'product_name': 'pytest P4 structured product',
        'category_id': str(category['id']),
        'sub_category': 'บานพับทดสอบ',
        'brand_id': str(brand['id']),
        'series': 'PYTESTSERIES',
        'model': 'PT1',
        'size': '3นิ้ว',
        'packaging_th': 'ตัว',
        'unit_type': 'ตัว',
        'cost_price': '12.5',
        'base_sell_price': '20',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM products WHERE product_name = 'pytest P4 structured product'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row['created_via'] == 'manual'
    assert row['brand_id'] == brand['id']
    assert row['category_id'] == category['id']
    assert row['packaging_th'] == 'ตัว'
    assert row['series'] == 'PYTESTSERIES'
    assert row['model'] == 'PT1'
    assert row['sku_code'] is not None
    assert not row['sku_code'].startswith('INT-')


def test_product_new_post_invalid_packaging_rejected(admin_client, tmp_db):
    """An out-of-range packaging_th (bypassing the <select> — e.g. a crafted
    request) must hit the products_packaging_th_check_insert CHECK trigger;
    the route should flash an error and NOT create the product."""
    resp = admin_client.post('/products/new', data={
        'product_name': 'pytest invalid packaging product',
        'packaging_th': 'ไม่มีจริง',
        'unit_type': 'ตัว',
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.data[:500]

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT 1 FROM products WHERE product_name = 'pytest invalid packaging product'"
    ).fetchone()
    conn.close()
    assert row is None


def test_products_show_alt_renders(admin_client):
    """The 'เติมได้จากแพ็ค' tick (show_alt) must render without error."""
    resp = admin_client.get('/products?show_alt=1')
    assert resp.status_code == 200, resp.data[:500]


def test_products_show_alt_shows_buildable_marker(admin_client, tmp_db):
    """A product with buildable>0 shows the (+y) alternative-stock marker
    in the stock column only when show_alt is on."""
    import models
    res = models.get_buildable()  # uses the monkeypatched tmp_db clone
    name = None
    conn = sqlite3.connect(tmp_db)
    for pid, info in res.items():
        if info['buildable'] > 0:
            r = conn.execute(
                "SELECT product_name FROM products WHERE id=? AND is_active=1", (pid,)
            ).fetchone()
            if r:
                name = r[0]
                break
    if not name:
        pytest.skip("no active buildable product in the live clone")
    frag = name[:8]
    with_alt = admin_client.get('/products', query_string={'show_alt': '1', 'q': frag})
    without = admin_client.get('/products', query_string={'q': frag})
    assert with_alt.status_code == 200 and without.status_code == 200
    assert b'(+' in with_alt.data           # marker shown when ticked
    assert b'(+' not in without.data        # and absent when not ticked


def test_product_detail_shows_buildable(admin_client, tmp_db):
    """A product that is a conversion output shows the 'แกะ/แพ็คเพิ่มได้' true-
    availability block on its detail page (Phase 4, display-only)."""
    import models
    target = None
    for pid, info in models.get_buildable().items():
        if info['buildable'] > 0:
            r = sqlite3.connect(tmp_db).execute(
                "SELECT 1 FROM products WHERE id=? AND is_active=1", (pid,)).fetchone()
            if r:
                target = pid
                break
    if target is None:
        pytest.skip("no active buildable product in clone")
    resp = admin_client.get(f'/products/{target}')
    assert resp.status_code == 200, resp.data[:500]
    assert 'แกะ/แพ็คเพิ่มได้'.encode() in resp.data
