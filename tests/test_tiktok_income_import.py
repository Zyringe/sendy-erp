"""TikTok income file into Sendy (PR-2): payout + fee breakdown per TikTok order.

Plan: projects/tiktok-order-import/tiktok-order-import-plan.md, PR-2 steps 2-5.
Put's scope (2026-09-30): no cashbook entry, no wallet/withdrawal rows, no
automatch, no reconcile. The money is still in TikTok (Q1) and IV linking is
PR-3, whose _CUST_CODE has no tiktok key yet.

Fixtures: the real order CSV and income file of 2026-09-30, on empty_db with
migration 197 applied (re-runnable), so nothing depends on the dev DB.
"""
import io
import json
import os

import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

import marketplace_match
import marketplace_reconcile
import models
from marketplace_files import detect_file, load_order_export
from models.marketplace import import_marketplace_orders

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FIX = os.path.join(REPO, 'tests', 'fixtures', 'tiktok')
ORDERS_CSV = os.path.join(FIX, 'tiktok_orders_sample.csv')
INCOME_XLSX = os.path.join(FIX, 'tiktok_income_sample.xlsx')
MIG_197 = os.path.join(REPO, 'data', 'migrations', '197_marketplace_orders_tiktok.sql')

O379, O543, O817 = '585884671861360379', '585884215723460543', '585883444661159817'


def _read(p):
    with open(p, 'rb') as f:
        return f.read()


@pytest.fixture
def conn(empty_db_conn):
    with open(MIG_197, encoding='utf-8') as f:
        empty_db_conn.executescript(f.read())
    empty_db_conn.commit()
    return empty_db_conn


def _import_orders(conn):
    import_marketplace_orders(conn, load_order_export(_read(ORDERS_CSV))[1], 'orders.csv')


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def _upload(*files):
    return _client().post(
        '/marketplace/upload',
        data={'files': [(io.BytesIO(_read(p)), name) for p, name in files]},
        content_type='multipart/form-data', follow_redirects=True).get_data(as_text=True)


INCOME = (INCOME_XLSX, 'income_20260930032855(UTC+7).xlsx')
ORDERS = (ORDERS_CSV, 'ทั้งหมด คำสั่งซื้อ-2026-09-30.csv')


def _payouts(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT order_sn, actual_payout, settled_at, settlement_source FROM marketplace_orders "
        "WHERE platform='tiktok' ORDER BY order_sn")]


def _fees(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT order_sn, item_value, fee_commission, fee_service, fee_transaction, fee_platform, "
        "fee_ads_escrow, fee_tax, shipping_net, fee_saver, fee_total, net_payout, created_at "
        "FROM marketplace_order_fees WHERE platform='tiktok' ORDER BY order_sn")]


def _untouched_tables(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ('marketplace_wallet_txns', 'marketplace_payouts', 'marketplace_order_invoice',
                      'cashbook_transactions')}


# ── detection ────────────────────────────────────────────────────────────────

def test_detect_file_knows_the_tiktok_income_xlsx():
    assert detect_file(io.BytesIO(_read(INCOME_XLSX))) == ('tt_income', 'tiktok')


# ── route ────────────────────────────────────────────────────────────────────

def test_upload_stamps_payouts_and_fees(conn):
    _import_orders(conn)
    before = _untouched_tables(conn)

    html = _upload(INCOME)

    assert 'นำเข้าไม่สำเร็จ' not in html and 'ไม่รู้จักชนิดไฟล์' not in html
    assert [(sn, p, d) for sn, p, d, _ in _payouts(conn)] == [
        (O817, 0.0, '2026-09-05'), (O543, 88.38, '2026-09-13'), (O379, 309.05, '2026-09-13')]
    f = {r[0]: r for r in _fees(conn)}
    assert [f[o][10] for o in (O379, O543, O817)] == [125.95, 30.62, 0.0]
    # commission, service(growth), transaction, platform(infra), affiliate, tax, shipping, saver
    assert f[O379][2:10] == (-41.89, -32.58, -13.96, -1.07, -36.45, 0.0, 0.0, 0.0)
    assert round(sum(p for _, p, _, _ in _payouts(conn)), 2) == 397.43
    # Scope: no wallet rows, no payout batch, no IV link, no cashbook entry.
    assert _untouched_tables(conn) == before


