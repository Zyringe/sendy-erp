"""Route-level integration tests for bp_ecommerce (ecommerce-revamp Phase 4).

Covers the product-centric overview page (`/ecommerce`) and the per-product
detail page (`/ecommerce/product/<id>`) that replaced the old 3-tab UI.
Uses tmp_db (a live-DB clone) so routes + models + templates all execute
against real data shapes — pid 225 (5 shopee ps rows: 1 real + 4 propagated
freebie-bundle stubs with NULL variation_id, + 1 lazada row) and pid 22
(a [แกะ] pack/unpack product) are the same worked examples the plan and the
Phase 3 model tests use.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest


@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated (bypasses /login —
    auth isn't what these tests validate). Mirrors test_bp_products_routes.py."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


def _skip_if_missing(tmp_db, product_id):
    row = sqlite3.connect(tmp_db).execute(
        "SELECT 1 FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if row is None:
        pytest.skip(f"pid {product_id} not present in this DB snapshot")


def _product_with_no_listings(tmp_db):
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("""
        SELECT p.id FROM products p
         WHERE p.is_active = 1
           AND p.id NOT IN (SELECT internal_product_id FROM platform_skus
                             WHERE internal_product_id IS NOT NULL)
         LIMIT 1
    """).fetchone()
    if row is None:
        pytest.skip("No unlisted active product in this DB snapshot")
    return row[0]


# ── overview page ──────────────────────────────────────────────────────────

def test_overview_renders(admin_client):
    resp = admin_client.get('/ecommerce')
    assert resp.status_code == 200, resp.data[:500]


@pytest.mark.parametrize('flt', ['red', 'amber', 'dead', 'shopee', 'lazada', 'tiktok'])
def test_overview_filter_chips_render(admin_client, flt):
    resp = admin_client.get(f'/ecommerce?flt={flt}')
    assert resp.status_code == 200, resp.data[:500]


def test_overview_search_renders(admin_client):
    resp = admin_client.get('/ecommerce?q=%E0%B8%81%E0%B8%B8%E0%B8%8D%E0%B9%81%E0%B8%88')  # กุญแจ
    assert resp.status_code == 200, resp.data[:500]


def test_overview_sidebar_present(admin_client):
    """Three-nav-surfaces rule: a page missing from _ENDPOINT_MODULE loses its
    whole sidebar. /ecommerce must show the link at least twice (desktop
    sidebar + mobile drawer)."""
    resp = admin_client.get('/ecommerce')
    html = resp.data.decode('utf-8')
    assert html.count('href="/ecommerce"') >= 2


def test_overview_upload_forms_have_csrf(admin_client):
    html = admin_client.get('/ecommerce').data.decode('utf-8')
    assert html.count('name="csrf_token"') >= 2  # weekly-file form + import-info form


# ── product detail page ───────────────────────────────────────────────────

def test_product_detail_renders_pid225(admin_client, tmp_db):
    _skip_if_missing(tmp_db, 225)
    resp = admin_client.get('/ecommerce/product/225')
    assert resp.status_code == 200, resp.data[:500]
    html = resp.data.decode('utf-8')
    assert 'LCK-LK-SD-#SL-231-AC' in html
    # the item-less bucket (4 propagated freebie-bundle stubs) must render
    # without crashing and be tagged as pending-file, not shown as real stock.
    assert 'รอไฟล์รอบถัดไป' in html


def test_product_detail_renders_pid4_pack_unpack(admin_client, tmp_db):
    """pid 4 has both a real shopee listing and an active [แกะ]/[แพ็ค]
    conversion formula as output -- exercises the +แพ็ค buildable display."""
    _skip_if_missing(tmp_db, 4)
    resp = admin_client.get('/ecommerce/product/4')
    assert resp.status_code == 200, resp.data[:500]


def test_product_detail_404_when_no_listings(admin_client, tmp_db):
    pid = _product_with_no_listings(tmp_db)
    resp = admin_client.get(f'/ecommerce/product/{pid}')
    assert resp.status_code == 404


def test_product_detail_404_when_product_missing(admin_client):
    resp = admin_client.get('/ecommerce/product/99999999')
    assert resp.status_code == 404


def test_product_detail_sidebar_present(admin_client, tmp_db):
    _skip_if_missing(tmp_db, 225)
    html = admin_client.get('/ecommerce/product/225').data.decode('utf-8')
    assert html.count('href="/ecommerce"') >= 2


def test_product_detail_endpoint_module_mapped():
    import access_control
    assert access_control._ENDPOINT_MODULE['ecommerce.ecommerce_product'] == 'trade'


# ── sku_edit: qty_per_sale inline edit + `next` redirect ──────────────────

def test_sku_edit_honors_next_and_preserves_price_stock(admin_client, tmp_db):
    _skip_if_missing(tmp_db, 225)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sku = conn.execute(
        "SELECT id, price, special_price, stock FROM platform_skus "
        "WHERE internal_product_id=225 AND platform='shopee' AND variation_id IS NOT NULL"
    ).fetchone()
    conn.close()
    if sku is None:
        pytest.skip("pid 225 has no real (non-stub) shopee sku in this DB snapshot")

    next_url = '/ecommerce/product/225'
    resp = admin_client.post(f"/ecommerce/sku/{sku['id']}/edit", data={
        'price': '' if sku['price'] is None else str(sku['price']),
        'special_price': '' if sku['special_price'] is None else str(sku['special_price']),
        'stock': '' if sku['stock'] is None else str(sku['stock']),
        'qty_per_sale': '3',
        'next': next_url,
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'] == next_url

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT qty_per_sale, price, stock FROM platform_skus WHERE id=?", (sku['id'],)
    ).fetchone()
    conn.close()
    assert row[0] == 3.0
    # price/stock round-tripped unchanged (update_platform_sku fully overwrites
    # these columns — the inline form must carry them as hidden fields).
    assert row[1] == sku['price']
    assert row[2] == sku['stock']


def test_sku_edit_rejects_offsite_next(admin_client, tmp_db):
    _skip_if_missing(tmp_db, 225)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sku = conn.execute(
        "SELECT id FROM platform_skus WHERE internal_product_id=225 "
        "AND platform='shopee' AND variation_id IS NOT NULL"
    ).fetchone()
    conn.close()
    if sku is None:
        pytest.skip("pid 225 has no real (non-stub) shopee sku in this DB snapshot")

    resp = admin_client.post(f"/ecommerce/sku/{sku['id']}/edit", data={
        'qty_per_sale': '2',
        'next': '//evil.example.com/steal',
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'].startswith('/ecommerce')
    assert 'evil.example.com' not in resp.headers['Location']


def test_sku_edit_does_not_bump_imported_at(admin_client, tmp_db):
    """A manual qty_per_sale edit is NOT a file import: it must never touch
    imported_at. _snapshot_dates() (ecommerce_overview) reads the per-platform
    file date as MAX(imported_at) — one bumped row would shift the whole
    platform's snapshot to today, zeroing sold_since and faking the freshness
    pill (found in the P4 review: the PR's own live-test edit did exactly
    that to the local DB's shopee snapshot)."""
    _skip_if_missing(tmp_db, 225)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sku = conn.execute(
        "SELECT id, platform, price, special_price, stock, qty_per_sale, imported_at "
        "FROM platform_skus WHERE internal_product_id=225 "
        "AND platform='shopee' AND variation_id IS NOT NULL"
    ).fetchone()
    snap_before = conn.execute(
        "SELECT date(MAX(imported_at)) FROM platform_skus WHERE platform='shopee'"
    ).fetchone()[0]
    conn.close()
    if sku is None:
        pytest.skip("pid 225 has no real (non-stub) shopee sku in this DB snapshot")

    resp = admin_client.post(f"/ecommerce/sku/{sku['id']}/edit", data={
        'price': '' if sku['price'] is None else str(sku['price']),
        'special_price': '' if sku['special_price'] is None else str(sku['special_price']),
        'stock': '' if sku['stock'] is None else str(sku['stock']),
        'qty_per_sale': '5',
    }, follow_redirects=False)
    assert resp.status_code == 302

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT qty_per_sale, imported_at FROM platform_skus WHERE id=?", (sku['id'],)
    ).fetchone()
    snap_after = conn.execute(
        "SELECT date(MAX(imported_at)) FROM platform_skus WHERE platform='shopee'"
    ).fetchone()[0]
    conn.close()
    assert row[0] == 5.0                       # the edit itself landed
    assert row[1] == sku['imported_at']        # file stamp untouched
    assert snap_after == snap_before           # platform snapshot unmoved


