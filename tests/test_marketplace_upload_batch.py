"""Route-level tests for /marketplace/upload — the one box that takes every
marketplace export at once.

The weekly job uploads three Shopee files in one go. Windows hands them to the
browser in display order, which is alphabetical:

    Income.โอนเงินสำเร็จ.th.<range>.xlsx      -> 'I'
    my_balance_transaction_report.<range>.xlsx -> 'm'
    Order.all.<range>.xlsx                     -> 'O'

so the Income file arrives FIRST and the Order file LAST. Payouts are stamped
onto orders with `UPDATE marketplace_orders ... WHERE order_sn = ?`, so an
Income file processed before its Order file matches nothing and every payout in
it is dropped — with no row kept anywhere to replay it from. These tests pin the
batch contract that stops that, and stop the route reporting success when it
did not fully succeed.
"""
import io
import os

import pandas as pd
import pytest

os.environ.setdefault('SKIP_DB_INIT', '1')

# Unique to this test module so it can never collide with live-DB rows.
ORDER_SN = 'TESTUPLOADSN001'
PAYOUT = 123.45
SETTLED_AT = '2026-07-15 10:00'

_INCOME_COLS = ['หมายเลขคำสั่งซื้อ', 'วันที่โอนชำระเงินสำเร็จ',
                'จำนวนเงินทั้งหมดที่โอนแล้ว (฿)', 'สินค้าราคาปกติ',
                'ค่าคอมมิชชั่น', 'ค่าธรรมเนียม (%)', 'วันที่ทำการสั่งซื้อ',
                'ชื่อผู้ใช้ (ผู้ซื้อ)']

_ORDER_COLS = ['หมายเลขคำสั่งซื้อ', 'สถานะการสั่งซื้อ', 'วันที่ทำการสั่งซื้อ',
               'เวลาการชำระสินค้า', 'ชื่อสินค้า', 'จำนวน', 'ราคาขาย',
               'ราคาขายสุทธิ', 'จำนวนเงินทั้งหมด', 'ชื่อผู้รับ']


