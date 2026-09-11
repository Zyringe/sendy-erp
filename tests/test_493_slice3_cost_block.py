"""TDD for #493 Slice 3 — the cost block on the สินค้าที่ซื้อบ่อย card.

IN — per (product, unit) row, admin + manager ONLY:
  ทุนเฉลี่ย (WACC = the product's current cost_price) and ทุนซื้อล่าสุด (latest
  PURCHASE event in product_cost_ledger, read-only), both ex-VAT per base unit
  and converted to the row's unit; กำไรถ้าขายราคาเดิมวันนี้ and กำไรที่ราคาวันนี้,
  each with a tap-to-reveal of its own arithmetic; 🔴 ต่ำกว่าทุน and
  ⚠ ต่ำกว่าทุนซื้อล่าสุด badges on the figure they judge; "ไม่มีทุน" when the
  product has no cost.

Put's two rulings (2026-09-11, before these tests were written):
  A. The last price's badges judge the money we KEEP (net ÷ qty, ex-VAT) — on a
     แยก VAT bill the displayed price includes the 7% we remit, so a sale at
     95 + VAT against a cost of 100 IS below cost and gets 🔴.
  B. กำไรที่ราคาวันนี้ is computed at the customer's LAST order quantity, so a
     buy-N-get-M bundle's free units count once that quantity reaches N.

The gate is at the DATA layer (the customer page is open to staff): the route
asks for cost only for admin/manager, and the template gates on the same
`is_manager` flag. Both gates are pinned separately below — removing either
one alone must turn a test red.

Seam 1: the customer summary data contract (get_customer_summary_by_code).
Seam 2: HTTP render with session-injected roles.
Helpers copied from test_493_slice2_product_card.py (same shapes).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import re
from html.parser import HTMLParser
from urllib.parse import quote

import pytest

SENDAI_BRAND_ID = 3  # เซ็นได — verified against the live dev DB (own-brand)

TEST_CODE = 'TEST4933'
TEST_NAME = 'ลูกค้าทดสอบ 493 สาม'

_pid_counter = [493300]


def _mk_product(conn, name='สินค้าทดสอบสาม', unit_type='ตัว', base=100.0, cost=60.0,
                opening_cost=0.0):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "opening_cost, brand_id, is_active) VALUES (?,?,?,?,?,?,1)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, opening_cost, SENDAI_BRAND_ID),
    )
    conn.commit()
    return cur.lastrowid


def _name(conn, pid):
    return conn.execute("SELECT product_name FROM products WHERE id = ?", (pid,)).fetchone()[0]


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
          vat_type=1, unit='ตัว', total=None, discount=None):
    if total is None:
        total = net
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net, discount) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (date_iso, f"{doc_base}-{suffix}", doc_base, pid, TEST_NAME, TEST_CODE,
         qty, unit, unit_price, vat_type, total, net, discount),
    )
    conn.commit()


def _ledger(conn, pid, event_type, event_date, unit_cost, ref=None):
    conn.execute(
        "INSERT INTO product_cost_ledger "
        "(product_id, event_type, event_date, qty_change, unit_cost, stock_after, "
        " wacc_after, reference_no) VALUES (?,?,?,?,?,?,?,?)",
        (pid, event_type, event_date, 10, unit_cost, 10, unit_cost, ref),
    )
    conn.commit()


def _dozen(conn, pid):
    conn.execute("INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?,?,?)",
                 (pid, 'โหล', 12))
    conn.commit()


@pytest.fixture
def cust(tmp_db_conn):
    _mk_customer(tmp_db_conn)
    _clear_customer(tmp_db_conn)
    yield tmp_db_conn
    _clear_customer(tmp_db_conn)


def _summary(include_cost=True):
    import models
    return models.get_customer_summary_by_code(TEST_CODE, include_cost=include_cost)


def _card(data, pid, unit='ตัว'):
    return next(c for c in data['product_cards'] if c['product_id'] == pid and c['unit'] == unit)


# ── Seam 1: the data contract ───────────────────────────────────────────────

def test_cost_is_absent_from_the_data_unless_asked_for(cust):
    conn = cust
    pid = _mk_product(conn)
    _line(conn, doc_base='IV49500', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100)
    import models
    default = models.get_customer_summary_by_code(TEST_CODE)
    # CONTROL first: the row is really on the page in both calls.
    assert _card(default, pid)['last'] is not None
    assert all('cost' not in c for c in default['product_cards'])
    asked = _summary(include_cost=True)
    assert all('cost' in c for c in asked['product_cards'])


def test_wacc_is_cost_price_converted_to_the_row_unit(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _dozen(conn, pid)
    _line(conn, doc_base='IV49501', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100, unit='ตัว')
    _line(conn, doc_base='IV49502', suffix=1, pid=pid, date_iso='2026-01-02',
          qty=1, unit_price=1000, net=1000, unit='โหล')
    data = _summary()
    assert _card(data, pid, 'ตัว')['cost']['wacc_per_unit'] == pytest.approx(60.0)
    assert _card(data, pid, 'โหล')['cost']['wacc_per_unit'] == pytest.approx(720.0)


def test_last_purchase_cost_is_the_latest_purchase_event_in_the_row_unit(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _dozen(conn, pid)
    _ledger(conn, pid, 'INITIAL', '2025-01-01', 40.0)
    _ledger(conn, pid, 'PURCHASE', '2026-01-01', 50.0, ref='RR001')
    _ledger(conn, pid, 'PURCHASE', '2026-03-01', 55.0, ref='RR002')
    # A LATER event that is not a purchase must not win.
    _ledger(conn, pid, 'CONVERSION_IN', '2026-04-01', 99.0, ref='CV001')
    _line(conn, doc_base='IV49503', suffix=1, pid=pid, date_iso='2026-05-01',
          qty=1, unit_price=1000, net=1000, unit='โหล')
    lp = _card(_summary(), pid, 'โหล')['cost']['last_purchase']
    assert lp['per_unit'] == pytest.approx(660.0)   # 55 × 12
    assert lp['date'] == '2026-03-01'
    assert lp['ref'] == 'RR002'


def test_rendering_the_summary_never_writes_the_cost_ledger(cust):
    """get_cost_history lazily RECALCULATES (and commits) for a product with
    no ledger rows — the product page's loader. This page must read only."""
    conn = cust
    pid = _mk_product(conn, cost=60.0, opening_cost=60.0)
    _line(conn, doc_base='IV49504', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100)
    count = lambda: conn.execute(
        "SELECT COUNT(*) FROM product_cost_ledger WHERE product_id = ?", (pid,)).fetchone()[0]
    assert count() == 0
    card = _card(_summary(), pid)
    assert card['cost']['last_purchase'] is None
    assert count() == 0
    # CONTROL: this product is one the lazy loader WOULD write for, so the
    # zero above means "not called", not "called and had nothing to write".
    import models
    models.get_cost_history(pid)
    assert count() > 0