def test_sku_edit_rejects_backslash_next(admin_client, tmp_db):
    """Browsers normalize '\\' to '/' in URLs, so '/\\evil.com' would become
    protocol-relative '//evil.com' — the plain '//' check alone misses it."""
    _skip_if_missing(tmp_db, 225)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sku = conn.execute(
        "SELECT id FROM platform_skus WHERE internal_product_id=225 "
        "AND platform='shopee' AND variation_id IS NOT NULL"
    ).fetchone()
    conn.close()
    if sku is None:
        pytest.skip("pid 225 has no real (non-stub) shopee sku in this DB snapshot")

    resp = admin_client.post(f"/ecommerce/sku/{sku['id']}/edit", data={
        'qty_per_sale': '2',
        'next': '/\\evil.example.com/steal',
    }, follow_redirects=False)
    assert resp.status_code == 302
    # must fall back to the overview, not redirect into the backslash path
    assert resp.headers['Location'].startswith('/ecommerce')
    assert 'evil.example.com' not in resp.headers['Location']


def test_sku_edit_falls_back_to_overview_without_next(admin_client, tmp_db):
    _skip_if_missing(tmp_db, 225)
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    sku = conn.execute(
        "SELECT id FROM platform_skus WHERE internal_product_id=225 "
        "AND platform='shopee' AND variation_id IS NOT NULL"
    ).fetchone()
    conn.close()
    if sku is None:
        pytest.skip("pid 225 has no real (non-stub) shopee sku in this DB snapshot")

    resp = admin_client.post(f"/ecommerce/sku/{sku['id']}/edit", data={
        'qty_per_sale': '2',
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers['Location'].startswith('/ecommerce')
    assert '/ecommerce/product' not in resp.headers['Location']
