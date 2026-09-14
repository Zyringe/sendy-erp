"""#496: sales-side counts labelled เอกสาร/บิล count INVOICES, not invoice lines.

A `sales_transactions.doc_no` is one LINE of an invoice (`IV6901304-1`); the
invoice is `doc_base`. Every reader below used `COUNT(DISTINCT doc_no)` or
`GROUP BY doc_no`, which on the sales table is a line count. The fix changes
only the GRAIN: each page keeps exactly the population (filters, dates, unit
selection, SR in or out) it had before.

Purchase-side counts are correct and stay: `purchase_transactions.doc_no` IS
the document (lines are told apart by `line_seq`, mig 091).

Fixture, seeded in the real shape (`doc_no = '<base>-<n>'`, `doc_base =
'<base>'`) on `empty_db` (the full live schema, zero rows, so nothing is
inherited from the dev DB). Every expected number below is a hand count of
this table, not a re-run of the code's query.

  doc       date        customer  lines
  IV49601   2031-03-02  X         P1 24 ตัว ฿3,840  +  P1 3 ตัว ฿0 (freebie, unit_price 0)
  IV49602   2031-03-03  X         P1 5 ตัว ฿800    +  P2 1 ตัว ฿50   (two products)
  IV49603   2031-03-04  Y         P1 2 แผง ฿3,000  +  P1 4 ตัว ฿640  (one product, two units)
  IV49604   2031-03-05  X         P1 10 ตัว ฿1,600 +  P1 1 ตัว ฿0 (freebie priced 160, 100% off)
  SR49601   2031-03-06  X         P1 -2 ตัว -฿320  +  P1 -1 ตัว -฿160 (credit note)
  HP49601   2031-03-02  supplier  purchase, 2 lines (doc_no = doc_base, line_seq 1/2)
  HP49602   2031-03-03  supplier  purchase, 1 line
"""
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import models

P1, P2 = 949601, 949602
X_NAME, X_CODE = 'ร้านทดสอบ496 ก', 'T496A'
Y_NAME, Y_CODE = 'ร้านทดสอบ496 ข', 'T496B'
SUPPLIER = 'ผู้จำหน่ายทดสอบ496'
MONTH_FROM, MONTH_TO = '2031-03-01', '2031-03-31'

# (doc_base, n, date, customer, product, qty, unit, unit_price, discount, net)
SALES = [
    ('IV49601', 1, '2031-03-02', 'X', P1, 24, 'ตัว', 160, None, 3840),
    ('IV49601', 2, '2031-03-02', 'X', P1, 3, 'ตัว', 0, None, 0),
    ('IV49602', 1, '2031-03-03', 'X', P1, 5, 'ตัว', 160, None, 800),
    ('IV49602', 2, '2031-03-03', 'X', P2, 1, 'ตัว', 50, None, 50),
    ('IV49603', 1, '2031-03-04', 'Y', P1, 2, 'แผง', 1500, None, 3000),
    ('IV49603', 2, '2031-03-04', 'Y', P1, 4, 'ตัว', 160, None, 640),
    ('IV49604', 1, '2031-03-05', 'X', P1, 10, 'ตัว', 160, None, 1600),
    ('IV49604', 2, '2031-03-05', 'X', P1, 1, 'ตัว', 160, '100%', 0),
    ('SR49601', 1, '2031-03-06', 'X', P1, -2, 'ตัว', 160, None, -320),
    ('SR49601', 2, '2031-03-06', 'X', P1, -1, 'ตัว', 160, None, -160),
]
_CUST = {'X': (X_NAME, X_CODE), 'Y': (Y_NAME, Y_CODE)}


