"""TDD for #498 milestone 3 — customer page: the เสนอเพิ่ม card.

Seam 1 (data-layer wiring): get_customer_summary_by_code's new 'suggestions'
key — additive, date-filter INDEPENDENT (the helper takes no date params at
all, see test_498_cross_sell_suggestions.py), top_products/product_cards
byte-for-byte unchanged.
Seam 2 (HTTP / render): the card itself, session-injected roles — prior art
tests/test_497_customer_page_winback_notes.py, tests/test_493_slice3_cost_block.py.

Prior art for fixtures: tests/test_498_cross_sell_suggestions.py (shares its
helpers), tests/test_497_customer_page_winback_notes.py.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import pytest

SENDAI_BRAND_ID = 3

TEST_CODE = 'TEST4981'
TEST_NAME = 'ลูกค้าทดสอบ 498 หน้าลูกค้า'

_pid_counter = [498100]


def _mk_product(conn, name='สินค้าทดสอบ 498 หน้าลูกค้า', unit_type='ตัว', base=100.0, cost=60.0,
                 sub_category=None, is_active=1):
    _pid_counter[0] += 1
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, base_sell_price, cost_price, "
        "brand_id, is_active, sub_category) VALUES (?,?,?,?,?,?,?)",
        (f"{name} #{_pid_counter[0]}", unit_type, base, cost, SENDAI_BRAND_ID, is_active,
         sub_category),
    )
    conn.commit()
    return cur.lastrowid


def _mk_customer(conn, code=TEST_CODE, name=TEST_NAME):
    conn.execute(
        "INSERT INTO customers (code, name) VALUES (?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
        (code, name),
    )
    conn.commit()


def _set_stock(conn, pid, qty):
    conn.execute(
        "INSERT INTO stock_levels (product_id, quantity) VALUES (?, ?) "
        "ON CONFLICT(product_id) DO UPDATE SET quantity = excluded.quantity",
        (pid, qty),
    )
    conn.commit()


def _line(conn, *, doc_base, suffix, pid, date_iso, code, name=None, qty=1,
          unit_price=100, net=100, unit='ตัว'):
    name = name or code
    conn.execute(
        "INSERT INTO sales_transactions "
        "(date_iso, doc_no, doc_base, product_id, customer, customer_code, "
        " qty, unit, unit_price, vat_type, total, net) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
        (date_iso, f'{doc_base}-{suffix}', doc_base, pid, name, code, qty, unit,
         unit_price, net, net),
    )
    conn.commit()


def _other_shops(conn, pid, n, *, prefix, date_iso, start=0):
    for i in range(start, start + n):
        code = f'{prefix}{i:03d}'
        _line(conn, doc_base=f'IV{prefix}{i:03d}', suffix=1, pid=pid, date_iso=date_iso,
              code=code)


def _clear(conn, *codes):
    for code in codes:
        conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (code,))
    conn.commit()


def _wipe_real_recent_history(conn, days=800):
    """The route always calls the suggestions helper with the REAL
    wall-clock today (no override) -- `tmp_db` clones the live dev DB WITH
    its data, and real B2B history inside a real trailing-24-month window
    would legitimately outrank a small fixture's shop_count, pushing it out
    of the top-10 the page actually renders. `days=800` safely covers more
    than the 730-day window measured back from today, so after this call
    NO real row can qualify -- only fixture rows inserted afterward can.
    Deletes globally (every customer), which is safe here: it never touches
    stock_levels/transactions (no trigger on sales_transactions DELETE
    besides audit_log), and this is a throwaway per-test tmp copy of the DB,
    never the live one."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    conn.execute("DELETE FROM sales_transactions WHERE date_iso >= ?", (cutoff,))
    conn.commit()


def _client(tmp_db, role='admin'):
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = role
        s['role'] = role
    return c


@pytest.fixture
def cust(tmp_db_conn):
    conn = tmp_db_conn
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    yield conn
    _clear(conn, TEST_CODE)


