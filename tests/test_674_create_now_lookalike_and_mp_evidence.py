"""#674: สร้างเลย created #2093, a duplicate of #2016 (ลูกรีเวท DOME 4-6 NAT).

Two gaps let it through, each pinned here:
  A. the suggest popup ignored marketplace evidence: the code's only sale was a
     Shopee order whose item was already mapped to #2016;
  C. create_now's duplicate guard compared the exact sku_code only, and the
     proposed FAS-SD-4-6-NAT-SC was not FAS-RVT-SD-DOME-4-6-NAT.

The fixture mirrors prod's shape (verified 2026-09-29): same category_id,
size and color_code, DIFFERENT sub_category (ลูกรีเวท vs ตะปูยิงรีเวท).

⚠ tmp_db clones the dev DB with its data: every row here is keyed on a
ZZ674 marker and wiped first.
"""
import os
import sqlite3

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

_CAT_ID = 6
_SIZE = 'ZZ674-4-6'
_BSN = 'ZZTEST-674-MP'
_BSN_OTHER = 'ZZTEST-674-OTHER'
_BSN_CREATE = 'ZZTEST-674-CREATE'
_DOC = 'IVZZ674'
_ORDER = 'ZZ674ORDER'
_BSN_NAME = 'ตะปูยิงรีเวท ZZ674 Nature (ซอง100ตัว)'


def _clean(conn):
    pid_rows = conn.execute(
        "SELECT id FROM products WHERE size = ?", (_SIZE,)).fetchall()
    for bsn in (_BSN, _BSN_OTHER, _BSN_CREATE):
        row = conn.execute(
            "SELECT approved_product_id FROM pending_product_suggestions WHERE bsn_code=?",
            (bsn,)).fetchone()
        if row and row[0]:
            pid_rows.append((row[0],))
        conn.execute("DELETE FROM pending_product_suggestions WHERE bsn_code=?", (bsn,))
        conn.execute("DELETE FROM product_code_mapping WHERE bsn_code=?", (bsn,))
        conn.execute("DELETE FROM sales_transactions WHERE bsn_code=?", (bsn,))
    conn.execute("DELETE FROM marketplace_order_invoice WHERE doc_base=?", (_DOC,))
    conn.execute("DELETE FROM marketplace_orders WHERE order_sn=?", (_ORDER,))
    for (pid,) in pid_rows:
        conn.execute("DELETE FROM stock_levels WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()


def _product(conn, name, *, color='NAT', sub='ลูกรีเวท', active=1, sku=None):
    cur = conn.execute(
        "INSERT INTO products (product_name, sku_code, is_active, unit_type, "
        "category_id, sub_category, size, color_code) "
        "VALUES (?, ?, ?, 'ตัว', ?, ?, ?, ?)",
        (name, sku or f'ZZ674-{name}', active, _CAT_ID, sub, _SIZE, color))
    return cur.lastrowid


def _sale(conn, bsn, customer_code, product_id=None):
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, bsn_code, "
        "product_name_raw, customer_code, qty, unit, net, product_id) "
        "VALUES ('2026-09-21', ?, ?, ?, ?, ?, 1, 'ซอง', 100, ?)",
        (f'{_DOC}-{bsn}', _DOC, bsn, _BSN_NAME, customer_code, product_id))


def _order(conn, item_pids):
    oid = conn.execute(
        "INSERT INTO marketplace_orders (platform, order_sn) VALUES ('shopee', ?)",
        (_ORDER,)).lastrowid
    for i, pid in enumerate(item_pids):
        conn.execute(
            "INSERT INTO marketplace_order_items (order_id, platform, order_sn, "
            "line_key, item_name, internal_product_id, qty) "
            "VALUES (?, 'shopee', ?, ?, '4-6 มิเนียม(100ตัว)', ?, 1)",
            (oid, _ORDER, f'L{i}', pid))
    conn.execute(
        "INSERT INTO marketplace_order_invoice (platform, order_sn, doc_base, "
        "customer_code, match_method) VALUES ('shopee', ?, ?, 'Zหน้าร้าน', 'auto')",
        (_ORDER, _DOC))


