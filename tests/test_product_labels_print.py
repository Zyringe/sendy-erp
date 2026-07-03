"""Phase 3 — product-label (ป้ายสินค้า) team print UI.

Covers `labels.print_page` (`/labels/print`) and `labels.search_api`
(`/api/labels/search`): role gating (admin/manager/staff allowed, general/
shareholder blocked), search results, and the nav pin (desktop sidebar +
mobile drawer + `_ENDPOINT_MODULE` — see erp-engineering-discipline "A
navigable page lives in THREE nav surfaces").

Runs against `tmp_db` (a copy of the live DB), which already carries Phase 1's
imported product_labels (1,104 rows). Expected rows are re-derived by direct
SQL query on tmp_db per test, never hardcoded (see verification-discipline).

Python 3.9 — Optional[...] not X | None.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')


# ── helpers ──────────────────────────────────────────────────────────────────

def _client(role, user_id=1):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


# ── /labels/print — role gate ────────────────────────────────────────────────

def test_print_page_admin_ok(tmp_db):
    assert _client('admin').get('/labels/print').status_code == 200


def test_print_page_manager_ok(tmp_db):
    assert _client('manager').get('/labels/print').status_code == 200


def test_print_page_staff_ok(tmp_db):
    assert _client('staff').get('/labels/print').status_code == 200


def test_print_page_shareholder_forbidden(tmp_db):
    assert _client('shareholder').get('/labels/print').status_code == 403


def test_print_page_general_redirected(tmp_db):
    # general is mobile-kiosk-only; require_login redirects before the
    # route's own _require_print_role() ever runs.
    assert _client('general').get('/labels/print').status_code == 302


# ── /api/labels/search — role gate + content ─────────────────────────────────

def test_search_api_admin_ok(tmp_db):
    assert _client('admin').get('/api/labels/search?q=x').status_code == 200


def test_search_api_staff_ok(tmp_db):
    assert _client('staff').get('/api/labels/search?q=x').status_code == 200


def test_search_api_shareholder_forbidden(tmp_db):
    assert _client('shareholder').get('/api/labels/search?q=x').status_code == 403


def test_search_api_general_redirected(tmp_db):
    assert _client('general').get('/api/labels/search?q=x').status_code == 302


def test_search_api_empty_q_returns_empty(tmp_db):
    r = _client('admin').get('/api/labels/search?q=')
    assert r.status_code == 200
    assert r.get_json()['items'] == []


def test_search_api_returns_matching_rows(tmp_db):
    row = _conn(tmp_db).execute(
        "SELECT id, product_name, barcode, brand, packaging_th, size_th, label_size "
        "FROM product_labels WHERE is_active = 1 AND product_name LIKE '%กรรไกร%' LIMIT 1"
    ).fetchone()
    assert row is not None, "fixture DB should carry product_labels rows with กรรไกร in the name"

    data = _client('admin').get('/api/labels/search?q=กรรไกร').get_json()
    ids = [it['id'] for it in data['items']]
    assert row['id'] in ids
    match = next(it for it in data['items'] if it['id'] == row['id'])
    assert match['product_name'] == row['product_name']
    assert match['barcode'] == (row['barcode'] or '')
    assert match['brand'] == (row['brand'] or '')
    assert match['label_size'] == row['label_size']


def test_search_api_by_barcode(tmp_db):
    row = _conn(tmp_db).execute(
        "SELECT barcode FROM product_labels WHERE barcode IS NOT NULL AND barcode <> '' "
        "AND is_active = 1 LIMIT 1"
    ).fetchone()
    data = _client('admin').get(f'/api/labels/search?q={row["barcode"]}').get_json()
    assert any(it['barcode'] == row['barcode'] for it in data['items'])


def test_search_api_caps_results(tmp_db):
    # SENDAI is the largest brand bucket (892 rows in Phase 1 import) — a
    # broad match must still be capped, not return everything.
    data = _client('admin').get('/api/labels/search?q=SENDAI').get_json()
    assert 0 < len(data['items']) <= 30


# ── Nav pin (post-ship-fix regression, per erp-engineering-discipline) ───────

def test_print_page_nav_present_desktop_and_mobile(tmp_db):
    from app import _ENDPOINT_MODULE
    assert _ENDPOINT_MODULE.get('labels.print_page') == 'operation'
    assert _ENDPOINT_MODULE.get('labels.search_api') == 'operation'

    html = _client('admin').get('/labels/print').get_data(as_text=True)
    # both the desktop sidebar AND the mobile drawer must link to /labels/print
    assert html.count('href="/labels/print"') >= 2, "พิมพ์ป้ายสินค้า link missing from a nav"


def test_print_page_nav_visible_to_staff(tmp_db):
    # nav gate is a DIFFERENT (broader) block than Phase 2's admin-only
    # labels links — staff must see the print link too.
    html = _client('staff').get('/labels/print').get_data(as_text=True)
    assert html.count('href="/labels/print"') >= 2
