"""Pin the /products/<id>/trade bill-unit filter: one product can sell in
both แผง and ตัว, so the unit chips must summarize every unit while the
active filter narrows the summary/top_customers/monthly/docs blocks to just
that unit. sales_transactions.unit is already fully normalized (no acronym
leftovers), so this filters on raw equality.

Rows are seeded in the real shape (doc_no = '<base>-<n>', doc_base =
'<base>'), with IV1 carrying the product on two lines, so an invoice count
and a line count come out different (#496). Seeding doc_no == doc_base, as
this file used to, could not tell them apart."""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models

PID = 950101


def _seed_product(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
        "VALUES (?, ?, ?, ?, 1)", (PID, 'TRADEUNIT', 'ตัว', 'SKU-TRADEUNIT'))
    conn.commit()


def _sale(conn, doc, unit, customer, qty=1, net=100, date='2026-01-01', line=1):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        " customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        " synced_to_stock) VALUES ('t',?,?,?,?,'C1','raw',?,'C1',?,?,10,0,0,?,?,1)",
        (date, f'{doc}-{line}', doc, PID, customer, qty, unit, net, net))


@pytest.fixture
def admin_client(empty_db):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 't'
        sess['role'] = 'admin'
    return c


def test_units_breakdown_and_unit_filter(empty_db_conn):
    _seed_product(empty_db_conn)
    _sale(empty_db_conn, 'IV1', 'แผง', 'A')
    _sale(empty_db_conn, 'IV1', 'แผง', 'A', net=0, line=2)    # freebie, same invoice
    _sale(empty_db_conn, 'IV2', 'แผง', 'B')
    _sale(empty_db_conn, 'IV3', 'ตัว', 'C')
    _sale(empty_db_conn, 'IV4', 'ตัว', 'D')
    _sale(empty_db_conn, 'IV5', 'ตัว', 'E')
    empty_db_conn.commit()

    data = models.get_product_trade_summary(PID)
    assert data['summary']['doc_count'] == 5              # 6 lines
    assert data['units'] == [
        {'unit': 'ตัว', 'doc_count': 3},
        {'unit': 'แผง', 'doc_count': 2},
    ]
    assert data['unit'] is None

    data2 = models.get_product_trade_summary(PID, unit='ตัว')
    assert data2['summary']['doc_count'] == 3
    assert data2['unit'] == 'ตัว'
    # Chips must always show every unit + counts even while one is active.
    assert data2['units'] == data['units']
    assert {r['customer'] for r in data2['top_customers']} == {'C', 'D', 'E'}
    assert len(data2['monthly']) == 1
    assert len(data2['docs']) == 3
    assert {r['doc_base'] for r in data2['docs']} == {'IV3', 'IV4', 'IV5'}
    assert [u['unit'] for r in data2['docs'] for u in r['units']] == ['ตัว'] * 3

    data3 = models.get_product_trade_summary(PID, unit='แผง')
    assert data3['summary']['doc_count'] == 2
    # IV1's two แผง lines are one row.
    assert sorted(r['doc_base'] for r in data3['docs']) == ['IV1', 'IV2']


def test_trade_page_renders_unit_chips_and_column(admin_client, empty_db_conn):
    _seed_product(empty_db_conn)
    _sale(empty_db_conn, 'IV1', 'แผง', 'A')
    _sale(empty_db_conn, 'IV2', 'ตัว', 'B')
    empty_db_conn.commit()

    resp = admin_client.get(f'/products/{PID}/trade?unit=ตัว')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    chips = re.findall(r'class="btn btn-outline-secondary[^"]*">([^<]+)</a>', html)
    assert len(chips) == 3
    assert chips[0] == 'ทั้งหมด 2' and sorted(chips[1:]) == ['ตัว 1', 'แผง 1']
    # The unit now reads inside the จำนวน cell ("1.0 ตัว"), not a column of its own.
    assert re.findall(r'<td class="text-end small text-subtle">\s*([^<]+?)\s*</td>', html) \
        == ['1.0 ตัว']
