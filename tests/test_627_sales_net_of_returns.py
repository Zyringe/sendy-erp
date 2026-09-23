"""#627 — ยอดขาย on the trade screens is NET of returns, and /trade-dashboard
drops the documents invoiced in error.

The bug: SR (credit-note) lines are stored with POSITIVE net/qty, the same
importer convention #591 fixed on the purchase side for GR. /sales and
/products/<id>/trade summed a bare `net`, so a return was ADDED as a sale;
/trade-dashboard dropped SR entirely, so a return was ignored. Measured on
prod, 2026-01-01..09-30: /sales ฿2,900,927.44, dashboard ฿2,886,748.94, a gap
of exactly the 16 SR lines (฿14,178.50).

Put's rulings, 2026-09-22 (issue #627):
  1. The trade screens (/sales, /trade-dashboard, /products/<id>/trade, the
     call card's product ordering) SUBTRACT a return: not dropped, not added.
     A product or a month may go negative.
  2. /trade-dashboard also drops the three วรสวัสดิ์ giveaway invoices
     (ar_writeoffs.excludes_revenue = 1). /sales keeps every document,
     including them (option ก, 2026-07-30).
  /accounting, /revenue, financial health and cashflow do NOT change.

Every expected figure below is worked by hand from the seeded rows, never
recomputed the way the code does. The fixture lives in March 2031, where the
cloned dev DB holds no rows, so every total is the fixture's alone.
"""
import datetime
import os
import re
from html import unescape

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

WINDOW = ('2031-03-01', '2031-03-31')
C1, C2, C3 = 'ลูกค้าทดสอบ 627 หนึ่ง', 'ลูกค้าทดสอบ 627 สอง', 'ลูกค้าทดสอบ 627 สาม'
GIVEAWAY_DOC = 'IV62704'

# The fixture, worked by hand (every line vat_type 1 unless noted):
#
#   IV62701  2031-03-03  C1  A  qty 10  net 1,000
#                        C1  B  qty  5  net   500
#   IV62702  2031-03-04  C2  A  qty  4  net   400   (vat_type 2)
#   IV62704  2031-03-05  C1  B  qty 20  net 2,000   invoiced in error (the giveaway)
#   SR62703  2031-03-12  C1  A  qty  3  net   300   a return, stored POSITIVE
#                        C1  D  qty  2  net   200   a return of a product never
#                                                   sold in the window
#
# net of returns, every document:   1,000 + 500 + 400 + 2,000 − 300 − 200 = 3,400
# net of returns, giveaway dropped: 3,400 − 2,000                         = 1,400
# returns ADDED (the /sales bug):   1,000 + 500 + 400 + 2,000 + 300 + 200 = 4,400
# returns DROPPED (old dashboard):  1,000 + 500 + 400                     = 1,900


def _reset(conn):
    conn.execute(
        "DELETE FROM sales_transactions WHERE customer IN (?, ?, ?) "
        "OR (date_iso >= ? AND date_iso <= ?)", (C1, C2, C3) + WINDOW)
    conn.execute("DELETE FROM ar_writeoffs WHERE doc_no = ?", (GIVEAWAY_DOC,))
    conn.commit()


def _product(conn, name):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "is_active) VALUES (?, 'ตัว', 100, 60, 1)", (name,))
    return cur.lastrowid


def _line(conn, doc_base, seq, *, pid, customer, qty, net, date_iso, vat_type=1,
          code=None, name_raw=None):
    """One sales line in the real shape: doc_no '<base>-<seq>'. A credit note
    is stored exactly like the real ones: positive qty and net — and, like the
    real ones, it may print `product_name_raw` differently from the invoice it
    reverses (prod: '(ต)' vs '(P)', 'SS' vs 'SS S/D')."""
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, product_id, "
        " product_name_raw, customer, customer_code, qty, unit, unit_price, vat_type, "
        " total, net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, f'{doc_base}-{seq}', doc_base, pid, name_raw or f'สินค้า {pid}',
         customer, code, qty, 'ตัว', net / qty, vat_type, net, net))


