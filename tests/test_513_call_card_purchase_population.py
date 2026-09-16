"""#513 — the call surfaces read a customer's PURCHASES, not its raw rows.

#493 taught the customer page that a credit note (SR) is not a purchase. The
call card and the /call worklist kept reading `MAX(date_iso)` and
`COUNT(DISTINCT doc_base)` over every row the customer has, so a return read
as a recent purchase:

  - /call's ซื้อล่าสุด column, and the เงียบ badge computed from it. That badge
    is the whole point of the worklist — it surfaces customers who stopped
    buying — and a return hid exactly the customer it exists to find.
  - the card header's ซื้อล่าสุด and จำนวนครั้งซื้อ.

Measured on the PROD snapshot 2026-09-14 (max sale 2026-09-12): ซื้อล่าสุด
changes on 15 of 280 /call rows, 2 rows GAIN the เงียบ badge, and จำนวนครั้งซื้อ
drops on 86 of 280 cards (163 documents summed across cards).

Every fixture here FORCES its own state: `tmp_db` clones the live dev DB with
its data, so each test deletes its key's rows first and inserts exactly what it
asserts.
"""
import datetime as dt
import os
import re

os.environ.setdefault('SKIP_DB_INIT', '1')

import call_card as cc

CODE = 'ZZ513'
NAME = 'ร้านทดสอบห้าหนึ่งสาม'

TODAY = dt.date.today()
BUY_1 = (TODAY - dt.timedelta(days=400)).isoformat()   # the real last purchase
BUY_2 = (TODAY - dt.timedelta(days=430)).isoformat()   # an older purchase
RETURN = (TODAY - dt.timedelta(days=5)).isoformat()    # a credit note, days ago


def _app():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    return flask_app


def _client():
    c = _app().test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 4
        sess['username'] = 'staffer'
        sess['role'] = 'staff'
    return c


def _line(conn, doc_base, seq, date_iso, net, qty=10.0):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, customer, customer_code, qty, unit, "
        " unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, f'{doc_base}-{seq}', doc_base, NAME, CODE, qty, 'ตัว',
         net / qty, 0, net, net))


def _seed(conn):
    """Two invoices (the newest 400 days old) and one credit note 5 days old.

    Returns nothing; every number the tests assert is derivable from here:
      purchases   = 2 documents, newest BUY_1
      all rows    = 3 documents, newest RETURN
    """
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                 (CODE, NAME))
    conn.execute("DELETE FROM customers WHERE code = ?", (CODE,))
    conn.execute("INSERT INTO customers (code, name, address) VALUES (?,?,?)",
                 (CODE, NAME, '1 ถนนทดสอบ กรุงเทพมหานคร'))
    _line(conn, 'IV9990001', 1, BUY_1, 1000.0)
    _line(conn, 'IV9990002', 1, BUY_2, 500.0)
    _line(conn, 'SR9990003', 1, RETURN, 300.0)
    conn.commit()

    # CONTROL on the fixture itself: the credit note is really there and really
    # is the newest row, so a filter that drops it has something to drop.
    raw = conn.execute(
        "SELECT MAX(date_iso) AS d, COUNT(DISTINCT doc_base) AS n "
        "FROM sales_transactions WHERE customer_code = ?", (CODE,)).fetchone()
    assert raw['d'] == RETURN and raw['n'] == 3, \
        'fixture never landed — the assertions below would pin nothing'


def _row(conn):
    rows = [r for r in cc.get_call_list(conn, spend_window='all', sort='name')
            if r['customer_code'] == CODE]
    assert len(rows) == 1, \
        f'CONTROL: the fixture customer is not on the worklist ({len(rows)} rows)'
    return rows[0]


# ── /call worklist ───────────────────────────────────────────────────────────

def test_call_list_last_buy_is_the_last_PURCHASE_not_the_last_row(tmp_db_conn):
    """ซื้อล่าสุด on the worklist. Before the fix this read RETURN."""
    _seed(tmp_db_conn)
    assert _row(tmp_db_conn)['last_buy'] == BUY_1


