"""TDD — `/accounting` incomplete-month reading (fix-list item 2).

CONTEXT.md -> "Internal P&L" -> เดือนที่ข้อมูลยังไม่ครบ (incomplete month): a
month whose cashbook rows have not all been keyed yet must hide กำไรสุทธิ
rather than show it wrong. An account is "expected" for a given month when it
is is_active=1 AND carried a qualifying opex row in >= 3 of that month's
previous 6 calendar months; the month is incomplete when any expected
account has no qualifying row IN it. "Qualifying" = the same population the
statement's ค่าใช้จ่าย uses (direction='expense', account not a transfer
account, category not in _NON_OPEX_CATEGORIES) — an account whose only rows
in the target month are 'ซื้อสินค้า' still reads MISSING (case 8). Presence
is judged over the WHOLE calendar month, never the slice inside
[date_from, date_to] (case 6's two-month period pins this).

`expense_status` is the single field the template branches on:
'no_coverage' | 'incomplete' | 'complete'. `incomplete_months` is
`[{'ym': ..., 'missing': [...]}]`, oldest first, empty unless
expense_status == 'incomplete'.

Fixture: `empty_db` / `empty_db_conn` (full live schema, zero rows) — never
`tmp_db`, every real prior-month cashbook row in the live dev DB would leak
into the lookback window and make an unrelated account "expected".
"""
import re
import sqlite3

import pytest

import models


# ── seed helpers (copied from test_accounting_summary_v2.py, extended with
# display_name/is_active for the account-expectedness cases) ────────────────

def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _mk_product(conn, cost_price=0.0):
    cur = conn.execute(
        "INSERT INTO products (product_name, cost_price) VALUES ('t', ?)",
        (cost_price,))
    return cur.lastrowid


def _mk_sale(conn, date_iso, doc_no, net, product_id=None, qty=1):
    if product_id is None:
        product_id = _mk_product(conn)
    unit_price = net / qty if qty else net
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, product_id, qty, unit_price, net, total)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (date_iso, doc_no, product_id, qty, unit_price, net, net))


def _mk_account(conn, code, is_transfer=0, is_active=1, display_name=None):
    cur = conn.execute(
        "INSERT INTO cashbook_accounts (code, display_name, is_active, is_transfer) "
        "VALUES (?, ?, ?, ?)",
        (code, display_name, is_active, is_transfer))
    return cur.lastrowid