def _seed(conn):
    _reset(conn)
    a, b, d = (_product(conn, f'สินค้าทดสอบ 627 {x}') for x in 'ABD')
    _line(conn, 'IV62701', 1, pid=a, customer=C1, qty=10, net=1000, date_iso='2031-03-03')
    _line(conn, 'IV62701', 2, pid=b, customer=C1, qty=5, net=500, date_iso='2031-03-03')
    _line(conn, 'IV62702', 1, pid=a, customer=C2, qty=4, net=400, date_iso='2031-03-04',
          vat_type=2)
    _line(conn, GIVEAWAY_DOC, 1, pid=b, customer=C1, qty=20, net=2000, date_iso='2031-03-05')
    # The credit note prints A's name differently from IV62701's line, the
    # shape that made the top-10 list show it as its own negative row (#627
    # review): grouping mapped rows on product_id alone is what nets it.
    _line(conn, 'SR62703', 1, pid=a, customer=C1, qty=3, net=300, date_iso='2031-03-12',
          name_raw='สินค้า A (ต) ตามใบลดหนี้')
    _line(conn, 'SR62703', 2, pid=d, customer=C1, qty=2, net=200, date_iso='2031-03-12')
    conn.execute(
        "INSERT INTO ar_writeoffs (doc_no, customer_name, amount, type, writeoff_date, "
        " reason, excludes_revenue) VALUES (?, ?, 2000, 'expense', '2031-03-31', "
        " 'test 627: invoiced in error', 1)", (GIVEAWAY_DOC, C1))
    conn.commit()
    # CONTROL: all six lines landed in the window and nothing else is there.
    n = conn.execute("SELECT COUNT(*) FROM sales_transactions "
                     "WHERE date_iso >= ? AND date_iso <= ?", WINDOW).fetchone()[0]
    assert n == 6, f'fixture window must hold exactly the 6 seeded lines, found {n}'
    return {'A': a, 'B': b, 'D': d}


# ── /sales: every document, returns subtracted (ruling 1, option ก kept) ────

def test_sales_cards_subtract_the_return_and_keep_the_giveaway(tmp_db_conn):
    _seed(tmp_db_conn)
    import models
    by_vat = {r['vat_type']: dict(r) for r in models.get_sales_summary(*WINDOW)}

    # CONTROL: the cards still count every LINE listed below them, SR included.
    assert by_vat[1]['txn_count'] == 5 and by_vat[2]['txn_count'] == 1
    assert by_vat[1]['total_net'] == pytest.approx(3000.0), (
        '1,000 + 500 + 2,000 − 300 − 200: the return is SUBTRACTED and the '
        f"giveaway stays (option ก), got {by_vat[1]['total_net']}")
    assert by_vat[2]['total_net'] == pytest.approx(400.0)
    assert by_vat[1]['total_qty'] == pytest.approx(10 + 5 + 20 - 3 - 2)


def test_sales_doc_search_on_a_credit_note_reads_negative(tmp_db_conn):
    """The doc_no search mode takes the same path: one SR document on its own
    is money going back, not a sale."""
    _seed(tmp_db_conn)
    import models
    rows = models.get_sales_summary(doc_no='SR62703')
    assert len(rows) == 1 and rows[0]['txn_count'] == 2
    assert rows[0]['total_net'] == pytest.approx(-500.0)


# ── /trade-dashboard: returns subtracted AND the giveaway dropped ────────────

def test_dashboard_sales_card_nets_the_return_and_drops_the_giveaway(tmp_db_conn):
    _seed(tmp_db_conn)
    import models
    d = models.get_trade_dashboard(*WINDOW)

    # CONTROL: nothing purchased in the window, so the margin card is sales alone.
    assert d['purchases']['total_net'] == 0
    assert d['sales']['doc_count'] == 2, (
        'invoices only: IV62701 + IV62702. The credit note is not an invoice '
        'and the giveaway is dropped')
    assert d['sales']['total_net'] == pytest.approx(1400.0), (
        f"1,000 + 500 + 400 − 300 − 200, got {d['sales']['total_net']}")
    assert d['sales']['total_qty'] == pytest.approx(10 + 5 + 4 - 3 - 2)
    assert d['gross_profit'] == pytest.approx(1400.0)


def test_dashboard_giveaway_clause_is_what_drops_the_2000(tmp_db_conn):
    """The giveaway clause must have something on its far side: unflag the
    document and the same rows must come back in by exactly its net."""
    _seed(tmp_db_conn)
    import models
    flagged = models.get_trade_dashboard(*WINDOW)
    tmp_db_conn.execute("UPDATE ar_writeoffs SET excludes_revenue = 0 WHERE doc_no = ?",
                        (GIVEAWAY_DOC,))
    tmp_db_conn.commit()
    unflagged = models.get_trade_dashboard(*WINDOW)
    assert unflagged['sales']['total_net'] - flagged['sales']['total_net'] == pytest.approx(2000.0)
    assert unflagged['sales']['doc_count'] - flagged['sales']['doc_count'] == 1