def _card_html(html):
    """The เสนอเพิ่ม card's own fragment — never a page-wide substring (the
    card header icon is unique; the same product name pattern can also
    appear on สินค้าที่ซื้อบ่อย for a DIFFERENT customer's card, and hook
    names/other data can hide inside a <script> block)."""
    m = re.search(
        r'<i class="bi bi-lightbulb me-2 text-accent"></i>เสนอเพิ่ม.*?(?=\{% endblock %\}|$)',
        html, re.S)
    # Template renders, not source — find the card's own containing <div
    # class="card"> block by scanning forward to the matching depth-0 close.
    start = html.find('<i class="bi bi-lightbulb me-2 text-accent"></i>เสนอเพิ่ม')
    assert start != -1, 'เสนอเพิ่ม card header not found'
    # Walk back to the opening <div class="card"> that owns this header.
    card_open = html.rfind('<div class="card">', 0, start)
    assert card_open != -1
    # Find the matching close by depth-counting <div ...> vs </div> from card_open.
    depth = 0
    i = card_open
    for tag in re.finditer(r'<div\b[^>]*>|</div>', html[card_open:]):
        depth += 1 if tag.group(0).startswith('<div') else -1
        if depth == 0:
            return html[card_open:card_open + tag.end()]
    raise AssertionError('unbalanced <div> while scoping the เสนอเพิ่ม card')


# ── Seam 1: data-layer wiring ────────────────────────────────────────────────

def test_summary_wires_suggestions_key_from_the_helper(cust, monkeypatch):
    """The suggestion LOGIC (population, ranking, exclusions, grouping) is
    exercised end-to-end, with a controllable `today`, in
    tests/test_498_cross_sell_suggestions.py. `get_customer_summary_by_code`
    always calls the helper with the REAL wall-clock today (no override),
    and `tmp_db_conn` clones the LIVE dev DB with its data — real B2B
    history legitimately produces more than 10 qualifying products at
    real-today, which would swamp any small fixture and make a
    membership/count assertion here flaky. So this seam test proves
    DELEGATION instead, deterministically: the summary's 'suggestions' key
    is exactly whatever the helper returns, called with exactly
    (conn, customer_code) — no extra args."""
    monkeypatch_calls = []
    sentinel = [{'product_id': 999999999, 'marker': 'STUB-498'}]

    def _stub(conn_arg, customer_code):
        monkeypatch_calls.append(customer_code)
        return sentinel

    import models.customers as customers
    monkeypatch.setattr(customers, '_cross_sell_suggestions', _stub)

    data = customers.get_customer_summary_by_code(TEST_CODE)
    assert data['suggestions'] is sentinel
    assert monkeypatch_calls == [TEST_CODE]
    # top_products/product_cards are untouched by this feature.
    assert 'top_products' in data and 'product_cards' in data


def test_summary_never_threads_the_date_filter_into_suggestions(cust, monkeypatch):
    """Structural, deterministic version of "independent of the page's date
    filter": the stub's signature takes no date kwargs at all, so if the
    call site ever grew `date_from=date_from` this would raise TypeError
    instead of silently passing."""
    calls = []

    def _stub(conn_arg, customer_code):
        calls.append(customer_code)
        return []

    import models.customers as customers
    monkeypatch.setattr(customers, '_cross_sell_suggestions', _stub)

    customers.get_customer_summary_by_code(
        TEST_CODE, date_from='2020-01-01', date_to='2020-12-31')
    assert calls == [TEST_CODE]


def test_suggestions_helper_skipped_for_a_nonexistent_code(tmp_db_conn, monkeypatch):
    """N-404 (review round 1): a code with no master row and no sales
    history at all must never reach the suggestions helper's aggregate
    query + resolve_price calls -- it is about to 404 anyway. Control:
    test_summary_wires_suggestions_key_from_the_helper (above) proves the
    helper IS called for a real, existing code."""
    conn = tmp_db_conn
    ghost_code = 'GHOST4981NOTREAL'
    conn.execute("DELETE FROM customers WHERE code = ?", (ghost_code,))
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (ghost_code,))
    conn.commit()

    calls = []

    def _stub(conn_arg, customer_code):
        calls.append(customer_code)
        return []

    import models.customers as customers
    monkeypatch.setattr(customers, '_cross_sell_suggestions', _stub)

    data = customers.get_customer_summary_by_code(ghost_code)
    assert data['exists'] is False
    assert calls == []