def test_margin_at_last_price_counts_same_product_freebies(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _line(conn, doc_base='IV49505', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=10, unit_price=100, net=1000)
    _line(conn, doc_base='IV49505', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=2, unit_price=0, net=0)
    m = _card(_summary(), pid)['cost']['margin_last']
    # (1000 − 60 × (10 + 2)) ÷ 1000
    assert m['kept'] == pytest.approx(1000.0)
    assert m['paid_cost'] == pytest.approx(600.0)
    assert m['free_cost'] == pytest.approx(120.0)
    assert m['pct'] == pytest.approx(28.0)


def test_margin_at_last_price_converts_a_freebie_in_another_unit(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _dozen(conn, pid)
    _line(conn, doc_base='IV49506', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=10, unit_price=100, net=1000, unit='ตัว')
    _line(conn, doc_base='IV49506', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=0, net=0, unit='โหล')
    m = _card(_summary(), pid, 'ตัว')['cost']['margin_last']
    # (1000 − 60 × (10 + 12)) ÷ 1000
    assert m['free_cost'] == pytest.approx(720.0)
    assert m['pct'] == pytest.approx(-32.0)


def test_margin_arithmetic_ties_to_the_displayed_unit_cost(cust):
    """The reveal shows "ทุน 55.51 × 12" — so the cost it subtracts must BE
    55.51 × 12 = 666.12, not 55.5061475 × 12 = 666.07 (found on real data,
    38จ01 pid 787). Same rule as the resolver's own margin: round the
    per-unit cost to 2 decimals first (price_lookup: cost_per_unit =
    round(cost * ratio, 2)), so both margins on a row use one method."""
    conn = cust
    pid = _mk_product(conn, cost=55.5061475)
    _line(conn, doc_base='IV49524', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=12, unit_price=52.92, net=635.04)
    cost = _card(_summary(), pid)['cost']
    m = cost['margin_last']
    assert cost['wacc_per_unit'] == pytest.approx(55.51)
    assert m['paid_cost'] == pytest.approx(666.12)          # 55.51 × 12, what the screen says
    assert m['profit'] == pytest.approx(-31.08)
    assert m['pct'] == pytest.approx(-4.89)


def test_margin_at_last_price_is_unknown_when_a_freebie_ratio_is_unknown(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _line(conn, doc_base='IV49507', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=10, unit_price=100, net=1000, unit='ตัว')
    _line(conn, doc_base='IV49507', suffix=2, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=0, net=0, unit='แพ็ค')   # no unit_conversions row
    cost = _card(_summary(), pid, 'ตัว')['cost']
    assert cost['wacc_per_unit'] == pytest.approx(60.0)   # control: cost itself is known
    assert cost['margin_last'] is None


def test_margin_at_last_price_on_a_split_vat_bill_uses_the_money_we_keep(cust):
    conn = cust
    pid = _mk_product(conn, cost=60.0)
    _line(conn, doc_base='IV49508', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=10, unit_price=100, net=1000, vat_type=2)
    m = _card(_summary(), pid)['cost']['margin_last']
    assert m['kept'] == pytest.approx(1000.0)     # not 1070 — the 7% is remitted
    assert m['pct'] == pytest.approx(40.0)


def test_margin_today_is_the_resolvers_at_the_customers_last_quantity(cust):
    """Put's ruling B: at the last order's quantity (12), a buy-12-get-1
    bundle applies, so cost carries 13/12 of a unit — 35%, not the 40% the
    same price shows at a quantity of 1."""
    conn = cust
    pid = _mk_product(conn, base=100.0, cost=60.0)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, bundle_buy, "
        " bundle_free, date_start, is_active) VALUES (?, 'ซื้อ 12 แถม 1', 'bundle', 12, 1, "
        " '2024-01-01', 1)", (pid,))
    conn.commit()
    _line(conn, doc_base='IV49509', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=12, unit_price=100, net=1200)
    card = _card(_summary(), pid)
    assert card['today']['price_per_unit'] == pytest.approx(100.0)   # unchanged by qty
    m = card['cost']['margin_today']
    assert m['incl_free_units'] is True
    assert m['cost_side'] == pytest.approx(65.0)
    assert m['pct'] == pytest.approx(35.0)


def test_a_product_with_no_cost_has_no_margin_and_no_badges(cust):
    conn = cust
    pid = _mk_product(conn, base=100.0, cost=0.0)
    _line(conn, doc_base='IV49510', suffix=1, pid=pid, date_iso='2026-01-01',
          qty=1, unit_price=100, net=100)
    cost = _card(_summary(), pid)['cost']
    assert cost['has_cost'] is False
    assert cost['wacc_per_unit'] is None
    assert cost['margin_last'] is None
    assert cost['margin_today'] is None
    assert not any(cost[k] for k in ('last_below_wacc', 'last_below_last_purchase',
                                     'today_below_wacc', 'today_below_last_purchase'))


def test_last_price_below_wacc_is_judged_on_the_money_we_keep(cust):
    """Put's ruling A: 95 + VAT displays as 101.65, above a cost of 100, but
    we keep 95 — that is below cost. The 105 + VAT row is the control."""
    conn = cust
    pid_low = _mk_product(conn, name='ขายต่ำกว่าทุนแยกVAT', cost=100.0)
    pid_ok = _mk_product(conn, name='ขายสูงกว่าทุนแยกVAT', cost=100.0)
    _line(conn, doc_base='IV49511', suffix=1, pid=pid_low, date_iso='2026-01-01',
          qty=1, unit_price=95, net=95, vat_type=2)
    _line(conn, doc_base='IV49512', suffix=1, pid=pid_ok, date_iso='2026-01-01',
          qty=1, unit_price=105, net=105, vat_type=2)
    data = _summary()
    low, ok = _card(data, pid_low), _card(data, pid_ok)
    assert low['last']['price_per_unit'] == pytest.approx(101.65)   # displayed, VAT-incl
    assert low['cost']['last_below_wacc'] is True
    assert ok['cost']['last_below_wacc'] is False


def test_last_price_below_last_purchase_cost_but_above_wacc(cust):
    conn = cust
    pid = _mk_product(conn, cost=50.0)
    _ledger(conn, pid, 'PURCHASE', '2026-01-01', 80.0, ref='RR010')
    _line(conn, doc_base='IV49513', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=70, net=70)
    cost = _card(_summary(), pid)['cost']
    assert cost['last_below_wacc'] is False
    assert cost['last_below_last_purchase'] is True


def test_today_price_badges(cust):
    conn = cust
    pid_under = _mk_product(conn, name='ราคาตั้งต่ำกว่าทุน', base=100.0, cost=110.0)
    pid_lp = _mk_product(conn, name='ราคาตั้งต่ำกว่าทุนซื้อล่าสุด', base=100.0, cost=90.0)
    _ledger(conn, pid_lp, 'PURCHASE', '2026-01-01', 105.0, ref='RR011')
    for pid, doc in ((pid_under, 'IV49514'), (pid_lp, 'IV49515')):
        _line(conn, doc_base=doc, suffix=1, pid=pid, date_iso='2026-02-01',
              qty=1, unit_price=100, net=100)
    data = _summary()
    under, lp = _card(data, pid_under)['cost'], _card(data, pid_lp)['cost']
    assert under['today_below_wacc'] is True
    assert lp['today_below_wacc'] is False
    assert lp['today_below_last_purchase'] is True


# ── Seam 2: HTTP render by role ─────────────────────────────────────────────

def _client(role):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


def _row_cells(html, product_name):
    """{data-label: inner html} of the product-card row naming product_name.
    Scoped on purpose — the page's <script> can mention the same hooks."""
    rows = re.findall(r'<tr data-times-bought.*?</tr>', html, re.S)
    row = next(r for r in rows if product_name in r)
    return dict(re.findall(r'<td data-label="([^"]+)"[^>]*>(.*?)</td>', row, re.S))


def _seed_distinctive(tmp_db):
    """One product whose cost figures are strings nothing else on the page
    carries: WACC 37.13, last purchase 41.27, both margins 62.87%."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn, name='สินค้าทุนลับ', base=100.0, cost=37.13)
    _ledger(conn, pid, 'PURCHASE', '2026-01-01', 41.27, ref='RR020')
    _line(conn, doc_base='IV49520', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=100, net=100)
    name = _name(conn, pid)
    conn.close()
    return name


_SECRETS = ('37.13', '41.27', '62.87', 'ทุนเฉลี่ย', 'ทุนซื้อล่าสุด')


@pytest.mark.parametrize('role', ['staff', 'shareholder'])
def test_non_manager_page_carries_no_cost_anywhere(tmp_db, role):
    name = _seed_distinctive(tmp_db)
    resp = _client(role).get(f'/customer/code/{quote(TEST_CODE)}')
    html = resp.data.decode()
    # CONTROL: the page really rendered this customer's product row.
    assert resp.status_code == 200
    assert _row_cells(html, name)['ราคาล่าสุด']
    for s in _SECRETS:
        assert s not in html, f'{role} page leaks {s!r}'
    # CONTROL: the same seed DOES show every figure to a manager.
    mgr = _row_cells(_client('manager').get(f'/customer/code/{quote(TEST_CODE)}').data.decode(), name)
    mgr_row = ''.join(mgr.values())
    for s in _SECRETS:
        assert s in mgr_row, f'manager row is missing {s!r}'


@pytest.mark.parametrize('role,expect_cost', [('staff', False), ('shareholder', False),
                                              ('manager', True), ('admin', True)])
def test_route_asks_for_cost_only_for_admin_and_manager(tmp_db, role, expect_cost):
    """Data-layer gate: whatever the template does, the page's DATA must not
    contain cost for any other role (nothing to leak through an attribute)."""
    from flask import template_rendered
    from app import app as a
    name = _seed_distinctive(tmp_db)
    seen = []

    def _capture(sender, template, context, **extra):
        if template.name == 'customer_summary.html':
            seen.append(context['data'])

    template_rendered.connect(_capture, a)
    try:
        _client(role).get(f'/customer/code/{quote(TEST_CODE)}')
    finally:
        template_rendered.disconnect(_capture, a)
    assert len(seen) == 1
    cards = seen[0]['product_cards']
    assert any(name in (c['name'] or '') for c in cards)   # control: our row is there
    assert all(('cost' in c) is expect_cost for c in cards)


def test_template_gates_cost_even_if_the_data_carries_it(tmp_db, monkeypatch):
    """Template gate, pinned on its own: hand a staff render data WITH cost
    (a data-layer regression) — the page must still show none of it."""
    import models
    name = _seed_distinctive(tmp_db)
    real = models.get_customer_summary_by_code
    monkeypatch.setattr(models, 'get_customer_summary_by_code',
                        lambda code, *a, **kw: real(code, *a, **{**kw, 'include_cost': True}))
    html = _client('staff').get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    assert _row_cells(html, name)['ราคาล่าสุด']   # control: row rendered
    for s in _SECRETS:
        assert s not in html, f'template leaks {s!r} to staff'


def test_manager_badge_sits_on_the_figure_it_judges(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    _mk_customer(conn)
    _clear_customer(conn)
    # Last price 95 (kept) < WACC 96; today's list price 120 > WACC.
    pid = _mk_product(conn, name='แบดจ์ติดราคาล่าสุด', base=120.0, cost=96.0)
    _line(conn, doc_base='IV49521', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=95, net=95)
    name = _name(conn, pid)
    conn.close()
    cells = _row_cells(_client('manager').get(f'/customer/code/{quote(TEST_CODE)}').data.decode(), name)
    assert '🔴 ต่ำกว่าทุน' in cells['ราคาล่าสุด']
    assert 'ราคาวันนี้' in cells and '120.00' in cells['ราคาวันนี้']   # control: today cell rendered
    assert 'ต่ำกว่าทุน' not in cells['ราคาวันนี้']


def test_manager_margin_reveal_shows_the_rows_own_arithmetic(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn, name='กำไรแตะดู', base=100.0, cost=60.0)
    _line(conn, doc_base='IV49522', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=10, unit_price=100, net=1000)
    _line(conn, doc_base='IV49522', suffix=2, pid=pid, date_iso='2026-02-01',
          qty=2, unit_price=0, net=0)
    name = _name(conn, pid)
    conn.close()
    cell = _row_cells(_client('manager').get(f'/customer/code/{quote(TEST_CODE)}').data.decode(),
                      name)['ราคาล่าสุด']
    # A touch-friendly toggle (button, aria-pressed), explanation hidden until tapped.
    assert re.search(r'<button[^>]*data-margin-reveal[^>]*aria-pressed="false"', cell)
    reveal = re.search(r'<div[^>]*data-margin-detail[^>]*hidden[^>]*>(.*?)</div>', cell, re.S)
    assert reveal is not None
    detail = reveal.group(1)
    for figure in ('1,000.00', '600.00', '120.00', '280.00', '28.00'):
        assert figure in detail, f'reveal is missing {figure}'


class _CellChildren(HTMLParser):
    """Top-level children (elements + non-blank text) of each <td data-label>.
    On phones `table-mobile-cards` makes every td a 2-column grid (label |
    value), so a cell with more than ONE child scatters its lines across both
    columns — seen in a 390px render of 38จ01, the invoice link landing under
    the label and "ทุนเฉลี่ย" beside its own number."""
    VOID = {'br', 'img', 'input', 'hr', 'meta', 'link', 'wbr'}

    def __init__(self):
        super().__init__()
        self.label, self.depth, self.counts = None, 0, {}

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self.label, self.depth = dict(attrs).get('data-label'), 0
            if self.label:
                self.counts[self.label] = 0
            return
        if self.label and self.depth == 0:
            self.counts[self.label] += 1
        if tag not in self.VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'td':
            self.label = None
        elif self.label and tag not in self.VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.label and self.depth == 0 and data.strip():
            self.counts[self.label] += 1


def test_multi_line_cells_hold_one_child_so_the_phone_grid_cannot_scatter(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    _mk_customer(conn)
    _clear_customer(conn)
    # Every optional line at once: 🔴 + ⚠ badges, VAT note, bill discount,
    # freebie, margin + reveal, today's promo + margin, both cost figures.
    pid = _mk_product(conn, name='ทุกบรรทัดในช่อง', base=120.0, cost=100.0)
    _ledger(conn, pid, 'PURCHASE', '2026-01-01', 105.0, ref='RR030')
    conn.execute("INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
                 " date_start, is_active) VALUES (?, 'ลด 10%', 'percent', 10, '2024-01-01', 1)", (pid,))
    _line(conn, doc_base='IV49525', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=10, unit_price=100, net=950, total=980, discount='3%', vat_type=2)
    _line(conn, doc_base='IV49525', suffix=2, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=0, net=0)
    name = _name(conn, pid)
    conn.close()
    html = _client('manager').get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    row = next(r for r in re.findall(r'<tr data-times-bought.*?</tr>', html, re.S) if name in r)
    cells = _row_cells(html, name)
    assert '🔴 ต่ำกว่าทุน' in cells['ราคาล่าสุด'] and 'แถม' in cells['ราคาล่าสุด']   # control: full cell
    p = _CellChildren()
    p.feed(row)
    assert set(p.counts) == {'ครั้งที่ซื้อ', 'ราคาล่าสุด', 'ราคาวันนี้', 'ทุน', 'สต็อก'}   # control
    for label, n in p.counts.items():
        assert n == 1, f'{label} has {n} top-level children'


def test_manager_sees_no_cost_text_for_a_costless_product(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    _mk_customer(conn)
    _clear_customer(conn)
    pid = _mk_product(conn, name='ไม่มีทุนเลย', base=100.0, cost=0.0)
    _line(conn, doc_base='IV49523', suffix=1, pid=pid, date_iso='2026-02-01',
          qty=1, unit_price=100, net=100)
    name = _name(conn, pid)
    conn.close()
    cells = _row_cells(_client('manager').get(f'/customer/code/{quote(TEST_CODE)}').data.decode(), name)
    assert 'ไม่มีทุน' in cells['ทุน']
    assert 'data-margin-reveal' not in cells['ราคาล่าสุด']
    assert '100.00' in cells['ราคาล่าสุด']   # control: the price itself rendered