def test_dashboard_weekly_chart_can_go_negative(tmp_db_conn):
    _seed(tmp_db_conn)
    import models
    trend = {w['week']: w['sales'] for w in models.get_trade_dashboard(*WINDOW)['weekly_trend']}
    wk = lambda iso: datetime.date.fromisoformat(iso).strftime('%Y-W%W')
    assert trend == pytest.approx({
        wk('2031-03-03'): 1000 + 500 + 400,   # the giveaway (03-05) dropped
        wk('2031-03-12'): -300 - 200,         # a week holding only the return
    })


def test_dashboard_top10s_net_the_return_and_drop_the_giveaway(tmp_db_conn):
    pids = _seed(tmp_db_conn)
    import models
    d = models.get_trade_dashboard(*WINDOW)

    products = [(r['product_id'], r['total_net'], r['total_qty']) for r in d['top_products']]
    # THREE rows, not four: A's credit note prints another raw name and must
    # still net against A rather than form its own negative row (#627 review).
    assert len(products) == 3
    assert products == [
        (pids['A'], pytest.approx(1000 + 400 - 300), pytest.approx(10 + 4 - 3)),
        (pids['B'], pytest.approx(500), pytest.approx(5)),   # giveaway's 2,000 dropped
        (pids['D'], pytest.approx(-200), pytest.approx(-2)),
    ]

    customers = {r['customer']: (r['total_net'], r['doc_count']) for r in d['top_customers']}
    assert customers == {
        C1: (pytest.approx(1000 + 500 - 300 - 200), 1),
        C2: (pytest.approx(400), 1),
    }


# ── /products/<id>/trade: returns subtracted, every document ─────────────────

def test_product_trade_page_subtracts_the_return(tmp_db_conn):
    pids = _seed(tmp_db_conn)
    import models
    data = models.get_product_trade_summary(pids['A'], *WINDOW)

    assert data['summary']['doc_count'] == 3          # CONTROL: IV62701, IV62702, SR62703
    assert data['summary']['total_net'] == pytest.approx(1100.0)
    assert data['summary']['total_qty'] == pytest.approx(11.0)
    assert {m['month']: m['total_net'] for m in data['monthly']} == pytest.approx(
        {'2031-03': 1100.0})
    assert {c['customer']: c['total_net'] for c in data['top_customers']} == pytest.approx(
        {C1: 700.0, C2: 400.0})

    by_doc = {d['doc_base']: d['total_net'] for d in data['docs']}
    assert by_doc == pytest.approx({'IV62701': 1000.0, 'IV62702': 400.0, 'SR62703': -300.0})
    assert sum(by_doc.values()) == pytest.approx(data['summary']['total_net']), (
        'the document list must sum to the same figure as the header')
    sr_units = next(d['units'] for d in data['docs'] if d['doc_base'] == 'SR62703')
    assert sr_units == [{'unit': 'ตัว', 'qty': -3.0, 'free_qty': 0}]


def test_product_trade_page_can_go_negative(tmp_db_conn):
    pids = _seed(tmp_db_conn)
    import models
    data = models.get_product_trade_summary(pids['D'], *WINDOW)
    assert data['summary']['doc_count'] == 1
    assert data['summary']['total_net'] == pytest.approx(-200.0)
    assert data['summary']['total_qty'] == pytest.approx(-2.0)


def test_product_trade_page_keeps_the_giveaway(tmp_db_conn):
    """Ruling 2 names /trade-dashboard only; this page stays on every document."""
    pids = _seed(tmp_db_conn)
    import models
    data = models.get_product_trade_summary(pids['B'], *WINDOW)
    assert data['summary']['doc_count'] == 2
    assert data['summary']['total_net'] == pytest.approx(500 + 2000)


# ── the call card orders products net of returns ─────────────────────────────
#
# C3 bought E for 900 and A for 1,000, then returned 300 of A. Returns added,
# A (1,300) ranks above E (900); net of returns, E (900) ranks above A (700).

C3_CODE = 'T627C'


