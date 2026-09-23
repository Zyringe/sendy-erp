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
  SR49601   2031-03-06  X         P1 2 ตัว ฿320    +  P1 1 ตัว ฿0 (credit note, stored
                                  positive like prod; its ฿0 line is a return, not แถม)
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
    ('SR49601', 1, '2031-03-06', 'X', P1, 2, 'ตัว', 160, None, 320),
    ('SR49601', 2, '2031-03-06', 'X', P1, 1, 'ตัว', 0, None, 0),
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


# ── /accounting (models.get_accounting_summary) ──────────────────────────────

def test_accounting_period_counts_invoices_below_lines(seeded):
    s = models.get_accounting_summary(MONTH_FROM, MONTH_TO)
    # Revenue population excludes SR (the fixture has no HS docs, so #514's
    # HS-counted change doesn't move this number): 4 invoices over 8 lines.
    # The two numbers used to be identical on every period.
    assert s['line_count'] == 8
    assert s['doc_count'] == 4
    assert s['doc_count'] < s['line_count']


def test_accounting_page_period_line(admin):
    html = admin.get(f'/accounting?date_from={MONTH_FROM}&date_to={MONTH_TO}'
                     ).get_data(as_text=True)
    m = re.findall(r'(\d+) เอกสาร &nbsp;·&nbsp; (\d+) รายการ', html)
    assert m == [('4', '8')]


# ── /products/<id>/pricing (models.get_product_pricing) ──────────────────────
# Population today, kept: qty > 0 AND unit_price > 0. That drops IV49601's
# unpriced freebie and SR49601's ฿0 line, and keeps IV49604's PRICED freebie
# (unit_price 160, 100% off), so IV49604 carries two lines at ฿160. It also
# keeps SR49601's priced line, because prod stores credit notes with positive
# qty; whether pricing should drop credit notes is out of #496's scope.

def test_pricing_counts_invoices(seeded):
    pr = models.get_product_pricing(P1)
    assert pr['total_invoices'] == 5                  # 01, 02, 03, 04, SR (7 lines)

    assert len(pr['list_prices']) == 2
    by_price = {lp['unit_price']: lp for lp in pr['list_prices']}
    assert by_price[160]['invoice_count'] == 5        # 01, 02, 03, 04, SR (6 lines)
    assert by_price[1500]['invoice_count'] == 1       # 03

    assert len(by_price[160]['customers']) == 2
    under_160 = {c['customer']: c['invoice_count'] for c in by_price[160]['customers']}
    assert under_160 == {X_NAME: 4, Y_NAME: 1}        # X: 01, 02, 04, SR (5 lines)

    assert len(pr['effective_per_customer']) == 2
    eff = {e['customer']: e['invoice_count'] for e in pr['effective_per_customer']}
    assert eff == {X_NAME: 4, Y_NAME: 1}              # Y: 03 on two lines


def test_pricing_page_shows_invoice_counts(admin):
    html = admin.get(f'/products/{P1}/pricing').get_data(as_text=True)
    total = re.findall(r'บิลทั้งหมด</div>\s*<div class="stat-card-value">(\d+)</div>', html)
    assert total == ['5']
    # ราคาตั้ง table: the ฿160.00 row's จำนวนบิล cell (VAT badge cell in between).
    row160 = re.findall(r'<td class="fw-600">฿160\.00</td>\s*<td>.*?</td>\s*'
                        r'<td class="text-end">(\d+)</td>', html, re.S)
    assert row160 == ['5']
    # Per-customer sub-table under ฿160 (two customers, so it renders).
    sub_x = re.findall(r'<td>' + re.escape(X_NAME) + r'</td>\s*<td class="text-subtle">'
                       + X_CODE + r'</td>\s*<td class="text-end">(\d+)</td>', html)
    assert sub_x == ['4']
    # ราคาเฉลี่ย table: the บิล cell after the price cell.
    eff_x = re.findall(r'<td class="fw-500">' + re.escape(X_NAME) + r'</td>\s*'
                       r'<td class="text-subtle">' + X_CODE + r'</td>\s*'
                       r'<td class="text-end">.*?</td>\s*<td class="text-end">(\d+)</td>',
                       html, re.S)
    assert eff_x == ['4']


# ── call card (call_card.get_card, per-product doc_count; not rendered today) ─

def test_call_card_per_product_count_is_invoices(seeded):
    import call_card
    x = call_card.get_card(seeded, X_CODE)
    assert len(x['products']) == 2
    by_key = {(p['product_id'], p['unit']): p['doc_count'] for p in x['products']}
    # X's P1 ตัว lines: 01-1, 01-2 (freebie), 02-1, 04-1, 04-2 (freebie), SR-1,
    # SR-2 = 7 lines on 4 documents. This population keeps the credit note.
    assert by_key == {(P1, 'ตัว'): 4, (P2, 'ตัว'): 1}

    y = call_card.get_card(seeded, Y_CODE)
    assert len(y['products']) == 2
    # IV49603 carries P1 in two units: one invoice in EACH unit's group.
    assert {(p['product_id'], p['unit']): p['doc_count'] for p in y['products']} == \
        {(P1, 'แผง'): 1, (P1, 'ตัว'): 1}


