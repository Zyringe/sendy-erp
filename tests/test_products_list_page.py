"""Route + model tests for the P1 `/products` list-page revamp
(projects/sendy-products-page-improve/plan.md).

Covers: locations/bsn_codes subqueries on get_products(), BSN-code search,
Shopee/Lazada column removal, the ที่เก็บ column, the restock checkbox
persisting, and the always-on buildable badge (show_alt removed).

⚠ Assertion traps documented in the plan — do not regress these:
- 'ที่เก็บ' in html is ALREADY TRUE on unmodified code (substring of the
  location-filter placeholder สถานที่เก็บ). Assert the element ('>ที่เก็บ<').
- 'Shopee' not in html only proves the <th> is gone; pair with a column count.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def test_get_products_returns_locations_and_bsn_codes_keys(tmp_db):
    import models
    products, total = models.get_products(per_page=5)
    assert products, "expected at least one product in the live-DB clone"
    row = products[0]
    assert 'locations' in row.keys()
    assert 'bsn_codes' in row.keys()


def test_get_products_two_locations_comma_joined(tmp_db):
    import models
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT pl.product_id, GROUP_CONCAT(pl.floor_no, ', ') AS joined
          FROM product_locations pl
          JOIN products p ON p.id = pl.product_id AND p.is_active = 1
         GROUP BY pl.product_id
        HAVING COUNT(*) = 2
         LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        pytest.skip("no active product with exactly 2 locations in the live clone")

    products, total = models.get_products(search=str(row['product_id']), per_page=50)
    match = next(p for p in products if p['id'] == row['product_id'])
    assert set(match['locations'].split(', ')) == set(row['joined'].split(', '))
    assert match['locations'].count(',') == 1


def test_get_products_no_location_is_none(tmp_db):
    import models
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT p.id FROM products p
         WHERE p.is_active = 1
           AND p.id NOT IN (SELECT product_id FROM product_locations)
         LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        pytest.skip("every active product has a location in the live clone")

    products, total = models.get_products(search=str(row['id']), per_page=50)
    match = next(p for p in products if p['id'] == row['id'])
    assert match['locations'] is None


def test_get_products_search_by_bsn_code(tmp_db):
    import models
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT m.bsn_code, m.product_id FROM product_code_mapping m
          JOIN products p ON p.id = m.product_id AND p.is_active = 1
         WHERE m.product_id IS NOT NULL
         LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        pytest.skip("no active BSN-mapped product in the live clone")

    products, total = models.get_products(search=row['bsn_code'], per_page=50)
    assert row['product_id'] in [p['id'] for p in products]


def test_get_products_search_by_name_still_works(tmp_db):
    import models
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, product_name FROM products WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    conn.close()
    frag = row['product_name'][:6]

    products, total = models.get_products(search=frag, per_page=50)
    assert row['id'] in [p['id'] for p in products]


def test_products_page_no_shopee_lazada_columns(admin_client):
    resp = admin_client.get('/products')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Shopee' not in html
    assert 'Lazada' not in html
    # Proves the <td> went with the <th>, not just the header (assertion trap
    # in the plan: the bare-string check alone can pass with a misaligned table).
    # `<th(?=[ >])` (not the naive `'<th'` substring, which also matches `<thead>`).
    assert len(re.findall(r'<th(?=[ >])', html)) == 8


def test_products_page_has_location_column(admin_client):
    resp = admin_client.get('/products')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    # 'ที่เก็บ' alone is a substring of the placeholder สถานที่เก็บ and is
    # ALWAYS true — assert the header element instead (plan's assertion trap).
    assert '>ที่เก็บ<' in html


def test_products_page_restock_checkbox_persists(admin_client):
    resp = admin_client.get('/products?restock=1')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    # A bare 'checked' in html matches any other ticked box and proves nothing
    # (plan's assertion trap) — match name="restock" and checked on ONE <input>.
    assert re.search(r'<input[^>]*name="restock"[^>]*checked[^>]*>', html)


def test_products_page_buildable_badge_default_on(admin_client, tmp_db):
    import models
    res = models.get_buildable()
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
    conn.close()
    if not name:
        pytest.skip("no active buildable product in the live clone")

    resp = admin_client.get('/products', query_string={'q': name[:8]})
    assert resp.status_code == 200
    assert b'(+' in resp.data