def _mk_expense(conn, account_id, txn_date, amount, category='ค่าเช่า', direction='expense'):
    conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount)
           VALUES (?, ?, ?, ?, ?)""",
        (account_id, txn_date, direction, category, amount))


# ── 1. An expected account missing from the target month -> incomplete ──────

def test_missing_expected_account_marks_month_incomplete(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A', display_name='บัญชี A')
    for ym in ('2026-03', '2026-05', '2026-07'):  # 3 of the previous 6 (Mar-Aug)
        _mk_expense(conn, a, f'{ym}-10', 100.0)
    # no row for A in 2026-09 (the target month)

    b = _mk_account(conn, 'B')  # supplies period coverage; only 1 month -> never expected
    _mk_expense(conn, b, '2026-09-05', 50.0)

    _mk_sale(conn, '2026-09-10', 'IV001', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'incomplete'
    assert s['incomplete_months'] == [{'ym': '2026-09', 'missing': ['บัญชี A']}]
    assert s['net_profit'] is None
    assert s['expenses'] == pytest.approx(50.0)          # real partial number, still shown
    assert s['gross_profit'] == pytest.approx(1000.0)    # unchanged by the expense side


# ── 2. Only 2 of the previous 6 months -> NOT expected -> complete ──────────

def test_account_with_only_two_prior_months_is_not_expected(empty_db):
    conn = _conn(empty_db)
    c = _mk_account(conn, 'C')
    for ym in ('2026-04', '2026-06'):  # 2 of the previous 6 (Mar-Aug) -- below the >=3 clause
        _mk_expense(conn, c, f'{ym}-10', 100.0)
    # ABSENT from 2026-09 on purpose: if the >=3 clause were weaker (e.g. >=1)
    # C would count as expected and this absence would flip the month to
    # incomplete -- that's what makes this fixture able to fail.

    d = _mk_account(conn, 'D')  # supplies real period coverage; 1 month -> never expected
    _mk_expense(conn, d, '2026-09-05', 50.0)

    _mk_sale(conn, '2026-09-10', 'IV002', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'complete'
    assert s['incomplete_months'] == []
    assert s['net_profit'] == pytest.approx(1000.0 - 50.0)


# ── 3. is_active=0 -> never expected, even with rows in all 6 previous months ─

def test_inactive_account_is_never_expected(empty_db):
    conn = _conn(empty_db)
    d = _mk_account(conn, 'D', is_active=0)
    for ym in ('2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08'):
        _mk_expense(conn, d, f'{ym}-10', 100.0)
    # no row for D in 2026-09

    e = _mk_account(conn, 'E')  # supplies period coverage
    _mk_expense(conn, e, '2026-09-05', 50.0)

    _mk_sale(conn, '2026-09-10', 'IV003', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'complete'
    assert s['incomplete_months'] == []
    assert s['net_profit'] is not None


# ── 4. is_transfer=1 -> never expected, same setup as #3 ────────────────────

def test_transfer_account_is_never_expected(empty_db):
    conn = _conn(empty_db)
    f = _mk_account(conn, 'F', is_transfer=1)
    for ym in ('2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08'):
        _mk_expense(conn, f, f'{ym}-10', 100.0)
    # no row for F in 2026-09

    g = _mk_account(conn, 'G')  # supplies period coverage
    _mk_expense(conn, g, '2026-09-05', 50.0)

    _mk_sale(conn, '2026-09-10', 'IV004', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'complete'
    assert s['incomplete_months'] == []
    assert s['net_profit'] is not None


# ── 5. Control: an ordinary complete month ───────────────────────────────────

def test_complete_month_reports_net_profit(empty_db):
    conn = _conn(empty_db)
    op = _mk_account(conn, 'OP')
    _mk_expense(conn, op, '2026-09-10', 100.0)
    _mk_sale(conn, '2026-09-05', 'IV005', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'complete'
    assert s['incomplete_months'] == []
    assert s['net_profit'] == pytest.approx(900.0)


# ── 6. A 2-month period where only the SECOND month is short ────────────────

def test_two_month_period_names_only_the_short_month(empty_db):
    conn = _conn(empty_db)
    h = _mk_account(conn, 'H', display_name='บัญชี H')
    # present in 3 of Aug's previous 6 (Feb-Jul) AND in Aug itself
    for ym in ('2026-03', '2026-05', '2026-07', '2026-08'):
        _mk_expense(conn, h, f'{ym}-10', 100.0)
    # absent from 2026-09, though present in 4 of Sep's previous 6 (Mar-Aug)

    _mk_sale(conn, '2026-08-05', 'IV006a', net=1000.0)
    _mk_sale(conn, '2026-09-05', 'IV006b', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-08-01', '2026-09-30')
    assert s['expense_status'] == 'incomplete'
    assert s['incomplete_months'] == [{'ym': '2026-09', 'missing': ['บัญชี H']}]
    assert s['net_profit'] is None


# ── 7. Zero cashbook rows in the period -> no_coverage, not incomplete ──────

def test_zero_cashbook_rows_is_no_coverage_not_incomplete(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2025-11-05', 'IV007', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2025-11-01', '2025-11-30')
    assert s['expense_status'] == 'no_coverage'
    assert s['incomplete_months'] == []
    assert s['expenses'] is None
    assert s['net_profit'] is None


# ── 7b. Zero opex rows in the target month, but an account IS expected --
#        must read incomplete, never no_coverage (ordering, review round 2) ──

def test_zero_opex_rows_with_expected_accounts_reads_incomplete_not_no_coverage(empty_db):
    conn = _conn(empty_db)
    n_acct = _mk_account(conn, 'N', display_name='บัญชี N')
    for ym in ('2026-03', '2026-05', '2026-07'):  # 3 of the previous 6 (Mar-Aug)
        _mk_expense(conn, n_acct, f'{ym}-10', 100.0)
    # ZERO cashbook rows of any kind in 2026-09 -- not even a real coverage
    # row, unlike case 1. This is the shape that used to read 'no_coverage'.

    _mk_sale(conn, '2026-09-10', 'IV011', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'incomplete'
    assert s['incomplete_months'] == [{'ym': '2026-09', 'missing': ['บัญชี N']}]
    assert s['expenses'] is None
    assert s['net_profit'] is None


# ── 8. An account whose only row in the target month is 'ซื้อสินค้า' still
#       reads MISSING (clarification 1: non-opex category doesn't count) ────

def test_purchase_only_row_in_target_month_still_reads_missing(empty_db):
    conn = _conn(empty_db)
    i_acct = _mk_account(conn, 'I', display_name='บัญชี I')
    for ym in ('2026-03', '2026-05', '2026-07'):
        _mk_expense(conn, i_acct, f'{ym}-10', 100.0, category='ค่าเช่า')
    # target month: only a ซื้อสินค้า (non-opex) row -- must NOT count as present
    _mk_expense(conn, i_acct, '2026-09-05', 999.0, category='ซื้อสินค้า')

    j_acct = _mk_account(conn, 'J')  # supplies real period coverage
    _mk_expense(conn, j_acct, '2026-09-06', 50.0, category='ค่าเช่า')

    _mk_sale(conn, '2026-09-10', 'IV008', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['expense_status'] == 'incomplete'
    assert s['incomplete_months'] == [{'ym': '2026-09', 'missing': ['บัญชี I']}]
    assert s['expenses'] == pytest.approx(50.0)   # the ซื้อสินค้า row stays excluded from the total


# ── 9. Render test ────────────────────────────────────────────────────────

@pytest.fixture
def admin_client_empty(empty_db):
    """Same shape as test_bp_ecommerce_routes.py's admin_client_empty --
    a data-less schema-only DB (empty_db), authed as admin."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _extract_element(html, elem_id):
    """Pull the id="<elem_id>" element out by itself so an assertion can't
    match text sitting elsewhere on the page (Thai-substring trap,
    .claude/rules/verification-discipline.md). The alert div nests no
    further <div>, so the first </div> after the opening tag IS its close."""
    m = re.search(
        r'<div[^>]*id="' + re.escape(elem_id) + r'"[^>]*>.*?</div>',
        html, re.S)
    return m.group(0) if m else None


