"""#646 — สินค้าที่ซื้อบ่อย cards net the credit notes the header already nets.

The page disagreed with itself. `get_customer_summary_by_code`'s header nets
returns on both figures (ยอดซื้อรวม since #494, จำนวนชิ้น since #627) while
`_customer_product_cards` summed raw `qty`/`net` over a population that DROPS
an SR line instead of subtracting it, so a returned product kept its gross
quantity and money. Measured on the 2026-09-22 prod snapshot: 56 customers,
275 credit-note lines, worst gap 194 ชิ้น (47ป001) and ฿21,840 (56ช001).

Put's ruling, 2026-09-23 (option A): net the card's qty and money, keep
`times_bought` and ซื้อล่าสุด on the purchase population (a returned document
IS still an engagement — the 2026-09-17 ruling this must not disturb), and
carry the returned amounts through so the card can say so out loud. A product
the customer only ever returned gets no card: it was never something they buy.

What that means for what a user sees. `total_qty` is not rendered anywhere and
`total_net` only drives the ยอด sort button, so netting changes WHICH products
reach the top-20 union and in what order, not a printed number. The visible
half is the badge, which is why the render test below is not optional.

Seam 1: the `product_cards` data contract — prior art test_493_slice2_product_card.py.
Seam 2: the predicate partition in price_lookup.
Seam 3: HTTP render of the badge.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re

import pytest

SENDAI_BRAND_ID = 3

TEST_CODE = 'TEST6460'
TEST_NAME = 'ลูกค้าทดสอบ 646'

_pid_counter = [646200]


def _mk_product(conn, name='สินค้าทดสอบ 646', unit_type='ตัว', base=100.0, cost=60.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active) VALUES (?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _clear(conn):
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ? OR customer = ?",
                 (TEST_CODE, TEST_NAME))
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, qty, unit_price, net,
          vat_type=1, unit='ตัว'):
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, f"{doc_base}-{suffix}", doc_base, pid, TEST_NAME, TEST_CODE,
         qty, unit, unit_price, vat_type, net, net),
    )
    conn.commit()


@pytest.fixture
def cust(tmp_db_conn):
    tmp_db_conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name", (TEST_CODE, TEST_NAME))
    tmp_db_conn.commit()
    _clear(tmp_db_conn)
    pid = _mk_product(tmp_db_conn)
    yield tmp_db_conn, pid
    _clear(tmp_db_conn)


def _cards(code=TEST_CODE):
    import models
    return models.get_customer_summary_by_code(code)['product_cards']


def _card(pid, unit='ตัว'):
    hits = [c for c in _cards() if c['product_id'] == pid and c['unit'] == unit]
    assert len(hits) == 1, f"expected exactly one card for pid {pid} / {unit}, got {len(hits)}"
    return hits[0]


def _sold_and_returned(conn, pid, *, sold_qty=100, sold_net=1000.0,
                       returned_qty=40, returned_net=400.0):
    _line(conn, doc_base='IV64601', suffix=1, pid=pid, date_iso='2026-01-05',
          qty=sold_qty, unit_price=10, net=sold_net)
    _line(conn, doc_base='SR64601', suffix=1, pid=pid, date_iso='2026-02-05',
          qty=returned_qty, unit_price=10, net=returned_net)


# ── Seam 1: the card's own figures ──────────────────────────────────────────

def test_card_qty_and_money_are_net_of_returns(cust):
    conn, pid = cust
    _sold_and_returned(conn, pid)
    card = _card(pid)
    assert card['total_qty'] == 60       # 100 sold - 40 returned
    assert card['total_net'] == 600.0    # 1000 - 400


def test_times_bought_still_counts_the_purchase_population_only(cust):
    """Put, 2026-09-17: a credit note is not a purchase, but the invoice it
    reverses still is. Netting the money must not move this count either way."""
    conn, pid = cust
    _sold_and_returned(conn, pid)
    assert _card(pid)['times_bought'] == 1


def test_card_carries_the_returned_amounts(cust):
    conn, pid = cust
    _sold_and_returned(conn, pid)
    card = _card(pid)
    assert card['returned_qty'] == 40
    assert card['returned_net'] == 400.0


def test_a_card_with_no_return_reports_zero_not_none(cust):
    """The control for the badge: every card carries the keys, and a clean one
    is falsy, so the template's `{% if %}` cannot fire on every row."""
    conn, pid = cust
    _line(conn, doc_base='IV64602', suffix=1, pid=pid, date_iso='2026-01-05',
          qty=100, unit_price=10, net=1000.0)
    card = _card(pid)
    assert card['returned_qty'] == 0
    assert card['returned_net'] == 0
    assert card['total_qty'] == 100


def test_a_product_only_ever_returned_gets_no_card(cust):
    """A return with no invoice behind it is not something the customer buys.
    82 of prod's 272 mapped credit-note lines are this shape (measured
    2026-09-23): the customer holds no non-SR line for that product at all."""
    conn, pid = cust
    other = _mk_product(conn, name='สินค้าที่คืนอย่างเดียว')
    _line(conn, doc_base='IV64603', suffix=1, pid=pid, date_iso='2026-01-05',
          qty=10, unit_price=10, net=100.0)
    _line(conn, doc_base='SR64603', suffix=1, pid=other, date_iso='2026-02-05',
          qty=5, unit_price=10, net=50.0)
    pids = [c['product_id'] for c in _cards()]
    assert pid in pids                      # control: the fixture did reach the cards
    assert other not in pids