def test_reimport_leaves_everything_unchanged(conn):
    _import_orders(conn)
    _upload(INCOME)
    first = (_payouts(conn), _fees(conn))
    assert len(first[0]) == 3 and len(first[1]) == 3

    _upload(INCOME)

    assert (_payouts(conn), _fees(conn)) == first


def test_income_before_orders_writes_nothing_and_says_order_first(conn):
    html = _upload(INCOME)
    assert conn.execute("SELECT COUNT(*) FROM marketplace_order_fees").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM marketplace_orders").fetchone()[0] == 0
    assert 'ไม่พบออเดอร์ 3 รายการ' in html
    assert 'ไฟล์ Order' in html


def test_income_and_orders_in_one_batch_orders_land_first(conn):
    """Listed income-first (as a browser might send it): the route still sorts
    the order CSV ahead of it, so all 3 payouts land."""
    _upload(INCOME, ORDERS)
    assert [p for _, p, _, _ in _payouts(conn)] == [0.0, 88.38, 309.05]


def test_upload_never_calls_automatch_or_reconcile_for_tiktok(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(marketplace_match, 'run_automatch', lambda c, p: calls.append(('match', p)))
    monkeypatch.setattr(marketplace_reconcile, 'reconcile_payouts',
                        lambda c, p: calls.append(('reconcile', p)) or {})
    _import_orders(conn)
    _upload(INCOME)
    assert calls == []
    # Control: run_automatch really cannot take tiktok yet (PR-3 adds _CUST_CODE).
    assert 'tiktok' not in marketplace_match._CUST_CODE


def test_adjustment_rows_are_named_in_the_flash(conn, monkeypatch):
    import parse_tiktok_income as pti
    real = pti.parse_tiktok_income

    def with_adjustment(*a):
        out = real(*a)
        out['adjustments'] = [{'id': '9990001', 'type': 'การปรับยอด', 'amount': -12.5}]
        return out
    monkeypatch.setattr(pti, 'parse_tiktok_income', with_adjustment)
    _import_orders(conn)
    html = _upload(INCOME)
    assert '9990001' in html and '-12.50' in html


def test_refused_file_writes_nothing(conn, monkeypatch):
    import parse_tiktok_income as pti

    def refuse(*a):
        raise pti.TikTokIncomeError('ยอดไม่ตรง')
    monkeypatch.setattr(pti, 'parse_tiktok_income', refuse)
    _import_orders(conn)
    html = _upload(INCOME)
    assert 'นำเข้าไม่สำเร็จ' in html and 'ยอดไม่ตรง' in html
    assert all(p is None for _, p, _, _ in _payouts(conn))
    assert _fees(conn) == []


# ── display ──────────────────────────────────────────────────────────────────

def _row_html(html, order_sn):
    start = html.index(order_sn)
    return html[html.rindex('<tr', 0, start):html.index('</tr>', start)]


def test_dashboard_shows_the_real_payout(conn):
    _import_orders(conn)
    _upload(INCOME)
    html = _client().get('/marketplace?platform=tiktok').get_data(as_text=True)
    for sn, shown in ((O379, '309.05'), (O543, '88.38'), (O817, '0.00')):
        row = _row_html(html, sn)
        assert shown in row, sn
        assert 'รอไฟล์ Income' not in row


def test_order_modal_shows_tiktok_fee_lines_with_real_names(conn):
    _import_orders(conn)
    _upload(INCOME)
    oid = conn.execute("SELECT id FROM marketplace_orders WHERE order_sn=?", (O379,)).fetchone()[0]
    d = models.get_marketplace_order_detail(conn, oid)
    lines = {x['label']: x['amount'] for x in d['fee_lines']}
    assert lines == {'มูลค่าสินค้า': 435.0, 'ค่าคอมมิชชั่น': -41.89,
                     'ค่าคอมแอฟฟิลิเอต': -36.45, 'ค่าสนับสนุนการเติบโตร้านค้า': -32.58,
                     'ค่าธุรกรรมการชำระเงิน': -13.96, 'ค่าโครงสร้างพื้นฐาน': -1.07}
    assert round(sum(lines.values()), 2) == 309.05
    assert 'fee_raw_json' not in d['fees']


# ── margin: a cancelled order has none ───────────────────────────────────────

TT_SKUS = {'1737136796219442872': 9401, '1737136999694436024': 9402,
           '1736984084285458104': 9403, '1736984084285654712': 9404}


def _seed_costed_listings(conn, cost=None):
    """Each fixture SKU mapped to its own product; O379's product costs 20."""
    cost = cost or {9401: 20.0, 9402: 10.0, 9403: 10.0, 9404: 10.0}
    for vid, pid in TT_SKUS.items():
        conn.execute("INSERT INTO products (id, product_name, cost_price) VALUES (?,?,?)",
                     (pid, f'p{pid}', cost[pid]))
        conn.execute("INSERT INTO platform_skus (platform, product_name, variation_id, stock, "
                     "internal_product_id, qty_per_sale) VALUES ('tiktok', 'x', ?, 5, ?, 1)",
                     (vid, pid))
    conn.commit()


def _margin(conn, platform, sn):
    oid = conn.execute("SELECT id FROM marketplace_orders WHERE platform=? AND order_sn=?",
                       (platform, sn)).fetchone()[0]
    return models.get_order_margin(conn, oid)


def test_settled_cancel_has_no_margin_and_a_sale_keeps_its_own(conn):
    _seed_costed_listings(conn)
    _import_orders(conn)
    _upload(INCOME)
    m817 = _margin(conn, 'tiktok', O817)
    assert m817['net'] == 0.0                 # the fee row exists: the case that went red
    assert m817['margin'] is None and m817['margin_pct'] is None
    assert m817['cancelled'] is True
    m379 = _margin(conn, 'tiktok', O379)
    assert (m379['cogs'], m379['margin'], m379['margin_pct']) == (20.0, 289.05, 93.5)
    assert m379['cancelled'] is False


def test_shopee_cancel_without_settlement_is_unchanged(conn):
    """Control: an unsettled Shopee cancel had no margin before (no net) and has none now."""
    conn.execute("INSERT INTO products (id, product_name, cost_price) VALUES (9501, 's', 7)")
    conn.execute("INSERT INTO platform_skus (platform, product_name, variation_id, stock, "
                 "internal_product_id, qty_per_sale) VALUES ('shopee', 's', 'SV', 5, 9501, 1)")
    conn.execute("INSERT INTO marketplace_orders (platform, order_sn, status) "
                 "VALUES ('shopee', 'SPCAN1', 'ยกเลิกแล้ว')")
    oid = conn.execute("SELECT id FROM marketplace_orders WHERE order_sn='SPCAN1'").fetchone()[0]
    conn.execute("INSERT INTO marketplace_order_items (order_id, platform, order_sn, line_key, "
                 "internal_product_id, qty) VALUES (?, 'shopee', 'SPCAN1', 'k', 9501, 1)", (oid,))
    conn.commit()
    m = _margin(conn, 'shopee', 'SPCAN1')
    assert (m['net'], m['margin'], m['cogs']) == (None, None, 7.0)


def test_empty_income_file_warns_instead_of_reporting_success(conn, tmp_path):
    import openpyxl
    wb = openpyxl.load_workbook(INCOME_XLSX)
    wb['รายละเอียดคำสั่งซื้อ'].delete_rows(2, 3)
    for row in wb['รายงาน'].iter_rows():
        if row[1].value == 'ยอดการชำระเงินทั้งหมด':
            row[5].value = '0'
    empty = tmp_path / 'income_empty.xlsx'
    wb.save(empty)
    _import_orders(conn)
    html = _upload((str(empty), 'income_empty.xlsx'))
    assert 'alert-success' not in html
    assert 'ไม่มีรายการ' in html
    assert _fees(conn) == []