def _income_xlsx(order_sn=ORDER_SN, payout=PAYOUT, settled_at=SETTLED_AT):
    """Minimal but real Shopee Income Transfer workbook.

    detect_file keys on the sheet names {'Income','Service Fee Details'};
    load_income_sheet scans past the metadata banner for the header row, so the
    two banner rows here mirror a genuine export.
    """
    banner = pd.DataFrame([['รายงานการโอนเงิน', None], ['ร้าน: test', None]])
    body = pd.DataFrame(
        [[order_sn, settled_at, str(payout), '150.00', '-26.55', '17%',
          '2026-07-10', 'buyer1']],
        columns=_INCOME_COLS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        banner.to_excel(w, sheet_name='Income', index=False, header=False)
        body.to_excel(w, sheet_name='Income', index=False, startrow=2)
        pd.DataFrame([['x']]).to_excel(
            w, sheet_name='Service Fee Details', index=False, header=False)
    buf.seek(0)
    return buf


def _order_xlsx(order_sn=ORDER_SN):
    """Minimal Shopee order export (flat, header on row 0 — no banner)."""
    body = pd.DataFrame(
        [[order_sn, 'สำเร็จแล้ว', '2026-07-10 09:00', '2026-07-10 09:05',
          'สินค้าทดสอบ', '1', '150.00', '150.00', '150.00', 'ผู้รับทดสอบ']],
        columns=_ORDER_COLS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        body.to_excel(w, sheet_name='Sheet1', index=False)
    buf.seek(0)
    return buf


def _junk_xlsx():
    """A real xlsx that matches no known export — detect_file returns (None, None)."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        pd.DataFrame([['nothing', 'useful']]).to_excel(
            w, sheet_name='Sheet1', index=False)
    buf.seek(0)
    return buf


def _broken_income_xlsx():
    """Detects as 'income' (right sheet names) but RAISES on parse (no required
    columns). This is the abort path — an unrecognised file is merely skipped
    via `continue`, so only a raising file proves per-file isolation.
    """
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        pd.DataFrame([['หมายเลขคำสั่งซื้อ ไม่มีจริง'], ['x']]).to_excel(
            w, sheet_name='Income', index=False, header=False)
        pd.DataFrame([['x']]).to_excel(
            w, sheet_name='Service Fee Details', index=False, header=False)
    buf.seek(0)
    return buf


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def _post(client, files):
    """POST a multi-file batch exactly as the settlement form does."""
    return client.post(
        '/marketplace/upload',
        data={'files': files},
        content_type='multipart/form-data',
        follow_redirects=True)


@pytest.fixture
def clean_order(tmp_db_conn):
    """Guarantee the test order_sn is absent before each run."""
    c = tmp_db_conn
    for t in ('marketplace_order_fees', 'marketplace_orders'):
        c.execute(f"DELETE FROM {t} WHERE order_sn = ?", (ORDER_SN,))
    c.commit()
    return c


def test_order_lands_before_income_so_payout_is_not_dropped(tmp_db, clean_order):
    """THE money-path regression.

    Income is listed first (as Windows sorts it). The route must still process
    the Order file first, or the payout in the Income file matches no order row
    and is silently discarded.
    """
    resp = _post(_client(), [
        (_income_xlsx(), 'Income.โอนเงินสำเร็จ.th.20260703_20260714.xlsx'),
        (_order_xlsx(), 'Order.all.20260703_20260714.xlsx'),
    ])
    assert resp.status_code == 200

    row = clean_order.execute(
        "SELECT actual_payout, settled_at FROM marketplace_orders "
        "WHERE platform='shopee' AND order_sn=?", (ORDER_SN,)).fetchone()
    assert row is not None, 'order file was not imported at all'
    assert row['actual_payout'] == PAYOUT, (
        'payout was dropped — the Income file was processed before the Order '
        'file, so its UPDATE matched nothing')


def test_unrecognised_file_does_not_report_success(tmp_db, clean_order):
    """A skipped file must not come back on a green 'success' banner."""
    resp = _post(_client(), [(_junk_xlsx(), 'random.xlsx')])
    html = resp.get_data(as_text=True)
    assert 'ไม่รู้จักชนิดไฟล์' in html, 'the skip should be reported at all'
    assert 'alert-success' not in html, (
        'a file that imported NOTHING was reported on a green success banner')


def test_one_raising_file_does_not_abort_the_rest_of_the_batch(tmp_db, clean_order):
    """Per-file isolation.

    A file that throws mid-parse must not take the rest of the batch with it.
    Today the whole loop sits under one try/except, so the first raise abandons
    every file after it — while the files BEFORE it are already committed
    (each models.* call commits internally), and the user is told
    'นำเข้าไม่สำเร็จ' as though nothing landed.
    """
    resp = _post(_client(), [
        (_broken_income_xlsx(), 'Income.เสีย.xlsx'),
        (_order_xlsx(), 'Order.all.20260703_20260714.xlsx'),
        (_income_xlsx(), 'Income.โอนเงินสำเร็จ.th.20260703_20260714.xlsx'),
    ])
    html = resp.get_data(as_text=True)
    row = clean_order.execute(
        "SELECT actual_payout FROM marketplace_orders "
        "WHERE platform='shopee' AND order_sn=?", (ORDER_SN,)).fetchone()
    assert row is not None, 'a raising file aborted the batch before the good ones ran'
    assert row['actual_payout'] == PAYOUT
    assert 'Income.เสีย.xlsx' in html, 'the failing file must be named to the user'


def test_order_kind_flash_surfaces_skipped_lines(tmp_db, clean_order):
    """Task 3.3: the multi-file box's 'order' kind must surface the same
    skipped-lines note as the single-file /marketplace/import route — the
    order xlsx's one line ('สินค้าทดสอบ') matches no platform_skus row, so
    it resolves to neither a product nor a listing."""
    resp = _post(_client(), [(_order_xlsx(), 'Order.all.20260820.xlsx')])
    html = resp.get_data(as_text=True)
    assert 'ข้ามการหักสต็อก 1 บรรทัด (หา listing ไม่พบ)' in html
    assert ORDER_SN in html


def test_batch_leaves_an_import_log_row_even_with_no_files(tmp_db, tmp_db_conn):
    """Durable forensics.

    Railway's stderr logs vanish on container restart and the route swallows
    exceptions into a flash, so an import_log row is the ONLY evidence that
    survives. It must be written on entry — before any parsing — so it exists
    even when the batch dies.
    """
    before = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'marketplace:%'"
    ).fetchone()[0]
    _post(_client(), [])
    after = tmp_db_conn.execute(
        "SELECT notes FROM import_log WHERE filename LIKE 'marketplace:%' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    count = tmp_db_conn.execute(
        "SELECT COUNT(*) FROM import_log WHERE filename LIKE 'marketplace:%'"
    ).fetchone()[0]
    assert count == before + 1, 'an empty submit left no trace to diagnose from'
    assert '0' in (after['notes'] or ''), 'the file count must be recorded'


# ── Rollback on a mid-import failure (order-driven-platform-deduction) ─────

ROLLBACK_SN = 'TESTUPLOADSN-ROLLBACK-001'
ROLLBACK_VID = 'ROLLBACK-VID-001'


@pytest.fixture
def clean_rollback_marker(tmp_db_conn):
    """A dedicated platform_skus row so the test can assert its stock is
    UNTOUCHED after a rolled-back import, independent of whatever real
    listings the live-DB clone happens to carry."""
    c = tmp_db_conn
    c.execute("DELETE FROM marketplace_orders WHERE order_sn = ?", (ROLLBACK_SN,))
    c.execute("DELETE FROM platform_skus WHERE variation_id = ?", (ROLLBACK_VID,))
    c.commit()
    sku_id = c.execute(
        "INSERT INTO platform_skus (platform, variation_id, product_name, stock) "
        "VALUES ('shopee', ?, 'rollback marker', 20)", (ROLLBACK_VID,)).lastrowid
    c.commit()
    return c, sku_id


def test_failed_order_import_rolls_back_partial_writes(tmp_db, clean_rollback_marker,
                                                        monkeypatch):
    """A file that raises mid-import must leave NOTHING behind.

    import_marketplace_orders now holds stock/provenance writes inside its
    open transaction (order-driven-platform-deduction, Task 1.3), so the
    except handler committing (via _log_import) without rolling back first
    would make a PARTIAL import durable — a silently corrupted mirror (stock
    deducted with orphan/missing provenance), not merely an unimported order
    row. Simulate the exact shape: one real write on the route's own conn
    (header insert + a stock deduction + its provenance row), then raise —
    mirroring what a real partial import_marketplace_orders run leaves
    in-flight at the moment it dies.
    """
    import models
    conn, sku_id = clean_rollback_marker

    def _boom(route_conn, orders, source_file=None):
        oid = route_conn.execute(
            "INSERT INTO marketplace_orders (platform, order_sn, status) "
            "VALUES ('shopee', ?, 'MARKER')", (ROLLBACK_SN,)).lastrowid
        route_conn.execute(
            "UPDATE platform_skus SET stock = stock - 5 WHERE id=?", (sku_id,))
        route_conn.execute(
            "INSERT INTO platform_stock_deductions "
            "(source_table, source_id, platform_sku_id, units) "
            "VALUES ('marketplace_orders', ?, ?, 5)", (oid, sku_id))
        raise RuntimeError('simulated mid-import failure')

    monkeypatch.setattr(models, 'import_marketplace_orders', _boom)

    resp = _post(_client(), [(_order_xlsx(order_sn=ROLLBACK_SN), 'Order.all.rollback.xlsx')])
    assert resp.status_code == 200

    html = resp.get_data(as_text=True)
    assert 'นำเข้าไม่สำเร็จ' in html

    order_row = conn.execute(
        "SELECT id FROM marketplace_orders WHERE order_sn=?", (ROLLBACK_SN,)).fetchone()
    assert order_row is None, (
        'marker order header survived — the partial import was not rolled back')

    stock = conn.execute(
        "SELECT stock FROM platform_skus WHERE id=?", (sku_id,)).fetchone()['stock']
    assert stock == 20, 'stock write survived the failure — mirror silently corrupted'

    prov_count = conn.execute(
        "SELECT COUNT(*) FROM platform_stock_deductions WHERE platform_sku_id=?",
        (sku_id,)).fetchone()[0]
    assert prov_count == 0, 'a provenance row survived a rolled-back import'

    log_row = conn.execute(
        "SELECT notes FROM import_log WHERE notes LIKE 'marketplace:order:ERROR%' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert log_row is not None, (
        'the import_log ERROR row must survive — it is written AFTER the rollback')