def test_a_fully_returned_product_falls_below_a_smaller_surviving_one(cust):
    """The behavioural point. `total_net` drives the ยอด sort and decides which
    products reach the top-20 union, so a gross figure ranked a product the
    customer sent back above one they kept."""
    conn, big = cust
    small = _mk_product(conn, name='สินค้าที่เก็บไว้')
    _line(conn, doc_base='IV64604', suffix=1, pid=big, date_iso='2026-01-05',
          qty=100, unit_price=100, net=10000.0)
    _line(conn, doc_base='SR64604', suffix=1, pid=big, date_iso='2026-02-05',
          qty=100, unit_price=100, net=10000.0)
    _line(conn, doc_base='IV64605', suffix=1, pid=small, date_iso='2026-01-06',
          qty=1, unit_price=500, net=500.0)
    by_money = sorted(_cards(), key=lambda c: -c['total_net'])
    assert [c['product_id'] for c in by_money][:2] == [small, big]
    assert _card(big)['total_net'] == 0.0


def test_the_header_and_the_cards_now_agree_on_this_customer(cust):
    """The issue's actual complaint, asserted end to end: the cards may no
    longer out-total the header they sit under."""
    import models
    conn, pid = cust
    _sold_and_returned(conn, pid)
    data = models.get_customer_summary_by_code(TEST_CODE)
    assert data['summary']['total_qty'] == 60
    assert sum(c['total_qty'] for c in data['product_cards']) == 60
    assert sum(c['total_net'] for c in data['product_cards']) == pytest.approx(
        data['summary']['total_net'])


# ── Seam 2: the predicate partition ─────────────────────────────────────────

def test_returned_and_purchase_populations_are_disjoint_and_cover(tmp_db_conn):
    """Two named predicates over one hygiene rule, per the ar_writeoffs
    lesson: never an opt-out parameter. Asserted against real rows so a
    predicate that returned nothing cannot pass vacuously."""
    import price_lookup
    q = lambda clause: tmp_db_conn.execute(
        f"SELECT COUNT(*) FROM sales_transactions s WHERE {clause}").fetchone()[0]
    bought = q(price_lookup.purchase_population_filter('s'))
    returned = q(price_lookup.returned_lines_filter('s'))
    both = q(f"({price_lookup.purchase_population_filter('s')}) "
             f"AND ({price_lookup.returned_lines_filter('s')})")
    either = q(f"({price_lookup.purchase_population_filter('s')}) "
               f"OR ({price_lookup.returned_lines_filter('s')})")
    assert bought > 0, "control: the purchase population must not be empty here"
    assert returned > 0, "control: the returns population must not be empty here"
    assert both == 0
    assert either == bought + returned


def test_returned_lines_filter_keeps_only_credit_notes(cust):
    import price_lookup
    conn, pid = cust
    _sold_and_returned(conn, pid)
    rows = conn.execute(
        "SELECT doc_base FROM sales_transactions s "
        f"WHERE s.customer_code = ? AND {price_lookup.returned_lines_filter('s')}",
        (TEST_CODE,)).fetchall()
    assert [r[0] for r in rows] == ['SR64601']


# ── Seam 3: the badge actually renders ──────────────────────────────────────

def _card_row(html, pid):
    """The ONE <tr> for this product, extracted before asserting, so a match
    inside the page's sort script cannot satisfy the assertion."""
    body = html.split('id="productCardsTable"', 1)[1]
    rows = re.findall(r'<tr\b[^>]*data-times-bought=.*?</tr>', body, re.S)
    hits = [r for r in rows if f'/products/{pid}' in r]
    assert len(hits) == 1, f"expected one rendered card row for pid {pid}, got {len(hits)}"
    return hits[0]


def test_returned_card_renders_the_badge_and_a_clean_one_does_not(cust, tmp_db):
    from app import app as a
    conn, returned_pid = cust
    # ⚠ No 'คืน' in this name. A product called สินค้าที่ไม่ได้คืน satisfies the
    # negative assertion below through its own <a> text, which is the Thai
    # substring trap: the first version of this test failed for that reason.
    clean_pid = _mk_product(conn, name='สินค้าปกติ')
    _sold_and_returned(conn, returned_pid)
    _line(conn, doc_base='IV64606', suffix=1, pid=clean_pid, date_iso='2026-01-05',
          qty=7, unit_price=10, net=70.0)

    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'admin'
        s['role'] = 'admin'
    resp = c.get(f'/customer/code/{TEST_CODE}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # The badge marker, not a bare 'คืน': product names carry that word too.
    assert '↩ คืน 40' in _card_row(html, returned_pid)
    assert '↩ คืน' not in _card_row(html, clean_pid)
    # Control for the negative: the clean row DID render, with its own figures.
    assert '7 ครั้ง' not in _card_row(html, clean_pid)
    assert '1 ครั้ง' in _card_row(html, clean_pid)