def test_suggestions_helper_still_runs_for_a_code_with_sales_but_no_master_row(
        tmp_db_conn, monkeypatch):
    """Companion control for the N-404 gate above: the early existence
    probe ORs the customers-master check with a sales_transactions check,
    so a code with real bill history but NO master row (the "code with no
    master row renders the same" case the spec calls out) must still be
    treated as existing, and the suggestions helper must still run."""
    conn = tmp_db_conn
    code = 'NOMASTER4981'
    conn.execute("DELETE FROM customers WHERE code = ?", (code,))
    conn.execute("DELETE FROM sales_transactions WHERE customer_code = ?", (code,))
    conn.execute(
        "INSERT INTO sales_transactions (date_iso, doc_no, doc_base, customer, "
        "customer_code, qty, unit, unit_price, vat_type, total, net) "
        "VALUES ('2020-01-01','IVNOMASTER-1','IVNOMASTER',?,?,1,'ตัว',100,0,100,100)",
        ('ร้านไม่มีมาสเตอร์ 498', code),
    )
    conn.commit()

    calls = []

    def _stub(conn_arg, customer_code):
        calls.append(customer_code)
        return []

    import models.customers as customers
    monkeypatch.setattr(customers, '_cross_sell_suggestions', _stub)

    data = customers.get_customer_summary_by_code(code)
    assert data['exists'] is True
    assert calls == [code]


def test_date_filter_still_changes_product_cards(cust):
    """Companion control for the two stub tests above: proves the date
    filter reaches product_cards at all (so "independent of the date
    filter" is a meaningful claim about a filter that actually does
    something, not a filter that is a no-op everywhere)."""
    conn = cust
    pid_bought = _mk_product(conn, name='สินค้าเก่าที่ซื้อไปแล้ว วันที่')
    for i, d in enumerate(['2020-01-01', '2020-02-01']):
        _line(conn, doc_base=f'IVDTF{i}', suffix=1, pid=pid_bought, date_iso=d, code=TEST_CODE)

    import models.customers as customers
    unfiltered = customers.get_customer_summary_by_code(TEST_CODE)
    filtered = customers.get_customer_summary_by_code(TEST_CODE, date_from='2020-01-15')

    def _card(data):
        return next((c for c in data['product_cards'] if c['product_id'] == pid_bought), None)

    assert _card(unfiltered)['times_bought'] == 2
    assert _card(filtered)['times_bought'] == 1


# ── Seam 2: HTTP / render ────────────────────────────────────────────────────

def test_customer_page_shows_a_qualifying_suggestion(tmp_db):
    """The route always calls the helper with today=None (real wall clock),
    so — unlike the helper-level tests, which isolate themselves against a
    far-future `today` — a render test must seed dates inside a REAL
    trailing-24-month window: "yesterday" real time."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    _wipe_real_recent_history(conn)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    pid = _mk_product(conn, name='สินค้าขายดีหน้าเว็บ 498')
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='RENDER', date_iso=yesterday)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    assert 'สินค้าขายดีหน้าเว็บ 498' in card
    assert '4 ร้านซื้อ (24 เดือน)' in card
    assert 'ขายดีที่ร้านนี้ยังไม่มี' in card
    # T8 (review round 1): "the product name, linked to the product page" --
    # parse the PATH (url_for appends a `?book=` query param via the book
    # registry's url_defaults hook, so an exact href string would be wrong).
    hrefs = re.findall(r'<a\b[^>]*?\bhref="([^"]*)"', card)
    assert any(urlsplit(h).path == f'/products/{pid}' for h in hrefs)
    # T4 (review round 1): the empty-state line must NOT render when real
    # rows are present -- control for the negative assertion in
    # test_customer_page_shows_empty_state_when_nothing_qualifies below.
    assert 'ยังไม่มีสินค้าแนะนำ' not in card


def test_customer_page_shows_empty_state_when_nothing_qualifies(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    # Real dev-DB history at real wall-clock today would otherwise
    # legitimately qualify (TEST_CODE has bought nothing -> plain top-10),
    # making a genuinely empty result impossible to reach without this.
    _wipe_real_recent_history(conn)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    assert 'ยังไม่มีสินค้าแนะนำ' in card


def test_suggestion_card_control_it_actually_rendered(tmp_db):
    """Control for the negative tests below: the card element itself is
    findable at all on an unmodified page."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    assert 'เสนอเพิ่ม' in card
    assert len(card) > 200  # sanity: not a truncated/empty fragment