# ── /products/<id>/trade (models.get_product_trade_summary) ──────────────────
# Population today, kept: every P1 line in the date range, credit note
# included. P1 has 9 lines on 5 documents: IV49601-04 + SR49601.

def test_product_trade_counts_invoices(seeded):
    d = models.get_product_trade_summary(P1)
    assert d['summary']['doc_count'] == 5

    assert len(d['top_customers']) == 2
    assert {r['customer']: r['doc_count'] for r in d['top_customers']} == \
        {X_NAME: 4, Y_NAME: 1}                        # X: 01, 02, 04, SR (7 lines)

    assert len(d['monthly']) == 1
    assert d['monthly'][0]['doc_count'] == 5          # not rendered today, fixed anyway

    # Unit chips: ตัว sits on all 5 documents (8 lines), แผง on IV49603 only.
    assert d['units'] == [{'unit': 'ตัว', 'doc_count': 5},
                          {'unit': 'แผง', 'doc_count': 1}]
    # "ทั้งหมด" is the invoice count across units, NOT the chips' sum (6):
    # IV49603 carries P1 in both units and is one invoice.
    assert d['all_units_doc_count'] == 5 == d['summary']['doc_count']
    assert sum(u['doc_count'] for u in d['units']) == 6


def _docs_by_base(d):
    return {r['doc_base']: r for r in d['docs']}


def test_product_trade_docs_one_row_per_invoice(seeded):
    d = models.get_product_trade_summary(P1)
    assert len(d['docs']) == 5
    assert [r['doc_base'] for r in d['docs']] == \
        ['SR49601', 'IV49604', 'IV49603', 'IV49602', 'IV49601']    # newest first
    docs = _docs_by_base(d)
    # Paid 24 + unpriced ฿0 freebie 3 = one row, 27 units, 3 of them free.
    assert docs['IV49601']['units'] == [{'unit': 'ตัว', 'qty': 27, 'free_qty': 3}]
    assert docs['IV49601']['total_net'] == 3840
    # Paid 10 + PRICED freebie (160, 100% off, ฿0) 1.
    assert docs['IV49604']['units'] == [{'unit': 'ตัว', 'qty': 11, 'free_qty': 1}]
    assert docs['IV49604']['total_net'] == 1600
    # Two units on one invoice: each unit's quantity on its own, never summed.
    assert docs['IV49603']['units'] == [{'unit': 'แผง', 'qty': 2, 'free_qty': 0},
                                        {'unit': 'ตัว', 'qty': 4, 'free_qty': 0}]
    assert docs['IV49603']['total_net'] == 3640
    # Another product on the invoice (P2) is not this product's quantity.
    assert docs['IV49602']['units'] == [{'unit': 'ตัว', 'qty': 5, 'free_qty': 0}]
    # A credit note's ฿0 line is a return, never แถม (prod stores SR positive).
    # Its row reads NEGATIVE, qty and money alike (#627: net of returns).
    assert docs['SR49601']['units'] == [{'unit': 'ตัว', 'qty': -3, 'free_qty': 0}]
    assert docs['SR49601']['total_net'] == -320
    assert docs['IV49601']['customer'] == X_NAME and docs['IV49601']['date_iso'] == '2031-03-02'


def test_product_trade_unit_filter_keeps_population(seeded):
    t = models.get_product_trade_summary(P1, unit='ตัว')
    assert t['summary']['doc_count'] == 5             # 8 ตัว lines on 5 documents
    assert len(t['docs']) == 5
    # The filter narrows the lines, so IV49603 shows only its ตัว quantity.
    assert _docs_by_base(t)['IV49603']['units'] == [{'unit': 'ตัว', 'qty': 4, 'free_qty': 0}]

    p = models.get_product_trade_summary(P1, unit='แผง')
    assert p['summary']['doc_count'] == 1
    assert [r['doc_base'] for r in p['docs']] == ['IV49603']
    assert p['docs'][0]['units'] == [{'unit': 'แผง', 'qty': 2, 'free_qty': 0}]
    # Chips and "ทั้งหมด" never narrow with the active filter.
    assert t['units'] == p['units'] == models.get_product_trade_summary(P1)['units']
    assert t['all_units_doc_count'] == p['all_units_doc_count'] == 5


