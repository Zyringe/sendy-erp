"""#494 — ยอดซื้อรวม: a customer's purchases before VAT, net of credit notes,
defined once (`sales_filters.purchase_net_sql`) and read by every surface that
shows a per-customer purchase total.

The bug: credit-note (SR) lines are stored with positive qty and net, and the
header summed a bare SUM(net), so returns were ADDED. Measured on prod:
56ช001's header read ฿94,140.00 while its own document list summed to
฿50,460.00.

Every expected figure below is worked by hand from the seeded rows, never
recomputed the way the code does. tmp_db clones the live dev DB WITH its data,
so each test deletes its own keys first and seeds exactly what it asserts.
"""
import json
import os
import re
import sqlite3
from html import unescape
from urllib.parse import quote, urlsplit

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได (own-brand), same constant test_493_* uses

CODE = 'T494A'
NAME = 'ลูกค้าทดสอบ 494'
SR_CODE = 'T494B'
SR_NAME = 'ลูกค้าคืนของทดสอบ 494'

_DOCS = ('IV49401', 'IV49402', 'HS49403', 'SR49404', 'IV49405', 'SR49409')


def _reset(conn):
    conn.execute(
        "DELETE FROM sales_transactions WHERE customer_code IN (?, ?) "
        "OR customer IN (?, ?) OR doc_base IN ({})".format(','.join('?' * len(_DOCS))),
        (CODE, SR_CODE, NAME, SR_NAME) + _DOCS)
    conn.execute("DELETE FROM ar_writeoffs WHERE doc_no IN ({})".format(
        ','.join('?' * len(_DOCS))), _DOCS)
    for code, name in ((CODE, NAME), (SR_CODE, SR_NAME)):
        conn.execute(
            "INSERT INTO customers (code, name) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (code, name))
    conn.commit()


def _product(conn):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES ('สินค้าทดสอบ 494', 'ตัว', 100, 60, ?, 1)",
        (SENDAI_BRAND_ID,))
    conn.commit()
    return cur.lastrowid


def _line(conn, doc_base, net, *, date_iso, vat_type=1, code=CODE, name=NAME, pid=None):
    """One sales line in the real shape: doc_no '<base>-1', doc_base '<base>'.
    A credit note is stored exactly like the real ones: positive qty and net."""
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, "
        " customer, customer_code, qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, f'{doc_base}-1', doc_base, pid, name, code, 1, 'ตัว', net,
         vat_type, net, net))
    conn.commit()


def _client():
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c


def _num(text):
    return float(text.replace('฿', '').replace(',', '').strip())


def _header_total(html):
    """The ยอดซื้อรวม stat card's own value element, not a page-wide search."""
    m = re.search(r'ยอดซื้อรวม \(ก่อน VAT\)</div>\s*'
                  r'<div class="stat-card-value[^"]*">([^<]+)</div>', html)
    assert m, 'ยอดซื้อรวม card did not render'
    return _num(m.group(1))


def _monthly_series(html):
    m = re.search(r'const monthly = (\[.*?\]);', html)
    assert m, 'monthly chart data did not render'
    return json.loads(m.group(1))


def _link_paths(html):
    """Path of every <a href> on the page, query string dropped, so a pin on a
    document link survives the link gaining a query (#523 adds ?book=...)."""
    return [urlsplit(unescape(h)).path
            for h in re.findall(r'<a\b[^>]*\bhref="([^"]*)"', html)]


def _row_for(html, marker):
    """The one table row carrying `marker`, so a figure is read off THIS
    customer's row and never off a neighbour's."""
    rows = [r for r in re.findall(r'<tr\b.*?</tr>', html, re.S) if marker in r]
    assert len(rows) == 1, f'expected exactly one row carrying {marker!r}, got {len(rows)}'
    return rows[0]


# The fixture every surface is read from. Worked by hand:
#   IV49401  2026-01  ไม่บวก VAT        +1,000
#   IV49402  2026-02  แยก VAT           +1,000   before VAT, never 1,070
#   HS49403  2026-03  HS cash sale        +200   counted, as the document list shows it
#   SR49404  2026-04  credit note, +300   −300
#   IV49405  2026-03  invoiced in error  excluded (ar_writeoffs.excludes_revenue = 1)
# ยอดซื้อรวม = 1,000 + 1,000 + 200 − 300 = 1,900
EXPECTED_TOTAL = 1900.0


def _seed_full_customer(conn):
    _reset(conn)
    pid = _product(conn)
    _line(conn, 'IV49401', 1000, date_iso='2026-01-10', vat_type=1, pid=pid)
    _line(conn, 'IV49402', 1000, date_iso='2026-02-10', vat_type=2, pid=pid)
    _line(conn, 'HS49403', 200, date_iso='2026-03-10', vat_type=1, pid=pid)
    _line(conn, 'SR49404', 300, date_iso='2026-04-10', vat_type=1, pid=pid)
    _line(conn, 'IV49405', 5000, date_iso='2026-03-20', vat_type=1, pid=pid)
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_code, customer_name, amount, "
        " type, writeoff_date, excludes_revenue) "
        "VALUES ('IV49405', ?, ?, 5000, 'expense', '2026-04-01', 1)", (CODE, NAME))
    conn.commit()