def test_tab_button_meets_the_44px_touch_target(tmp_db):
    """N-tab (review round 1): the lone tab button measured 39px tall at
    390px in a real browser (docs/mobile-conventions.md section 5's floor
    is 44px) -- `.btn-touch` is scoped to the same
    `@media (max-width: 991.98px)` block as every other mobile touch
    target, so it changes nothing at desktop widths."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    tab_button = re.search(r'<button\b[^>]*data-bs-toggle="tab"[^>]*>', card)
    assert tab_button, 'tab button not found'
    assert 'btn-touch' in tab_button.group(0)


@pytest.mark.parametrize('role', ['staff', 'admin'])
def test_no_other_shops_name_or_code_appears_in_the_suggestion_card(tmp_db, role):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    _wipe_real_recent_history(conn)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    pid = _mk_product(conn, name='สินค้าเช็คชื่อร้านรั่ว 498')
    _set_stock(conn, pid, 50)
    other_names = ['ร้านลับ498หนึ่ง', 'ร้านลับ498สอง', 'ร้านลับ498สาม', 'ร้านลับ498สี่']
    other_codes = ['SECRET4980A', 'SECRET4980B', 'SECRET4980C', 'SECRET4980D']
    for code, name in zip(other_codes, other_names):
        _line(conn, doc_base=f'IV{code}', suffix=1, pid=pid, date_iso=yesterday,
              code=code, name=name)
    conn.close()

    c = _client(tmp_db, role=role)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    # Control: the row for THIS product actually rendered inside the card.
    assert 'สินค้าเช็คชื่อร้านรั่ว 498' in card
    for code, name in zip(other_codes, other_names):
        assert code not in card
        assert name not in card


def test_suggestions_section_has_no_cost_or_margin_text(tmp_db):
    """No cost/WACC/margin surfaces on suggestions even for admin (#498's
    own rule) -- the product it names ALSO appears on สินค้าที่ซื้อบ่อย with
    real margin/cost text for admin, so this scopes to the suggestion
    card's own fragment specifically, never the whole page."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    _wipe_real_recent_history(conn)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    pid = _mk_product(conn, name='สินค้าเช็คไม่มีต้นทุน 498', base=100.0, cost=60.0)
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='NOCOST', date_iso=yesterday)
    conn.close()

    c = _client(tmp_db, role='admin')
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    card = _card_html(html)
    assert 'สินค้าเช็คไม่มีต้นทุน 498' in card
    for forbidden in ('ทุนเฉลี่ย', 'ทุนซื้อล่าสุด', 'กำไร', 'data-margin-reveal', 'WACC'):
        assert forbidden not in card


def test_card_sits_after_the_product_cards_card(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    conn.close()

    c = _client(tmp_db)
    html = re.sub(r'<script\b.*?</script>', '', c.get(
        f'/customer/code/{quote(TEST_CODE)}').data.decode(), flags=re.S | re.I)
    products_hdr = '<i class="bi bi-trophy me-2 text-accent"></i>สินค้าที่ซื้อบ่อย'
    suggest_hdr = '<i class="bi bi-lightbulb me-2 text-accent"></i>เสนอเพิ่ม'
    pos_products = html.find(products_hdr)
    pos_suggest = html.find(suggest_hdr)
    assert pos_products != -1 and pos_suggest != -1
    assert pos_products < pos_suggest


class _CellChildren(HTMLParser):
    """Top-level children (elements + non-blank text) of each <td
    data-label> — prior art tests/test_493_slice3_cost_block.py. On phones
    `table-mobile-cards` makes every td a 2-column grid (label | value), so
    a cell with more than ONE child scatters its lines across both
    columns."""
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


def test_suggestion_row_cells_hold_one_top_level_child_each(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    _mk_customer(conn)
    _clear(conn, TEST_CODE)
    _wipe_real_recent_history(conn)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    pid = _mk_product(conn, name='สินค้าตรวจกริดมือถือ 498', base=60.0)
    conn.execute(
        "INSERT INTO promotions (product_id, promo_name, promo_type, discount_value, "
        " date_start, is_active) VALUES (?, 'ลด 10%', 'percent', 10, '2024-01-01', 1)",
        (pid,))
    conn.commit()
    _set_stock(conn, pid, 50)
    _other_shops(conn, pid, 4, prefix='GRID', date_iso=yesterday)
    conn.close()

    c = _client(tmp_db)
    html = c.get(f'/customer/code/{quote(TEST_CODE)}').data.decode()
    row_match = re.search(r'<tr>\s*<td class="td-primary">.*?สินค้าตรวจกริดมือถือ 498.*?</tr>',
                           html, re.S)
    assert row_match, 'suggestion row not found'
    p = _CellChildren()
    p.feed(row_match.group(0))
    assert set(p.counts) == {'ร้านซื้อ', 'ราคาวันนี้', 'สต็อก'}   # control
    for label, n in p.counts.items():
        assert n == 1, f'{label} has {n} top-level children'