def test_product_trade_docs_cap_counts_invoices(seeded):
    """210 invoices, each a paid line + a ฿0 freebie line (420 lines): the
    list holds the newest 200 INVOICES. Capping lines held only 100."""
    P3 = 949603
    seeded.execute("INSERT INTO products (id, product_name, unit_type, sku_code, is_active) "
                   "VALUES (?, 'สินค้าทดสอบ496 สาม', 'ตัว', 'SKU-496-3', 1)", (P3,))
    rows = []
    for i in range(210):
        base, date = f'IV497{i:03d}', f'2030-{1 + i // 28:02d}-{1 + i % 28:02d}'
        rows.append((date, f'{base}-1', base, P3, 5, 100, 500))
        rows.append((date, f'{base}-2', base, P3, 1, 0, 0))
    seeded.executemany(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer,"
        " qty, unit, unit_price, vat_type, total, net) VALUES (?, ?, ?, ?, 'Z', ?, 'ตัว',"
        " ?, 1, ?, ?)", [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[6]) for r in rows])
    seeded.commit()
    assert _oracle(seeded, "SELECT COUNT(*) FROM sales_transactions WHERE product_id = ?", P3) == 420

    d = models.get_product_trade_summary(P3)
    assert d['summary']['doc_count'] == 210
    assert len(d['docs']) == 200
    assert len({r['doc_base'] for r in d['docs']}) == 200
    newest_200 = [r[0] for r in seeded.execute(
        "SELECT doc_base FROM sales_transactions WHERE product_id = ? GROUP BY doc_base "
        "ORDER BY MAX(date_iso) DESC, doc_base LIMIT 200", (P3,))]
    assert [r['doc_base'] for r in d['docs']] == newest_200
    assert all(r['units'] == [{'unit': 'ตัว', 'qty': 6, 'free_qty': 1}] for r in d['docs'])


def _cells(row):
    return [re.sub(r'\s+', ' ', c).strip()
            for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]


def test_product_trade_page_shows_invoices(admin):
    html = admin.get(f'/products/{P1}/trade').get_data(as_text=True)
    chips = re.findall(r'class="btn btn-outline-secondary[^"]*">([^<]+)</a>', html)
    assert chips == ['ทั้งหมด 5', 'ตัว 5', 'แผง 1']   # ทั้งหมด is not 5 + 1
    card = re.findall(r'จำนวนเอกสาร</div>\s*<div class="stat-card-value text-warning">'
                      r'(\d+)</div>', html)
    assert card == ['5']
    top_x = re.findall(re.escape(X_NAME) + r'</a>\s*</td>(?:\s*<td[^>]*>[^<]*</td>){2}\s*'
                       r'<td class="text-end font-mono text-subtle">(\d+)</td>', html)
    assert top_x == ['4']

    # รายการเอกสารล่าสุด: one row per invoice, each linked once.
    assert html.count('รายการเอกสารล่าสุด') == 1
    section = html.split('รายการเอกสารล่าสุด', 1)[1].split('</table>', 1)[0]
    assert re.search(r'\((\d+) รายการ\)', section).group(1) == '5'
    rows = re.findall(r'<tr>(.*?)</tr>', section.split('<tbody>', 1)[1], re.S)
    assert len(rows) == 5
    by_doc = {}
    for row in rows:
        links = re.findall(r'href="/sales/doc/([^"?]+)', row)    # a ?book= may follow
        assert len(links) == 1
        by_doc[links[0]] = _cells(row)
    assert sorted(by_doc) == ['IV49601', 'IV49602', 'IV49603', 'IV49604', 'SR49601']
    assert all(len(c) == 5 for c in by_doc.values())    # วันที่ เลขเอกสาร ลูกค้า จำนวน ยอด
    assert by_doc['IV49601'][3] == '27.0 ตัว (แถม 3.0)'
    assert by_doc['IV49601'][4] == '3,840.00'
    assert by_doc['IV49603'][3] == '2.0 แผง<br>4.0 ตัว'
    assert by_doc['IV49604'][3] == '11.0 ตัว (แถม 1.0)'
    assert by_doc['SR49601'][3] == '-3.0 ตัว'                 # its ฿0 line is no แถม; #627 nets it


# ── edge: a freebie-only invoice ──────────────────────────────────────────────

def test_freebie_only_invoice_counts_where_its_population_does(seeded):
    """An invoice carrying only a ฿0 freebie line of the product is still an
    invoice on every page whose population holds that line today. Pricing's
    population (unit_price > 0) never held it, and still does not."""
    seeded.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, customer,"
        " customer_code, qty, unit, unit_price, vat_type, total, net)"
        " VALUES ('2031-03-07', 'IV49605-1', 'IV49605', ?, ?, ?, 2, 'ตัว', 0, 1, 0, 0)",
        (P1, X_NAME, X_CODE))
    seeded.commit()
    assert models.get_trade_dashboard(MONTH_FROM, MONTH_TO, conn=seeded)['sales']['doc_count'] == 5
    acc = models.get_accounting_summary(MONTH_FROM, MONTH_TO)
    assert (acc['doc_count'], acc['line_count']) == (5, 9)
    t = models.get_product_trade_summary(P1)
    assert t['summary']['doc_count'] == 6
    assert len(t['docs']) == 6
    assert _docs_by_base(t)['IV49605']['units'] == [{'unit': 'ตัว', 'qty': 2, 'free_qty': 2}]
    assert models.get_product_pricing(P1)['total_invoices'] == 5    # unchanged: unit_price 0
    import call_card
    x = {(p['product_id'], p['unit']): p['doc_count']
         for p in call_card.get_card(seeded, X_CODE)['products']}
    assert x[(P1, 'ตัว')] == 5