# ── The sign rule ────────────────────────────────────────────────────────────

def test_header_and_monthly_subtract_a_credit_note(tmp_db_conn):
    conn = tmp_db_conn
    _reset(conn)
    _line(conn, 'IV49401', 1000, date_iso='2026-01-10')
    _line(conn, 'SR49404', 300, date_iso='2026-02-10')

    import models
    data = models.get_customer_summary_by_code(CODE)

    # CONTROL: both documents reached the aggregate.
    assert data['summary']['doc_count'] == 2
    assert data['summary']['total_net'] == pytest.approx(700.0), (
        'credit note SR49404 (฿300) must be SUBTRACTED from ยอดซื้อรวม: '
        '1,000 − 300 = 700, not 1,000 + 300 = 1,300')
    assert len(data['monthly']) == 2
    months = {m['month']: m['total_net'] for m in data['monthly']}
    assert months == pytest.approx({'2026-01': 1000.0, '2026-02': -300.0}), (
        'the credit note month must chart as a NEGATIVE bar')


def test_credit_note_only_customer_renders_negative(tmp_db):
    """01พ14 on the dev DB: one document, a ฿690 credit note, and a header that
    read ยอดซื้อรวม ฿690.00. A customer who only returned goods bought −฿690."""
    conn = sqlite3.connect(tmp_db)
    _reset(conn)
    _line(conn, 'SR49409', 690, date_iso='2026-03-05', code=SR_CODE, name=SR_NAME)
    conn.close()

    html = _client().get(f'/customer/code/{quote(SR_CODE)}').data.decode()

    # CONTROL: the page resolved this customer and listed its one document.
    links = _link_paths(html)
    assert links, 'no <a href> parsed off the page'
    assert '/sales/doc/SR49409' in links
    assert _header_total(html) == -690.0
    series = _monthly_series(html)
    assert len(series) == 1
    assert series[0]['month'] == '2026-03'
    assert series[0]['total_net'] == pytest.approx(-690.0)


# ── One figure on every surface ──────────────────────────────────────────────

def test_every_surface_shows_the_same_purchase_total(tmp_db):
    """Header, /customers row, mobile ยอดสะสม, call card and the /call list's
    spend (window "all") must all read ฿1,900 for the same customer. Each
    figure is taken from its own element, after a CONTROL that the element is
    this customer's."""
    conn = sqlite3.connect(tmp_db)
    _seed_full_customer(conn)
    conn.close()
    c = _client()
    seen = {}

    # Customer page header
    html = c.get(f'/customer/code/{quote(CODE)}').data.decode()
    assert '/sales/doc/IV49401' in _link_paths(html)   # CONTROL: this customer's page
    seen['header'] = _header_total(html)

    # /customers list: the row whose รหัส cell is this code
    html = c.get(f'/customers?q={quote(CODE)}').data.decode()
    row = _row_for(html, f'<span class="badge badge-gray font-mono">{CODE}</span></td>')
    m = re.search(r'data-label="ยอดรวม \(฿\)"[^>]*>\s*([^<]+?)\s*</td>', row)
    assert m, 'ยอดรวม cell missing from the row'
    seen['customers_list'] = _num(m.group(1))

    # Mobile customer page (code-keyed)
    html = c.get(f'/m/customer/code/{quote(CODE)}').data.decode()
    assert '/sales/doc/IV49401' in _link_paths(html)   # CONTROL: this customer's documents
    m = re.search(r'ยอดสะสม \(ก่อน VAT\)</div>\s*<div[^>]*>([^<]+)</div>', html)
    assert m, 'ยอดสะสม card did not render'
    seen['mobile'] = _num(m.group(1))

    # Call card: the not-found branch redirects, so 200 is this customer's card
    resp = c.get(f'/call/{quote(CODE)}')
    assert resp.status_code == 200                     # CONTROL
    m = re.search(r'<div class="k">ยอดซื้อทั้งหมด</div><div class="v">([^<]+)</div>',
                  resp.data.decode())
    assert m, 'ยอดซื้อทั้งหมด did not render'
    seen['call_card'] = _num(m.group(1))

    # /call list, all-time window: this customer's row
    html = c.get(f'/call?q={quote(CODE)}&spend_window=all').data.decode()
    row = _row_for(html, f"/call/{CODE}'")            # CONTROL: exactly one row
    m = re.search(r'<td class="num cc-spend">\s*([^<]+?)\s*</td>', row)
    assert m, 'spend cell missing from the row'
    seen['call_list'] = _num(m.group(1))

    assert len(seen) == 5
    assert seen == {k: EXPECTED_TOTAL for k in seen}, (
        f'every surface must read ฿1,900 (credit note subtracted, before VAT, '
        f'HS counted, the document invoiced in error excluded): {seen}')
