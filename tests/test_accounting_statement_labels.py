"""TDD — `/accounting` carries the งบกองรวม label in EVERY page state (#593
stories 13, 14, 16).

CONTEXT.md -> "Internal P&L" -> งบกองรวม (pooled statement): the statement's
revenue is BSN only while its expenses are whatever flowed through the shared
accounts, which mixes both companies. ADR 0016 says the label is not optional
decoration — an unlabelled version of this statement is a wrong number
presented as a right one (story 14: present in EVERY state, not only the
months where the mixing is large).

Story 16's existing guard (test_accounting_prior_period_expense.py::
test_page_never_says_pid_duean_or_pid_banchi) renders exactly ONE page state
— a complete March with a prior-period row. The incomplete-month banner, the
no-coverage banner, and the mixed-basis table never render in that fixture,
so a ปิดเดือน typed into the incomplete-month banner (the text most likely to
attract that word) would pass the old guard. This file closes that gap by
rendering all five page states the template can produce, plus a source-level
sweep for branches no single fixture reaches.

Fixture: `empty_db` (schema-only clone, zero rows) — never `tmp_db`, whose
live-DB copy would leak real cashbook/ledger rows into every assertion.
Helpers are copied locally (repo convention for this test seam), not
imported from sibling test files.
"""
import os
import re
import sqlite3

import pytest

import sales_filters


CUT = sales_filters.COGS_HISTORICAL_FROM          # '2026-03-03'

_FORBIDDEN = ['ปิดเดือน', 'ปิดบัญชี', 'งบการเงิน', 'งบรวม', 'งบ BSN']

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'inventory_app', 'templates', 'accounting.html',
)


# ── seed helpers (same shape as test_accounting_prior_period_expense.py and
#    test_accounting_cogs_basis.py) ──────────────────────────────────────────

def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _mk_product(conn, cost_price=0.0, unit_type='ตัว', brand_id=None):
    return conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, brand_id) "
        "VALUES ('t', ?, ?, ?)", (unit_type, cost_price, brand_id)).lastrowid


def _mk_ledger(conn, product_id, event_date, wacc_after, event_type='PURCHASE'):
    return conn.execute(
        """INSERT INTO product_cost_ledger
             (product_id, event_type, event_date, qty_change, unit_cost,
              stock_after, wacc_after)
           VALUES (?, ?, ?, 1, ?, 1, ?)""",
        (product_id, event_type, event_date, wacc_after, wacc_after)).lastrowid


def _mk_sale(conn, date_iso, doc_no, net, product_id=None, qty=1, unit='ตัว'):
    if product_id is None:
        product_id = _mk_product(conn)
    unit_price = net / qty if qty else net
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, doc_base, product_id, qty, unit, unit_price, net, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (date_iso, doc_no, doc_no, product_id, qty, unit, unit_price, net, net))


def _mk_account(conn, code, is_transfer=0, is_active=1, display_name=None):
    cur = conn.execute(
        "INSERT INTO cashbook_accounts (code, display_name, is_active, is_transfer) "
        "VALUES (?, ?, ?, ?)",
        (code, display_name, is_active, is_transfer))
    return cur.lastrowid


def _mk_expense(conn, account_id, txn_date, amount, category='ค่าเช่า',
                direction='expense', belongs_to_period=None, description=None):
    cur = conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount, description,
              belongs_to_period)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (account_id, txn_date, direction, category, amount, description,
         belongs_to_period))
    return cur.lastrowid


