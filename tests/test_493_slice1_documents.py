"""TDD for #493 Slice 1 — customer page: one row per DOCUMENT, not per line.

Root bug (measured on prod for 23ท06): `sales_transactions.doc_no` carries a
per-line '-N' suffix, so grouping by it (customers.py's old `_customer_sales_
aggregates`) produced one "document" per LINE. 247 lines read as 247
documents when the real count is 66. The fix groups by `doc_base`.

Seam 1 (this file, primary): the customer summary/documents data contract,
proved on a synthetic customer this file owns end-to-end — tmp_db clones the
live dev DB WITH its data, so every test force-deletes then inserts its own
rows rather than inheriting any (verification-discipline.md). Prior art for
the insert helpers: test_price_lookup.py's _mk_product/_bill.

Seam 2: HTTP render, session-injected roles — prior art:
test_customer_code_route.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from urllib.parse import quote

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST493'
TEST_NAME = 'ลูกค้าทดสอบ 493'

_pid_counter = [493000]


def _mk_product(conn, name='สินค้าทดสอบ', unit_type='ตัว', base=100.0, cost=60.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()


def _clear_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                 (code, name))
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, qty, unit_price, net,
          vat_type=1, unit='ตัว', ref_invoice=None,
          customer_code=TEST_CODE, customer_name=TEST_NAME):
    doc_no = f"{doc_base}-{suffix}"
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, ref_invoice) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, doc_no, doc_base, pid, customer_name, customer_code,
         qty, unit, unit_price, vat_type, net, net, ref_invoice),
    )
    conn.commit()


@pytest.fixture
def cust(tmp_db_conn):
    _mk_customer(tmp_db_conn)
    _clear_customer(tmp_db_conn)
    pid = _mk_product(tmp_db_conn)
    yield tmp_db_conn, pid
    _clear_customer(tmp_db_conn)


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


# ── Seam 1: document grouping (models.get_customer_summary_by_code) ────────

def test_multi_line_invoice_is_one_document_row(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49301', suffix=1, pid=pid, date_iso='2026-01-05',
          qty=2, unit_price=100, net=200, vat_type=0)
    _line(conn, doc_base='IV49301', suffix=2, pid=pid, date_iso='2026-01-05',
          qty=1, unit_price=50, net=50, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    docs = [d for d in data['docs'] if d['doc_base'] == 'IV49301']
    assert len(docs) == 1
    assert docs[0]['item_count'] == 2
    assert docs[0]['total'] == pytest.approx(250)
    assert data['summary']['doc_count'] == 1


def test_split_vat_document_adds_vat_to_total(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49302', suffix=1, pid=pid, date_iso='2026-01-06',
          qty=1, unit_price=1000, net=1000, vat_type=2)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    docs = [d for d in data['docs'] if d['doc_base'] == 'IV49302']
    assert len(docs) == 1
    assert docs[0]['total'] == pytest.approx(1070)
    assert docs[0]['vat_type'] == 2


def test_credit_note_is_negative_with_reference_and_not_last_purchase(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49303', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=500, net=500, vat_type=1)
    _line(conn, doc_base='SR49304', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=500, net=500, vat_type=1, ref_invoice='IV49303')
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    sr = next(d for d in data['docs'] if d['doc_base'] == 'SR49304')
    assert sr['is_credit_note'] is True
    assert sr['total'] == pytest.approx(-500)
    assert sr['ref_invoice'] == 'IV49303'
    # ซื้อล่าสุด must be the invoice date, never the later credit note's date,
    # even though the SR is chronologically more recent.
    assert data['summary']['last_purchase_date'] == '2026-01-01'


def test_more_than_200_documents_all_present(cust):
    conn, pid = cust
    for i in range(205):
        _line(conn, doc_base=f'IV493{1000+i}', suffix=1, pid=pid,
              date_iso='2026-03-01', qty=1, unit_price=10, net=10, vat_type=0)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    ours = [d for d in data['docs'] if d['doc_base'].startswith('IV4931')
            or d['doc_base'].startswith('IV4930')]
    assert len(ours) == 205


def test_freebie_only_line_does_not_move_last_purchase_date_forward(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49305', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=500, net=500, vat_type=1)
    # A later ฿0 freebie-only line must not count as evidence of a purchase.
    _line(conn, doc_base='IV49306', suffix=1, pid=pid, date_iso='2026-04-01',
          qty=1, unit_price=0, net=0, vat_type=1)
    import models
    data = models.get_customer_summary_by_code(TEST_CODE)
    assert data['summary']['last_purchase_date'] == '2026-01-01'


def test_customer_list_doc_count_counts_documents_not_lines(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49307', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=10, net=10, vat_type=0)
    _line(conn, doc_base='IV49307', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=10, net=10, vat_type=0)
    _line(conn, doc_base='IV49308', suffix=1, pid=pid, date_iso='2026-01-02',
          qty=1, unit_price=10, net=10, vat_type=0)
    import models
    rows, _total = models.get_customers(search=TEST_CODE)
    row = next(r for r in rows if r['customer_code'] == TEST_CODE)
    assert row['doc_count'] == 2


def test_mobile_customer_documents_grouped_by_doc_base(cust):
    conn, pid = cust
    _line(conn, doc_base='IV49309', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    _line(conn, doc_base='IV49309', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=50, net=50, vat_type=0)
    import models
    docs = models.get_customer_documents('customer', TEST_NAME, limit=5)
    ours = [d for d in docs if d['doc_base'] == 'IV49309']
    assert len(ours) == 1
    assert ours[0]['item_count'] == 2
    assert ours[0]['total'] == pytest.approx(150)


# ── Seam 2: HTTP render ─────────────────────────────────────────────────────

def test_customer_page_shows_pre_vat_label_and_last_purchase(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49310', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=500, net=500, vat_type=1)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert '(ก่อน VAT)' in html
    assert 'ซื้อล่าสุด' in html
    assert '2026-01-01' in html


def test_customer_page_document_list_has_one_row_per_document(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49311', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    _line(conn, doc_base='IV49311', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=50, net=50, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert html.count('IV49311') == 1


def test_invoice_page_has_back_control(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49312', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get('/sales/doc/IV49312').data.decode()
    assert 'goBackOrSales' in html or 'history.back' in html


def test_mobile_customer_page_lists_documents_not_lines(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49313', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, vat_type=0)
    _line(conn, doc_base='IV49313', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=50, net=50, vat_type=0)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/m/customer/{quote(TEST_NAME)}').data.decode()
    assert html.count('IV49313') == 1
    assert '2 รายการ' in html