def _seed_c3(conn):
    _reset(conn)
    conn.execute("INSERT INTO customers (code, name) VALUES (?, ?) "
                 "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (C3_CODE, C3))
    a, e = _product(conn, 'สินค้าทดสอบ 627 A'), _product(conn, 'สินค้าทดสอบ 627 E')
    for doc, pid, qty, net, day, raw in (
            ('IV62711', a, 10, 1000, '03', None), ('IV62712', e, 9, 900, '04', None),
            # again a return printing its own name for the same product
            ('SR62713', a, 3, 300, '12', 'สินค้า A (ต) ตามใบลดหนี้')):
        _line(conn, doc, 1, pid=pid, customer=C3, qty=qty, net=net,
              date_iso=f'2031-03-{day}', code=C3_CODE, name_raw=raw)
    conn.commit()
    return a, e


def test_call_card_product_list_is_ordered_net_of_returns(tmp_db_conn):
    a, e = _seed_c3(tmp_db_conn)
    import call_card
    products = call_card._assemble_products(tmp_db_conn, [C3], None)
    assert len(products) == 2
    assert [p['product_id'] for p in products] == [e, a]
    qty = {p['product_id']: p['total_qty'] for p in products}
    assert qty == pytest.approx({e: 9.0, a: 7.0}), 'ซื้อรวม nets the returned 3'


def test_call_card_top_product_name_is_net_of_returns(tmp_db_conn):
    a, e = _seed_c3(tmp_db_conn)
    import models
    top = models.get_customer_summary(C3)['top_products']
    # TWO rows: the credit note's own raw name must not split A (#627 review).
    assert len(top) == 2
    assert top[0]['product_id'] == e, 'แบรนด์เด่น reads top_products[0]'
    assert top[1]['total_net'] == pytest.approx(700.0)


def test_customer_page_pieces_are_net_of_returns(tmp_db_conn):
    """The customer page's ยอดซื้อรวม has been net of returns since #494; its
    จำนวนชิ้น must follow the same rule, or one page disagrees with itself
    (team lead's ruling on #627). 10 + 9 − 3 = 16, never 22."""
    _seed_c3(tmp_db_conn)
    import models
    for summary in (models.get_customer_summary(C3)['summary'],
                    models.get_customer_summary_by_code(C3_CODE)['summary']):
        assert summary['doc_count'] == 3                 # CONTROL: the credit note is there
        assert summary['total_net'] == pytest.approx(1000 + 900 - 300)
        assert summary['total_qty'] == pytest.approx(10 + 9 - 3)


# ── the rendered pages say they are net of returns ───────────────────────────

NET_OF_RETURNS = 'หักรับคืนแล้ว'


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    return c


def _card_class(html, label):
    """The class attribute of the stat-card value that follows `label`."""
    m = re.search(re.escape(label) + r'.{0,400}?class="stat-card-value ([^"]*)"',
                  html, re.S)
    assert m, f'no stat card labelled {label!r}'
    return m.group(1)


def _card_value(html, label):
    """The stat-card value that follows `label`, as a float."""
    m = re.search(re.escape(label) + r'.{0,400}?stat-card-value[^>]*>\s*฿?([-\d,\.]+)',
                  html, re.S)
    assert m, f'no stat card labelled {label!r}'
    return float(m.group(1).replace(',', ''))


def test_sales_page_renders_the_netted_card_and_says_so(tmp_db_conn):
    _seed(tmp_db_conn)
    html = unescape(_client().get(
        '/sales?date_from={}&date_to={}'.format(*WINDOW)).get_data(as_text=True))
    assert _card_value(html, 'Type 1') == pytest.approx(3000.0)
    assert _card_value(html, 'Type 2') == pytest.approx(400.0)   # CONTROL: the parse works
    assert NET_OF_RETURNS in html


def test_dashboard_renders_the_netted_card_and_says_so(tmp_db_conn):
    _seed(tmp_db_conn)
    html = unescape(_client().get(
        '/trade-dashboard?date_from={}&date_to={}'.format(*WINDOW)).get_data(as_text=True))
    assert _card_value(html, 'ยอดขายรวม') == pytest.approx(1400.0)
    assert NET_OF_RETURNS in html
    # CONTROL for the negative-colour test below: a positive total stays green.
    assert _card_class(html, 'ยอดขายรวม') == 'text-success'


def test_dashboard_card_turns_red_when_the_period_is_negative(tmp_db_conn):
    """A window holding only the credit note reads −฿500. Negative is not
    success, so it must not render in the success colour (#627 review)."""
    _seed(tmp_db_conn)
    html = unescape(_client().get(
        '/trade-dashboard?date_from=2031-03-10&date_to=2031-03-31').get_data(as_text=True))
    assert _card_value(html, 'ยอดขายรวม') == pytest.approx(-500.0)
    assert _card_class(html, 'ยอดขายรวม') == 'text-danger'


def test_product_trade_page_renders_negative_and_says_so(tmp_db_conn):
    pids = _seed(tmp_db_conn)
    html = unescape(_client().get('/products/{}/trade?date_from={}&date_to={}'.format(
        pids['D'], *WINDOW)).get_data(as_text=True))
    assert _card_value(html, 'ยอดขายรวม') == pytest.approx(-200.0)
    assert _card_class(html, 'ยอดขายรวม') == 'text-danger'
    assert NET_OF_RETURNS in html


def test_product_trade_page_keeps_a_positive_total_green(tmp_db_conn):
    """CONTROL for the colour above: the class really does follow the sign."""
    pids = _seed(tmp_db_conn)
    html = unescape(_client().get('/products/{}/trade?date_from={}&date_to={}'.format(
        pids['A'], *WINDOW)).get_data(as_text=True))
    assert _card_value(html, 'ยอดขายรวม') == pytest.approx(1100.0)
    assert _card_class(html, 'ยอดขายรวม') == 'text-success'


# ── what must NOT change: the revenue question keeps its own rule ────────────

def test_revenue_surfaces_still_drop_returns_instead_of_subtracting(tmp_db_conn):
    """/accounting, /revenue, cashflow's revenue series and financial health
    answer the REVENUE question (GL 41-01): returns left out, not subtracted,
    and the giveaway out. #627 must not move them: 1,000 + 500 + 400 = 1,900,
    never the trade screens' 1,400."""
    _seed(tmp_db_conn)
    import cashflow
    import revenue
    import models.accounting as accounting
    import models.financial_health as financial_health

    acc = accounting.get_accounting_summary(*WINDOW)['sales_net']
    rev = revenue.revenue_summary(date_from=WINDOW[0], date_to=WINDOW[1])['total_revenue']
    cf = cashflow.revenue_by_month(date_from=WINDOW[0], date_to=WINDOW[1])
    fin = financial_health.get_trailing_months(
        n=1, as_of_date=datetime.date(2031, 4, 15))

    assert len(cf) == 1 and len(fin) == 1
    assert acc == pytest.approx(1900.0)
    assert rev == pytest.approx(1900.0)
    assert cf[0]['revenue'] == pytest.approx(1900.0)
    assert fin[0]['revenue'] == pytest.approx(1900.0)


# ── the real book: every screen ties to a hand-written oracle ────────────────

REAL = ('2026-01-01', '2026-09-30')
REAL_GIVEAWAY = ('IV6900401', 'IV6900402', 'IV6900403')


def _oracle(conn, *, drop_giveaway):
    """Jan-Sep 2026 from raw rows. The sign rule is typed out here and the
    giveaway listed literally, so neither the helper nor ar_writeoffs can make
    this agree by construction."""
    skip = (" AND doc_base NOT IN ({})".format(','.join("'%s'" % d for d in REAL_GIVEAWAY))
            if drop_giveaway else '')
    return conn.execute(f"""
        SELECT ROUND(SUM(CASE WHEN substr(doc_base, 1, 2) = 'SR' THEN -net ELSE net END), 2)
          FROM sales_transactions WHERE date_iso >= ? AND date_iso <= ?{skip}
    """, REAL).fetchone()[0]


def test_real_book_ties_to_the_oracle(tmp_db_conn):
    conn = tmp_db_conn
    if not conn.execute("SELECT COUNT(*) FROM sales_transactions WHERE doc_base IN (?,?,?)",
                        REAL_GIVEAWAY).fetchone()[0]:
        pytest.skip('the real giveaway documents are not in this DB')
    sr = conn.execute("SELECT COUNT(*) FROM sales_transactions WHERE date_iso >= ? "
                      "AND date_iso <= ? AND doc_base LIKE 'SR%'", REAL).fetchone()[0]
    assert sr > 0, 'CONTROL: the window must hold credit notes, or this ties nothing'

    import models
    sales = round(sum(r['total_net'] for r in models.get_sales_summary(*REAL)), 2)
    dash = round(models.get_trade_dashboard(*REAL)['sales']['total_net'], 2)
    assert sales == _oracle(conn, drop_giveaway=False)
    assert dash == _oracle(conn, drop_giveaway=True)