def test_call_list_quiet_badge_is_not_erased_by_a_recent_return(tmp_db_conn):
    """The defect that matters: the customer stopped buying 400 days ago, well
    past QUIET_AFTER_DAYS, and the credit note 5 days ago hid that."""
    _seed(tmp_db_conn)
    assert (TODAY - dt.date.fromisoformat(BUY_1)).days > cc.QUIET_AFTER_DAYS, \
        'CONTROL: the fixture purchase is not old enough to earn the badge'
    assert (TODAY - dt.date.fromisoformat(RETURN)).days <= cc.QUIET_AFTER_DAYS, \
        'CONTROL: the credit note is not recent enough to hide the badge'
    assert _row(tmp_db_conn)['badges']['quiet'] is True


def test_call_list_quiet_FILTER_returns_the_customer_the_return_was_hiding(tmp_db_conn):
    """The badge drives `?quiet=1`, which is how the worklist is actually
    worked. A row that only gets the badge is no use if the filter drops it."""
    _seed(tmp_db_conn)
    codes = [r['customer_code'] for r in
             cc.get_call_list(tmp_db_conn, spend_window='all', sort='name', quiet=True)]
    assert CODE in codes


def test_call_list_page_renders_the_purchase_date_and_the_badge(tmp_db_conn):
    """The rendered row, not the dict: ?q=<code> narrows the table to one row
    so each assertion is scoped to the customer under test."""
    _seed(tmp_db_conn)
    html = _client().get('/call?q=' + CODE + '&spend_window=all').get_data(as_text=True)

    assert html.count('<tr onclick') == 1, \
        'CONTROL: the search did not narrow the table to the fixture row'
    assert NAME in html, 'CONTROL: the fixture row did not render'
    assert BUY_1 in html
    assert RETURN not in html, 'the credit note date is on the page as ซื้อล่าสุด'
    assert '⏰ เงียบ' in html


# ── the call card header ─────────────────────────────────────────────────────

def _stat(html, label):
    """The rendered value of one cc-stat block, scoped to its own element —
    a bare substring test would also match the same text in the page's JS."""
    m = re.search(
        r'<div class="k">' + re.escape(label) + r'</div><div class="v"[^>]*>(.*?)</div>',
        html, re.S)
    assert m, f'CONTROL: the {label} stat block did not render'
    return m.group(1).strip()


def test_card_header_last_purchase_ignores_the_credit_note(tmp_db_conn):
    _seed(tmp_db_conn)
    html = _client().get('/call/' + CODE).get_data(as_text=True)

    assert NAME in html, 'CONTROL: the card did not render this customer'
    assert _stat(html, 'ซื้อล่าสุด') == BUY_1[:7]


def test_card_header_counts_purchases_not_documents(tmp_db_conn):
    """จำนวนครั้งซื้อ — two invoices, and a credit note that is not a purchase."""
    _seed(tmp_db_conn)
    html = _client().get('/call/' + CODE).get_data(as_text=True)

    assert NAME in html, 'CONTROL'
    assert _stat(html, 'จำนวนครั้งซื้อ') == '2 <small>ครั้ง</small>'


def test_summary_purchase_count_and_date_come_from_ONE_population(tmp_db_conn):
    """The anti-drift pin. `purchase_doc_count` and `last_purchase_date` must
    describe the same set of documents: the newest of the counted documents IS
    the ซื้อล่าสุด the card prints beside the count."""
    _seed(tmp_db_conn)
    import models
    s = models.get_customer_summary(NAME)['summary']

    assert s['doc_count'] == 3, \
        'CONTROL: doc_count is the DOCUMENT count (จำนวนเอกสาร) and keeps the SR'
    assert s['purchase_doc_count'] == 2
    assert s['last_purchase_date'] == BUY_1
    assert s['last_date'] == RETURN, \
        'CONTROL: last_date stays the raw activity date (the ช่วงเวลา end)'


# ── the mobile sales-trip list ───────────────────────────────────────────────

def test_sales_trip_last_sale_is_the_last_purchase(tmp_db_conn):
    """Same concept, same defect, one surface over: a rep planning a visit saw
    a return as the customer's last sale."""
    _seed(tmp_db_conn)
    html = _client().get('/m/sales-trip').get_data(as_text=True)

    assert NAME in html, 'CONTROL: the fixture customer is not on the trip list'
    m = re.search(r'<div class="trip-cust-name">' + re.escape(NAME) + r'</div>(.*?)</a>',
                  html, re.S)
    assert m, 'CONTROL: the fixture row did not render as a trip-cust block'
    block = m.group(1)
    assert BUY_1 in block
    assert RETURN not in block, 'the credit note date is shown as the last sale'
