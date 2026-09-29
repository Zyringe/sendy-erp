"""TikTok orders into marketplace_orders (PR-1 of the TikTok order import).

Plan: projects/tiktok-order-import/tiktok-order-import-plan.md, PR-1 steps 3-6.
Put decided (2026-09-30): a TikTok order does NOT touch the
`platform_skus.stock` mirror. The Express IV keeps adjusting TikTok stock
through ecommerce_overview._sold_since_by_pid, as before. One constant,
bsn_sync.PLATFORM_STOCK_DEDUCT_CUSTOMERS, decides both "an IV deducts the
mirror" and "an order deducts the mirror", so the two cannot drift.

Every fixture forces its own rows on empty_db, and applies migration 197 to
it (the rebuild is re-runnable), so the result does not depend on whether the
worktree DB the clone came from has 197 stamped yet.
"""
import io
import os

import pandas as pd
import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import models
from marketplace_files import detect_file, load_order_export
from models.bsn_sync import PLATFORM_STOCK_DEDUCT_CUSTOMERS
from models.marketplace import import_marketplace_orders
from parse_orders import parse_tiktok_orders

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FIXTURE = os.path.join(REPO, 'tests', 'fixtures', 'tiktok', 'tiktok_orders_sample.csv')
MIG_197 = os.path.join(REPO, 'data', 'migrations', '197_marketplace_orders_tiktok.sql')

# The 4 SKU IDs in the fixture (= platform_skus.variation_id on prod).
TT_SKUS = ('1737136796219442872', '1737136999694436024',
           '1736984084285458104', '1736984084285654712')


def _bytes():
    with open(FIXTURE, 'rb') as f:
        return f.read()


@pytest.fixture
def conn(empty_db_conn):
    with open(MIG_197, encoding='utf-8') as f:
        empty_db_conn.executescript(f.read())
    empty_db_conn.execute("INSERT INTO products (id, product_name) VALUES (9301, 'mapped')")
    empty_db_conn.commit()
    return empty_db_conn


def _seed_listing(conn, platform, variation_id, stock=10, product_id=9301,
                  stock_as_of='2026-01-01 00:00:00'):
    conn.execute(
        "INSERT INTO platform_skus (platform, product_name, variation_id, stock, "
        "internal_product_id, stock_as_of, imported_at) VALUES (?,?,?,?,?,?,?)",
        (platform, f'{platform} {variation_id}', variation_id, stock, product_id,
         stock_as_of, stock_as_of))
    conn.commit()


def _stock(conn, platform):
    return [tuple(r) for r in conn.execute(
        "SELECT variation_id, stock FROM platform_skus WHERE platform=? ORDER BY variation_id",
        (platform,))]


def _provenance(conn):
    return conn.execute("SELECT COUNT(*) FROM platform_stock_deductions").fetchone()[0]


# ── detection + the one loader ───────────────────────────────────────────────

def test_detect_file_knows_the_tiktok_order_csv():
    assert detect_file(io.BytesIO(_bytes())) == ('order', 'tiktok')


def test_detect_file_still_knows_a_lazada_statement_csv():
    """Control: the comma sniff must not shadow the ';' Lazada CSVs."""
    h = ('Statement Period;Statement Number;Transaction Date;Fee Name;'
         'Amount(Include Tax);VAT Amount;Order Number')
    assert detect_file(io.BytesIO(('﻿' + h + '\r\n').encode('utf-8'))) == ('laz_statement', 'lazada')


def test_loader_reads_the_tiktok_csv():
    platform, orders = load_order_export(_bytes())
    assert platform == 'tiktok'
    assert len(orders) == 3
    assert sum(len(o['items']) for o in orders) == 4
    # Same result as parsing the DataFrame directly: the loader adds no logic.
    df = pd.read_csv(io.BytesIO(_bytes()), dtype=str, keep_default_na=False, encoding='utf-8-sig')
    assert orders == parse_tiktok_orders(df)


def _xlsx(columns, row):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        pd.DataFrame([row], columns=columns).to_excel(w, sheet_name='Sheet1', index=False)
    return buf.getvalue()


def test_loader_reads_shopee_and_lazada_excel():
    sp = _xlsx(['หมายเลขคำสั่งซื้อ', 'ชื่อสินค้า', 'จำนวน'], ['SP1', 'x', '1'])
    lz = _xlsx(['orderNumber', 'orderItemId', 'itemName'], ['LZ1', '1', 'y'])
    assert load_order_export(sp)[0] == 'shopee'
    assert load_order_export(sp)[1][0]['order_sn'] == 'SP1'
    assert load_order_export(lz)[0] == 'lazada'
    assert load_order_export(lz)[1][0]['order_sn'] == 'LZ1'


def test_loader_refuses_a_file_that_is_not_an_order_export():
    junk = _xlsx(['foo', 'bar'], ['1', '2'])
    with pytest.raises(ValueError):
        load_order_export(junk)


# ── engine: TikTok orders never touch the mirror ─────────────────────────────

def test_one_constant_decides_which_platforms_deduct():
    assert set(PLATFORM_STOCK_DEDUCT_CUSTOMERS.values()) == {'shopee', 'lazada'}