def _seed(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    for pid, name in ((P1, 'สินค้าทดสอบ496 หนึ่ง'), (P2, 'สินค้าทดสอบ496 สอง')):
        conn.execute(
            "INSERT INTO products (id, product_name, unit_type, sku_code, is_active,"
            " base_sell_price, cost_price) VALUES (?, ?, 'ตัว', ?, 1, 200, 100)",
            (pid, name, f'SKU-496-{pid}'))
    for name, code in _CUST.values():
        conn.execute("INSERT INTO customers (code, name) VALUES (?, ?)", (code, name))
    for base, n, date, cust, pid, qty, unit, up, disc, net in SALES:
        name, code = _CUST[cust]
        conn.execute(
            "INSERT INTO sales_transactions (batch_id, date_iso, doc_no, doc_base,"
            " product_id, bsn_code, product_name_raw, customer, customer_code, qty,"
            " unit, unit_price, vat_type, discount, total, net, synced_to_stock)"
            " VALUES ('t', ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, ?, 1, ?, ?, ?, 1)",
            (date, f'{base}-{n}', base, pid, f'B{pid}', name, code, qty, unit, up,
             disc, net, net))
    for doc, seq, date, pid, qty, net in (('HP49601', 1, '2031-03-02', P1, 100, 8000),
                                          ('HP49601', 2, '2031-03-02', P2, 50, 1500),
                                          ('HP49602', 1, '2031-03-03', P1, 20, 1600)):
        conn.execute(
            "INSERT INTO purchase_transactions (batch_id, date_iso, doc_no, doc_base,"
            " line_seq, product_id, bsn_code, product_name_raw, supplier, qty, unit,"
            " unit_price, vat_type, total, net, synced_to_stock)"
            " VALUES ('t', ?, ?, ?, ?, ?, ?, 'raw', ?, ?, 'ตัว', 80, 1, ?, ?, 1)",
            (date, doc, doc, seq, pid, f'B{pid}', SUPPLIER, qty, net, net))
    conn.commit()


@pytest.fixture
def seeded(empty_db_conn):
    _seed(empty_db_conn)
    return empty_db_conn


@pytest.fixture
def admin(seeded):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 't'
        sess['role'] = 'admin'
    return c


def _oracle(conn, sql, *params):
    """Independent count straight off the seeded table."""
    return conn.execute(sql, params).fetchone()[0]


def test_fixture_is_the_real_shape_and_lines_outnumber_invoices(seeded):
    """CONTROL: every sales row is '<base>-<n>', and the fixture really does
    hold more lines than invoices, or the tests below could not tell the two
    counts apart."""
    assert _oracle(seeded, "SELECT COUNT(*) FROM sales_transactions") == 10
    assert _oracle(seeded, "SELECT COUNT(*) FROM sales_transactions "
                           "WHERE doc_no NOT LIKE doc_base || '-%'") == 0
    assert _oracle(seeded, "SELECT COUNT(DISTINCT doc_base) FROM sales_transactions") == 5
    assert _oracle(seeded, "SELECT COUNT(*) FROM purchase_transactions "
                           "WHERE doc_no <> doc_base") == 0


# ── /trade-dashboard (models.get_trade_dashboard) ────────────────────────────

def test_trade_dashboard_counts_invoices(seeded):
    d = models.get_trade_dashboard(MONTH_FROM, MONTH_TO, conn=seeded)
    # SR is excluded here today, and still is: IV49601..IV49604 = 4 invoices
    # (8 lines).
    assert d['sales']['doc_count'] == 4
    assert len(d['top_customers']) == 2
    by_cust = {r['customer']: r['doc_count'] for r in d['top_customers']}
    assert by_cust == {X_NAME: 3, Y_NAME: 1}      # X: 01, 02, 04 (6 lines)


def test_trade_dashboard_purchase_counts_unchanged(seeded):
    """Purchase side is the document already: 2 docs (3 lines), and the
    supplier table counts the same 2."""
    d = models.get_trade_dashboard(MONTH_FROM, MONTH_TO, conn=seeded)
    assert d['purchases']['doc_count'] == 2
    assert _oracle(seeded, "SELECT COUNT(DISTINCT doc_base) FROM purchase_transactions") == 2
    assert len(d['top_suppliers']) == 1
    assert d['top_suppliers'][0]['doc_count'] == 2


def test_trade_dashboard_page_shows_invoice_counts(admin):
    html = admin.get(f'/trade-dashboard?date_from={MONTH_FROM}&date_to={MONTH_TO}'
                     ).get_data(as_text=True)
    # The sales and purchase cards' "N เอกสาร" lines, in page order.
    card_counts = re.findall(r'<div class="mt-2 text-subtle small">(\d+) เอกสาร &middot;', html)
    assert card_counts == ['4', '2']
    total = re.search(r'เอกสารทั้งหมด</div>\s*<div class="stat-card-value"[^>]*>\s*(\d+)\s*</div>',
                      html)
    assert total and total.group(1) == '6'           # 4 sales invoices + 2 purchase docs
    assert re.search(r'ขาย (\d+) &middot; ซื้อ (\d+)', html).groups() == ('4', '2')
    cust_cell = re.search(re.escape(X_NAME) + r'</a>\s*</td>\s*<td class="text-end font-mono '
                          r'text-subtle">(\d+)</td>', html)
    assert cust_cell and cust_cell.group(1) == '3'
