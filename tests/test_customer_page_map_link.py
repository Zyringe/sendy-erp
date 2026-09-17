"""The desktop customer page must surface the saved Google-Map pin.

`customers` has carried lat/lng since migration 053 and the curated pins are
imported by scripts/import_customer_geomap.py, but until now only the MOBILE
page (templates/m/customer.html) rendered them -- on desktop the coordinates
were invisible, the same shape as the contact_note gap.

Assertions are scoped to the extracted map element, never a page-wide
substring: "แผนที่" already appears in the sidebar link to /customer-map, and
a bare `'maps' in html` would match that too.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
from urllib.parse import quote

import pytest

CODE_PIN = 'TESTMAP1'
CODE_NOPIN = 'TESTMAP2'
NAME_PIN = 'ลูกค้าทดสอบแผนที่ มีพิกัด'
NAME_NOPIN = 'ลูกค้าทดสอบแผนที่ ไม่มีพิกัด'
LAT, LNG = 18.7691990, 100.7643173          # กระต่ายเครื่องมือช่าง, จ.น่าน


def _mk(conn, code, name, lat=None, lng=None, gmap_name=None):
    conn.execute(
        "INSERT INTO customers (code, name, lat, lng, gmap_name) VALUES (?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, lat=excluded.lat, "
        "lng=excluded.lng, gmap_name=excluded.gmap_name",
        (code, name, lat, lng, gmap_name),
    )
    conn.commit()


def _client(role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _map_anchor(html):
    """The map element itself, so the assertion cannot be satisfied by the
    sidebar's own /customer-map link or by any script that mentions a URL."""
    m = re.search(r'<a[^>]*data-customer-map-link[^>]*>.*?</a>', html, re.S)
    return m.group(0) if m else None


@pytest.fixture
def cust(tmp_db_conn):
    conn = tmp_db_conn
    _mk(conn, CODE_PIN, NAME_PIN, LAT, LNG, 'กระต่ายเครื่องมือช่าง')
    _mk(conn, CODE_NOPIN, NAME_NOPIN)
    yield conn
    conn.execute("DELETE FROM customers WHERE code IN (?,?)", (CODE_PIN, CODE_NOPIN))
    conn.commit()


def test_customer_with_a_pin_renders_a_map_link_to_its_own_coordinates(cust):
    html = _client().get(f'/customer/code/{quote(CODE_PIN)}').data.decode()

    # Control: the page rendered the customer we asked for, so an empty
    # anchor below means "no map link", never "wrong page" or "not found".
    assert NAME_PIN in html

    anchor = _map_anchor(html)
    assert anchor is not None, 'no map link on the desktop customer page'
    assert f'{LAT},{LNG}' in anchor, anchor
    assert 'google.com/maps' in anchor, anchor


def test_customer_without_a_pin_renders_no_map_link(cust):
    html = _client().get(f'/customer/code/{quote(CODE_NOPIN)}').data.decode()

    # Same control: prove the page rendered before reading anything as absent.
    assert NAME_NOPIN in html
    assert _map_anchor(html) is None