@pytest.fixture
def admin_client_empty(empty_db):
    """Same shape as the sibling accounting test files — a data-less
    schema-only DB (empty_db), authed as admin."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _extract_element(html, elem_id):
    """Pull the id="<elem_id>" element out by itself, counting <div> depth so
    a NESTED div cannot truncate the fragment. Returns None when absent.
    Copied from test_accounting_prior_period_expense.py."""
    m = re.search(r'<div[^>]*id="' + re.escape(elem_id) + r'"[^>]*>', html)
    if not m:
        return None
    depth = 0
    for tok in re.finditer(r'<div\b|</div>', html[m.start():]):
        depth += 1 if tok.group(0) != '</div>' else -1
        if depth == 0:
            return html[m.start():m.start() + tok.end()]
    raise AssertionError(f'unbalanced <div> after id="{elem_id}"')


# ── per-state seeding + control assertion ───────────────────────────────────
# Each seed function returns (date_from, date_to). Each control function
# asserts the ONE thing that only its intended state can emit, so a wrong
# branch cannot satisfy it by accident.

def _seed_complete(conn):
    op = _mk_account(conn, 'OP')
    _mk_expense(conn, op, '2026-06-10', 100.0)
    _mk_sale(conn, '2026-06-05', 'IVC1', net=1000.0)
    return '2026-06-01', '2026-06-30'


def _control_complete(html):
    # the net-profit value renders (not "ยังไม่แสดง" / "คำนวณไม่ได้")
    net_card = re.search(
        r'กำไรสุทธิโดยประมาณ</div>\s*<div class="stat-card-value[^"]*"[^>]*>\s*(.*?)\s*</div>',
        html, re.S)
    assert net_card is not None
    assert re.search(r'\d', net_card.group(1)) is not None, \
        f'net-profit did not render a number: {net_card.group(1)!r}'


def _seed_no_coverage(conn):
    _mk_sale(conn, '2025-11-05', 'IVNC1', net=1000.0)
    return '2025-11-01', '2025-11-30'


def _control_no_coverage(html):
    assert 'ช่วงนี้ยังไม่มีข้อมูลค่าใช้จ่ายในสมุดรับ-จ่าย' in html


def _seed_incomplete(conn):
    k = _mk_account(conn, 'K', display_name='บัญชี K')
    for ym in ('2026-03', '2026-05', '2026-07'):   # 3 of the previous 6 (Mar-Aug)
        _mk_expense(conn, k, f'{ym}-10', 100.0)
    # k has no row in the target month -> "missing"
    m_acct = _mk_account(conn, 'M')                 # supplies real coverage; 1 month -> never expected
    _mk_expense(conn, m_acct, '2026-09-05', 50.0)
    _mk_sale(conn, '2026-09-10', 'IVI1', net=1000.0)
    return '2026-09-01', '2026-09-30'


def _control_incomplete(html):
    assert _extract_element(html, 'incomplete-month-alert') is not None


def _seed_prior_period(conn):
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68', belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IVPP1', net=1000.0)
    return '2026-03-01', '2026-03-31'


def _control_prior_period(html):
    assert _extract_element(html, 'prior-period-expense') is not None


def _seed_mixed_basis(conn):
    # an opex row keeps no_coverage the ONLY state without expenses, so a
    # label that ever gets gated on expenses fails in exactly one named case.
    a = _mk_account(conn, 'MB')
    _mk_expense(conn, a, '2026-03-15', 50.0)
    pid = _mk_product(conn, cost_price=100.0)
    _mk_ledger(conn, pid, CUT, wacc_after=10.0, event_type='INITIAL')
    _mk_sale(conn, '2026-03-02', 'IVMB1', net=500.0, product_id=pid, qty=1)  # pre-cutover
    _mk_sale(conn, '2026-03-10', 'IVMB2', net=500.0, product_id=pid, qty=1)  # post-cutover
    return '2026-03-01', '2026-03-31'


def _control_mixed_basis(html):
    assert _extract_element(html, 'cogs-basis-by-month') is not None


_STATES = {
    'complete': (_seed_complete, _control_complete),
    'no_coverage': (_seed_no_coverage, _control_no_coverage),
    'incomplete': (_seed_incomplete, _control_incomplete),
    'prior_period': (_seed_prior_period, _control_prior_period),
    'mixed_basis': (_seed_mixed_basis, _control_mixed_basis),
}


# ── 1. Every page state carries the label and never the forbidden words ────

@pytest.mark.parametrize('state', sorted(_STATES))
def test_every_page_state_carries_the_pooled_label(empty_db, admin_client_empty, state):
    seed, control = _STATES[state]
    conn = _conn(empty_db)
    date_from, date_to = seed(conn)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get(
        f'/accounting?date_from={date_from}&date_to={date_to}')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    # the state's own control FIRST -- proves this fixture actually reached
    # the branch it claims to, before anything else is asserted about it
    control(html)

    elem = _extract_element(html, 'pooled-statement')
    assert elem is not None, f'id="pooled-statement" absent in state={state}'
    assert 'งบกองรวม' in elem

    h1 = re.search(r'<h1 class="page-title">(.*?)</h1>', html, re.S)
    assert h1 is not None
    assert 'งบกองรวม' in h1.group(1), f'h1 missing งบกองรวม in state={state}: {h1.group(1)!r}'
    assert '(BSN)' not in h1.group(1), f'h1 still says (BSN) in state={state}'

    for word in _FORBIDDEN:
        assert word not in html, f'{word!r} found on the page in state={state}'


# ── 2. Source guard over every branch, including ones no fixture renders ───
# {% if %} branches a fixture never enters are invisible to the render test
# above. This reads the template SOURCE instead, after stripping Jinja
# comments -- a forbidden word sitting inside a `{# ... #}` never reaches the
# screen, so it is deliberately allowed there.

def test_source_has_no_forbidden_word_outside_a_jinja_comment():
    with open(_TEMPLATE_PATH, encoding='utf-8') as f:
        src = f.read()
    stripped = re.sub(r'\{#.*?#\}', '', src, flags=re.S)

    # CONTROL, asserted first: the strip did not eat the template. A strip
    # helper that returned '' would otherwise make every assertion below
    # vacuously pass.
    for marker in ('ข้อมูลค่าใช้จ่ายยังไม่ครบ', 'ค่าใช้จ่ายของงวดก่อน',
                    'ใช้ 2 เกณฑ์ปนกัน'):
        assert marker in stripped, \
            f'control marker {marker!r} missing from stripped source -- strip ate the template'

    for word in _FORBIDDEN:
        assert word not in stripped, f'{word!r} found outside any Jinja comment'