@pytest.fixture
def db(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _clean(conn)
    yield conn
    conn.close()


def _suggest(conn):
    import bsn_suggest
    return bsn_suggest.suggest_for_bsn(conn, _BSN, _BSN_NAME)


# ── A. marketplace evidence ──────────────────────────────────────────────────

def test_marketplace_mapped_product_is_first_with_evidence(db):
    real = _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ')
    _sale(db, _BSN, 'Zหน้าร้าน')
    _order(db, [real])
    db.commit()

    out = _suggest(db)
    first = out['matches'][0]
    assert first['product_id'] == real
    assert _ORDER in first['evidence']
    assert 'Shopee' in first['evidence']
    assert [m['product_id'] for m in out['matches']].count(real) == 1


def test_walk_in_sales_get_no_marketplace_evidence(db):
    real = _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ')
    _sale(db, _BSN, 'Gหน้าร้าน')
    _order(db, [real])
    db.commit()

    out = _suggest(db)
    assert not any(m.get('evidence') for m in out['matches'])


def test_multi_item_order_attributes_the_item_other_lines_do_not_explain(db):
    real = _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ')
    other = _product(db, 'ลูกรีเวท DOME ZZ674 สีดำ', color='BLK')
    _sale(db, _BSN, 'Zหน้าร้าน')
    _sale(db, _BSN_OTHER, 'Zหน้าร้าน', product_id=other)
    _order(db, [real, other])
    db.commit()

    evidenced = [m['product_id'] for m in _suggest(db)['matches'] if m.get('evidence')]
    assert evidenced == [real]


def test_multi_item_order_with_two_unexplained_items_gives_no_evidence(db):
    a = _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ')
    b = _product(db, 'ลูกรีเวท DOME ZZ674 สีดำ', color='BLK')
    _sale(db, _BSN, 'Zหน้าร้าน')
    _order(db, [a, b])
    db.commit()

    assert not any(m.get('evidence') for m in _suggest(db)['matches'])


# ── C. look-alike warning on สร้างเลย ────────────────────────────────────────

def _payload(**over):
    p = {
        'bsn_code': _BSN_CREATE, 'bsn_name': _BSN_NAME,
        'suggested_name': 'ตะปูยิงรีเวท Sendai ZZ674 สีธรรมชาติ (NAT) (ซอง)',
        'category': 'ทดสอบ', 'category_id': _CAT_ID,
        'sub_category': 'ตะปูยิงรีเวท', 'sub_category_short_code': 'ZTST',
        'series': None, 'brand_id': None, 'model': None, 'size': _SIZE,
        'color_th': 'สีธรรมชาติ', 'color_code': 'NAT', 'packaging': None,
        'condition': None, 'pack_variant': None, 'suggested_cost': 0.0,
        'suggested_unit_type': 'ตัว', 'units_per_carton': None,
        'units_per_box': None, 'brand_other_name': None,
        'color_code_other': None, 'packaging_other': None, 'bsn_unit': None,
        'unit_conversion_ratio': None, 'clone_source_pid': None,
    }
    p.update(over)
    return p


@pytest.fixture
def manager(tmp_db, db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-manager'
        sess['role'] = 'manager'
    return c


def _create(client, **over):
    return client.post('/mapping/save', json={'mappings': [
        dict(_payload(), action='create_now', **over)]})


def test_create_now_warns_on_same_category_size_color_then_confirms(manager, db):
    # Prod had three: #980 CSK and #997 รีเวทแผง sort before #2016, so the
    # warning has to name every look-alike, not just the lowest id.
    older = _product(db, 'ลูกรีเวท CSK ZZ674 สีธรรมชาติ')
    real = _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ')
    db.commit()

    resp = _create(manager)
    assert resp.status_code == 409, resp.data[:500]
    body = resp.get_json()
    assert body['duplicate_kind'] == 'lookalike'
    assert [c['id'] for c in body['candidates']] == [older, real]
    assert db.execute("SELECT COUNT(*) FROM pending_product_suggestions WHERE bsn_code=?",
                      (_BSN_CREATE,)).fetchone()[0] == 0

    resp2 = _create(manager, confirm_duplicate=True)
    assert resp2.status_code == 200, resp2.data[:500]
    assert resp2.get_json()['product_id'] != real


def test_create_now_ignores_inactive_or_different_colour_lookalikes(manager, db):
    _product(db, 'ลูกรีเวท DOME ZZ674 สีธรรมชาติ ปิดแล้ว', active=0)
    _product(db, 'ลูกรีเวท DOME ZZ674 สีดำ', color='BLK')
    db.commit()

    resp = _create(manager)
    assert resp.status_code == 200, resp.data[:500]