def test_tiktok_order_after_stock_as_of_leaves_the_mirror_alone(conn):
    for v in TT_SKUS:
        _seed_listing(conn, 'tiktok', v)
    before = _stock(conn, 'tiktok')
    assert len(before) == 4

    s = import_marketplace_orders(conn, load_order_export(_bytes())[1], 'tt.csv')

    assert (s['orders'], s['items'], s['unmapped']) == (3, 4, 0)
    assert s['deducted'] == 0 and s['skipped_lines'] == 0
    assert _stock(conn, 'tiktok') == before
    assert _provenance(conn) == 0


def test_shopee_control_still_deducts(conn):
    _seed_listing(conn, 'shopee', 'SV1', stock=10)
    order = {'platform': 'shopee', 'order_sn': 'SPX1', 'status': 'ที่ต้องจัดส่ง',
             'order_date': '2026-09-04 12:00', 'items': [
                 {'line_key': 'a', 'variation_id': 'SV1', 'item_name': 'x', 'qty': 2.0}]}
    s = import_marketplace_orders(conn, [order], 'sp.xlsx')
    assert s['deducted'] == 2
    assert _stock(conn, 'shopee') == [('SV1', 8)]
    assert _provenance(conn) == 1


def test_reimport_is_unchanged(conn):
    for v in TT_SKUS:
        _seed_listing(conn, 'tiktok', v)
    orders = load_order_export(_bytes())[1]
    import_marketplace_orders(conn, orders, 'tt.csv')
    snap = lambda: ([tuple(r) for r in conn.execute(
        "SELECT id, platform, order_sn, status, item_total, order_date, paid_date "
        "FROM marketplace_orders ORDER BY id")],
        [tuple(r) for r in conn.execute(
            "SELECT order_sn, line_key, qty, unit_price, item_subtotal, internal_product_id "
            "FROM marketplace_order_items ORDER BY order_sn, line_key")])
    first = snap()
    assert len(first[0]) == 3 and len(first[1]) == 4

    import_marketplace_orders(conn, orders, 'tt.csv')

    assert snap() == first
    assert _stock(conn, 'tiktok') == [(v, 10) for v in sorted(TT_SKUS)]
    assert _provenance(conn) == 0


# ── route: /marketplace/upload ───────────────────────────────────────────────

def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def test_upload_route_imports_the_tiktok_csv(conn):
    for v in TT_SKUS:
        _seed_listing(conn, 'tiktok', v)
    before = _stock(conn, 'tiktok')

    resp = _client().post('/marketplace/upload',
                          data={'files': [(io.BytesIO(_bytes()), 'ทั้งหมด คำสั่งซื้อ-2026-09-30.csv')]},
                          content_type='multipart/form-data', follow_redirects=True)

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'ไม่รู้จักชนิดไฟล์' not in html and 'นำเข้าไม่สำเร็จ' not in html
    assert conn.execute("SELECT COUNT(*) FROM marketplace_orders WHERE platform='tiktok'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM marketplace_order_items WHERE platform='tiktok' "
                        "AND internal_product_id IS NOT NULL").fetchone()[0] == 4
    assert _stock(conn, 'tiktok') == before
    assert _provenance(conn) == 0


# ── pages: badge + payout column ─────────────────────────────────────────────

def _import_fixture(conn):
    for v in TT_SKUS:
        _seed_listing(conn, 'tiktok', v)
    import_marketplace_orders(conn, load_order_export(_bytes())[1], 'tt.csv')


def _row_html(html, order_sn):
    start = html.index(order_sn)
    return html[html.rindex('<tr', 0, start):html.index('</tr>', start)]


def test_dashboard_badges_tiktok_and_waits_for_income(conn):
    _import_fixture(conn)
    html = _client().get('/marketplace?platform=tiktok').get_data(as_text=True)
    row = _row_html(html, '585884671861360379')
    assert 'TikTok' in row and 'Lazada' not in row and 'Shopee' not in row
    assert 'รอไฟล์ Income' in row
    assert '~ประมาณ' not in row
    # Control: the filter really narrowed to TikTok, and the other orders render too.
    assert html.count('class="js-order-detail"') == 3


def test_dashboard_summary_counts_tiktok(conn):
    _import_fixture(conn)
    summary = models.get_marketplace_summary()
    assert summary['tiktok']['orders'] == 3
    html = _client().get('/marketplace').get_data(as_text=True)
    assert 'TikTok' in html


def test_shopee_estimate_still_shows_when_a_fee_exists(conn):
    """Control for the payout rule: a fee on file keeps the old estimate."""
    conn.execute("INSERT INTO marketplace_orders (platform, order_sn, item_total, marketplace_fee, "
                 "order_date) VALUES ('shopee', 'SPFEE1', 100, 10, '2026-09-04 10:00')")
    conn.commit()
    row = _row_html(_client().get('/marketplace').get_data(as_text=True), 'SPFEE1')
    assert '~ประมาณ' in row and 'รอไฟล์ Income' not in row


def test_cancelled_tiktok_order_shows_in_returns_as_tiktok(conn):
    _import_fixture(conn)
    html = _client().get('/marketplace/returns').get_data(as_text=True)
    row = _row_html(html, '585883444661159817')
    assert 'TikTok' in row and 'Lazada' not in row