def test_render_incomplete_month_hides_net_profit(empty_db, admin_client_empty):
    conn = _conn(empty_db)
    k = _mk_account(conn, 'K', display_name='บัญชี K')
    for ym in ('2026-03', '2026-05', '2026-07'):
        _mk_expense(conn, k, f'{ym}-10', 100.0)
    m_acct = _mk_account(conn, 'M')
    _mk_expense(conn, m_acct, '2026-09-05', 50.0)
    _mk_sale(conn, '2026-09-10', 'IV009', net=1000.0)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/accounting?date_from=2026-09-01&date_to=2026-09-30')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    elem = _extract_element(html, 'incomplete-month-alert')
    assert elem is not None
    assert 'บัญชี K' in elem

    net_card = re.search(
        r'กำไรสุทธิโดยประมาณ</div>\s*<div class="stat-card-value[^"]*"[^>]*>\s*(.*?)\s*</div>',
        html, re.S)
    assert net_card is not None
    assert re.search(r'\d', net_card.group(1)) is None  # no number rendered


def test_render_complete_month_has_no_incomplete_alert(empty_db, admin_client_empty):
    conn = _conn(empty_db)
    op = _mk_account(conn, 'OP')
    _mk_expense(conn, op, '2026-09-10', 100.0)
    _mk_sale(conn, '2026-09-05', 'IV010', net=1000.0)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/accounting?date_from=2026-09-01&date_to=2026-09-30')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    assert _extract_element(html, 'incomplete-month-alert') is None

    net_card = re.search(
        r'กำไรสุทธิโดยประมาณ</div>\s*<div class="stat-card-value[^"]*"[^>]*>\s*(.*?)\s*</div>',
        html, re.S)
    assert net_card is not None
    assert re.search(r'\d', net_card.group(1)) is not None
