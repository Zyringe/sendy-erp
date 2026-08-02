"""VAT-book view — parity rendering, block policy, method guard, stale-tab
binding, toggle route (P2b/P2c integration).

Uses tmp_db (main book = live-DB copy) + a synthetic vat_book.db built from
the LIVE schema with one product + one sales line, placed where
book_registry.book_db_path('vat') resolves (same dir as the tmp main DB)."""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import book_registry as br

LIVE_DB = os.path.join(os.path.dirname(__file__), '..',
                       'inventory_app', 'instance', 'inventory.db')


@pytest.fixture
def vat_db(tmp_db):
    """Full-schema vat_book.db with 1 product + 1 sales line + book_meta."""
    path = br.book_db_path('vat')
    src = sqlite3.connect(f'file:{LIVE_DB}?mode=ro', uri=True)
    objects = src.execute(
        """SELECT sql FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1
                     WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END"""
    ).fetchall()
    src.close()
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = OFF")
    for (sql,) in objects:
        c.execute(sql)
    c.execute("CREATE TABLE book_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO book_meta VALUES ('built_at', '2026-08-02T12:00:00')")
    c.execute("INSERT INTO products (id, product_name, unit_type) "
              "VALUES (901, 'สินค้าสมุดVAT ทดสอบ', 'ตัว')")
    c.execute("INSERT INTO product_code_mapping (bsn_code, bsn_name, product_id) "
              "VALUES ('X901', 'สินค้าสมุดVAT ทดสอบ', 901)")
    c.execute("INSERT INTO sales_transactions "
              "(date_iso, doc_no, doc_base, product_id, bsn_code, product_name_raw,"
              " customer, qty, unit, unit_price, vat_type, total, net) "
              "VALUES ('2026-03-15', 'IV2600042-1', 'IV2600042', 901, 'X901',"
              " 'สินค้าสมุดVAT ทดสอบ', 'ลูกค้าVAT', 2, 'ตัว', 61.725, 2,"
              " 123.45, 123.45)")
    c.commit()
    c.close()
    return path


def _client(book=None, role='admin'):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = role
        if book:
            s['active_book'] = book
    return c


# ── parity rendering ────────────────────────────────────────────────────────

def test_vat_sales_page_renders_vat_book_rows(vat_db):
    html = _client('vat').get('/sales?date_from=2026-03-01&date_to=2026-03-31'
                              ).get_data(as_text=True)
    assert 'IV2600042' in html
    assert 'กำลังดูสมุด VAT' in html          # banner
    assert 'ข้อมูล ณ 2026-08-02' in html      # freshness from book_meta


def test_vat_product_detail_renders_vat_product(vat_db):
    html = _client('vat').get('/products/901').get_data(as_text=True)
    assert 'สินค้าสมุดVAT ทดสอบ' in html


def test_novat_default_unchanged_and_offers_toggle(vat_db):
    r = _client().get('/sales')
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'IV2600042' not in html            # vat row must NOT leak into main
    assert 'ดูสมุด VAT (xp5)' in html         # toggle offer (file exists)


def test_vat_product_badge_from_main_mapping(vat_db, tmp_db):
    """Dual-read: xp5 code (VAT book) → xp5_product_mapping + product name
    (MAIN db). Seed the mapping in main; badge appears on the VAT page."""
    import config
    main = sqlite3.connect(config.DATABASE_PATH)
    main_name = main.execute(
        "SELECT product_name FROM products WHERE id=1").fetchone()[0]
    main.execute("INSERT INTO xp5_product_mapping "
                 "(xp5_code, product_id, xp5_name, match_layer) "
                 "VALUES ('X901', 1, 'สินค้าสมุดVAT ทดสอบ', 'code+name')")
    main.commit(); main.close()
    html = _client('vat').get('/products/901').get_data(as_text=True)
    assert 'สินค้านี้ตรงกับสมุดปัจจุบัน' in html
    assert main_name in html


def test_vat_product_no_badge_when_unmapped(vat_db):
    html = _client('vat').get('/products/901').get_data(as_text=True)
    assert 'สินค้านี้ตรงกับสมุดปัจจุบัน' not in html


# ── block policy ────────────────────────────────────────────────────────────

