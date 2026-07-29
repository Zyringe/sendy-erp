"""Pin the /products/<id>/trade bill-unit filter: one product can sell in
both แผง and ตัว, so the unit chips must summarize every unit while the
active filter narrows the summary/top_customers/monthly/docs blocks to just
that unit. sales_transactions.unit is already fully normalized (no acronym
leftovers) and no doc mixes units, so this filters on raw equality."""
import os

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


def _sale(conn, doc, unit, customer, qty=1, net=100, date='2026-01-01'):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(batch_id,date_iso,doc_no,doc_base,product_id,bsn_code,product_name_raw,"
        " customer,customer_code,qty,unit,unit_price,vat_type,discount,total,net,"
        " synced_to_stock) VALUES ('t',?,?,?,?,'C1','raw',?,'C1',?,?,10,0,0,?,?,1)",
        (date, doc, doc, PID, customer, qty, unit, net, net))


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
    _sale(empty_db_conn, 'IV2', 'แผง', 'B')
    _sale(empty_db_conn, 'IV3', 'ตัว', 'C')
    _sale(empty_db_conn, 'IV4', 'ตัว', 'D')
    _sale(empty_db_conn, 'IV5', 'ตัว', 'E')
    empty_db_conn.commit()

    data = models.get_product_trade_summary(PID)
    assert data['summary']['doc_count'] == 5
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
    assert {r['doc_no'] for r in data2['docs']} == {'IV3', 'IV4', 'IV5'}
    assert all(r['unit'] == 'ตัว' for r in data2['docs'])


def test_trade_page_renders_unit_chips_and_column(admin_client, empty_db_conn):
    _seed_product(empty_db_conn)
    _sale(empty_db_conn, 'IV1', 'แผง', 'A')
    _sale(empty_db_conn, 'IV2', 'ตัว', 'B')
    empty_db_conn.commit()

    resp = admin_client.get(f'/products/{PID}/trade?unit=ตัว')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'หน่วย' in html
    assert 'ตัว 1' in html
    assert 'แผง 1' in html