def test_vat_blocks_non_parity_page_with_redirect(vat_db):
    r = _client('vat').get('/', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/sales')


def test_vat_blocks_api_with_json_403(vat_db):
    r = _client('vat').get('/api/products/search?q=x',
                           headers={'Accept': 'application/json'})
    assert r.status_code == 403
    assert 'error' in r.get_json()


def test_vat_barcodes_get_reads_vat_book_empty(vat_db):
    r = _client('vat').get('/api/products/901/barcodes')
    assert r.status_code == 200
    assert r.get_json() == {'items': []}      # vat book has no barcodes


# ── method guard (read-only) ────────────────────────────────────────────────

def test_vat_denies_post(vat_db):
    r = _client('vat').post('/products/901/brand', data={'brand_id': '1'},
                            follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/sales')


def test_vat_denies_delete_json(vat_db):
    r = _client('vat').delete('/api/products/901/barcodes?id=1')
    assert r.status_code == 403
    assert 'error' in r.get_json()


# ── stale-tab expected_book binding ─────────────────────────────────────────

def test_stale_vat_rendered_form_rejected_in_novat_session(vat_db):
    """Tab A rendered under VAT, tab B switched back to novat, tab A submits:
    the write must be rejected, not land on the main book."""
    r = _client().post('/products/901/brand',
                       data={'brand_id': '1', 'expected_book': 'vat'},
                       follow_redirects=False)
    assert r.status_code == 302               # redirected away, not executed
    with _client().session_transaction() as s:
        pass                                  # (flash checked via follow page)
    r2 = _client().post('/api/products/901/barcodes', json={'barcode': 'x'},
                        headers={'X-Expected-Book': 'vat'})
    assert r2.status_code == 409


def test_matching_expected_book_passes_guard(vat_db):
    """A novat-rendered form in a novat session must NOT be rejected by the
    binding (the request reaches the real handler)."""
    r = _client().post('/api/products/1/barcodes', json={'barcode': ''},
                       headers={'X-Expected-Book': 'novat'})
    # reaches the handler → its own validation answers 400 (barcode required)
    assert r.status_code == 400


MKT_CARD_HEADER = '<i class="bi bi-tags me-2 text-accent"></i>ราคา marketplace'
PROMO_CARD_HEADER = '<i class="bi bi-percent me-2 text-accent"></i>โปรโมชัน'


def test_vat_detail_hides_operational_cards_with_notice(vat_db):
    html = _client('vat').get('/products/901').get_data(as_text=True)
    assert 'มีเฉพาะสมุดปัจจุบัน' in html            # explicit-unavailable notice
    # assert on the card-header ELEMENTS — the notice text itself names the
    # cards, so a bare substring check cannot distinguish them
    assert MKT_CARD_HEADER not in html
    assert PROMO_CARD_HEADER not in html


def test_vat_detail_renders_no_mutation_controls(vat_db):
    """Structural: a read-only book page may carry ONLY the logout and
    book-toggle POST forms — any other form/modal trigger is a mutation
    control leaking into the VAT view (Codex R5)."""
    import re as _re
    html = _client('vat').get('/products/901').get_data(as_text=True)
    forms = _re.findall(r'<form[^>]*method="post"[^>]*>', html, _re.I)
    for f in forms:
        assert ('/logout' in f or '/book/toggle' in f), f'unexpected POST form: {f}'
    assert 'ปรับยอดสต็อก' not in html            # stock-adjust modal
    assert 'data-bs-target="#brandModal"' not in html


def test_novat_detail_keeps_operational_cards(tmp_db):
    html = _client().get('/products/1').get_data(as_text=True)
    assert MKT_CARD_HEADER in html
    assert PROMO_CARD_HEADER in html
    assert 'มีเฉพาะสมุดปัจจุบัน' not in html


# ── general (kiosk) role never enters VAT mode (Codex R4 P1) ────────────────

def test_general_role_with_stuck_vat_session_forced_novat(vat_db):
    """A general session that somehow carries active_book='vat' must behave
    as novat everywhere — no /sales↔/m/stock redirect loop."""
    c = _client('vat', role='general')
    r = c.get('/m/stock')
    assert r.status_code == 200               # kiosk page renders, no loop
    from app import app as flask_app
    with flask_app.test_request_context('/'):
        from flask import session
        session['role'] = 'general'
        session['active_book'] = 'vat'
        assert br.active_book() == 'novat'


def test_general_role_cannot_toggle(vat_db):
    c = _client(role='general')
    c.post('/book/toggle', data={'book': 'vat'}, follow_redirects=False)
    with c.session_transaction() as s:
        assert s.get('active_book', 'novat') == 'novat'


# ── toggle route ────────────────────────────────────────────────────────────

def test_toggle_to_vat_and_back(vat_db):
    c = _client()
    r = c.post('/book/toggle', data={'book': 'vat'}, follow_redirects=False)
    assert r.headers['Location'].endswith('/sales')
    with c.session_transaction() as s:
        assert s['active_book'] == 'vat'
    r = c.post('/book/toggle', data={'book': 'novat'}, follow_redirects=False)
    with c.session_transaction() as s:
        assert s['active_book'] == 'novat'


def test_toggle_to_missing_vat_refused(tmp_db):
    assert not os.path.exists(br.book_db_path('vat'))
    c = _client()
    c.post('/book/toggle', data={'book': 'vat'}, follow_redirects=False)
    with c.session_transaction() as s:
        assert s.get('active_book', 'novat') == 'novat'
