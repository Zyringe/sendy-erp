"""TDD — `/accounting` ค่าใช้จ่ายของงวดก่อน (spec #593, user stories 8-12, 17).

CONTEXT.md -> "Internal P&L" -> ค่าใช้จ่ายของงวดก่อน (prior-period expense):
a cost paid in this month that belongs to an earlier period gets its OWN
line, separate from ค่าใช้จ่ายดำเนินงาน, so the month's trading result is
measured without it while the money stays visible. ADR 0014 decision 3.

The period is carried by `cashbook_transactions.belongs_to_period`
(migration 187): NULL means "the cost belongs to the month it was paid"
(every row before this migration), a set value is the Gregorian period it
belongs to, 'YYYY' or 'YYYY-MM'. Its CHECK makes it structurally impossible
to store a period that is not strictly earlier than the payment month, or a
value that is not a period at all — that CHECK IS story 11 ("only when the
period it belongs to has been identified"), encoded in the schema rather
than in prose.

The partition is over the SAME population `ค่าใช้จ่าย` already reads:
direction='expense', `ca.is_transfer = 0`, category not in
`_NON_OPEX_CATEGORIES`. Deliberately NOT widened — the six ฿167,500 rows sit
on account `904` (is_transfer=1 today) and stay invisible until ADR 0017
un-flags it. Case 7 pins that.

`net_profit` = `gross_profit - (expenses + prior_period_expenses)`, i.e. its
VALUE does not move (story 10: every baht still lands in the bottom line).
Case 1's control proves that at two decimal places; the one_bit case proves
it at full precision, which is where the parentheses turn out to matter.

Fixture: `empty_db` / `admin_client_empty` (full live schema, zero rows) —
never `tmp_db`, whose live-DB copy carries 458 real cashbook rows that would
leak into every assertion here.
"""
import re
import sqlite3

import pytest

import models


# ── seed helpers (same shape as test_accounting_incomplete_month.py, plus
#    the belongs_to_period argument this file exists to exercise) ───────────

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


# ── 1. A stamped row leaves ค่าใช้จ่ายดำเนินงาน and lands on the new line,
#       and กำไรสุทธิ does not move. The control is the same fixture with the
#       stamp removed — it is what makes this pair able to fail. ────────────

def test_stamped_row_moves_to_the_prior_period_line_without_moving_net_profit(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68', belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV101', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert s['expenses'] == pytest.approx(100.0)
    assert s['prior_period_expenses'] == pytest.approx(400.0)
    assert s['net_profit'] == pytest.approx(1000.0 - 100.0 - 400.0)


def test_control_the_same_rows_unstamped_are_all_operating_expense(empty_db):
    """Same fixture as the case above with belongs_to_period left NULL: every
    baht sits in ค่าใช้จ่ายดำเนินงาน, the prior-period line is 0.00, and
    net_profit is the SAME number. Story 10."""
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68')
    _mk_sale(conn, '2026-03-05', 'IV102', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert s['expenses'] == pytest.approx(500.0)
    assert s['prior_period_expenses'] == pytest.approx(0.0)
    assert s['net_profit'] == pytest.approx(1000.0 - 500.0)


def test_stamping_a_row_does_not_move_net_profit_by_one_bit(empty_db):
    """Story 10 at full precision. Same DB, same rows, read twice — the only
    difference between the two reads is the stamp — so `==` is the assertion,
    not `approx`: a re-association of the subtraction that moved the result
    by 1 ULP would pass `approx` and still contradict the claim being made
    ("this number does not move"). The fixture's amounts are chosen so the
    arithmetic is NOT exact in binary; with round numbers this test cannot
    fail."""
    # These three amounts are NOT arbitrary: they are a searched triple for
    # which `X - (e + p)` and `(X - e) - p` differ in the last bit
    # (-214254.95 vs -214254.94999999998). With round numbers the two forms
    # agree and this test cannot fail — that is how the first draft of it was
    # vacuous.
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 268405.12, category='ค่าเช่า')
    stamped_id = _mk_expense(conn, a, '2026-03-09', 183478.77,
                             category='จ่ายค่าโบนัส', belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV114', net=237628.94)
    conn.commit()
    conn.close()

    split = models.get_accounting_summary('2026-03-01', '2026-03-31')

    conn = _conn(empty_db)
    conn.execute("UPDATE cashbook_transactions SET belongs_to_period = NULL WHERE id = ?",
                 (stamped_id,))
    conn.commit()
    conn.close()

    whole = models.get_accounting_summary('2026-03-01', '2026-03-31')

    assert whole['prior_period_expenses'] == 0.0        # control: the stamp really went
    assert split['prior_period_expenses'] == 183478.77
    assert split['expenses'] != whole['expenses']       # control: the split really happened
    assert split['net_profit'] == whole['net_profit']


# ── 2. expenses_by_category follows the same partition ─────────────────────

def test_category_wholly_prior_period_leaves_the_category_breakdown(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV103', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    names = [c['category_name'] for c in s['expenses_by_category']]
    assert names == ['ค่าเช่า']                       # control: the other one IS there
    assert 'จ่ายค่าโบนัส' not in names


def test_category_with_an_operating_row_too_keeps_its_operating_part(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-20', 70.0, category='จ่ายค่าโบนัส')   # this year's
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV104', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    by_cat = {c['category_name']: c['total'] for c in s['expenses_by_category']}
    assert by_cat == {'จ่ายค่าโบนัส': pytest.approx(70.0)}
    assert s['prior_period_expenses'] == pytest.approx(400.0)


# ── 3. prior_period_lines — individual rows, txn_date then amount DESC ──────

def test_prior_period_lines_content_and_ordering(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-09', 200.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68 ก', belongs_to_period='2025')
    _mk_expense(conn, a, '2026-03-09', 900.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68 ข', belongs_to_period='2025')
    _mk_expense(conn, a, '2026-03-02', 50.0, category='ค่าทำบัญชี',
                description='ค่าทำบัญชี ธ.ค.', belongs_to_period='2025-12')
    _mk_expense(conn, a, '2026-03-20', 70.0, category='ค่าเช่า')   # operating: must NOT appear
    _mk_sale(conn, '2026-03-05', 'IV105', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert s['prior_period_lines'] == [
        {'txn_date': '2026-03-02', 'belongs_to_period': '2025-12',
         'category': 'ค่าทำบัญชี', 'description': 'ค่าทำบัญชี ธ.ค.', 'amount': 50.0},
        {'txn_date': '2026-03-09', 'belongs_to_period': '2025',
         'category': 'จ่ายค่าโบนัส', 'description': 'โบนัสปี 68 ข', 'amount': 900.0},
        {'txn_date': '2026-03-09', 'belongs_to_period': '2025',
         'category': 'จ่ายค่าโบนัส', 'description': 'โบนัสปี 68 ก', 'amount': 200.0},
    ]
    assert s['prior_period_expenses'] == pytest.approx(1150.0)


# ── 4. A month holding ONLY prior-period rows is covered, not no_coverage ───

def test_month_with_only_prior_period_rows_is_complete_with_zero_operating(empty_db):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV106', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert s['expense_status'] == 'complete'
    assert s['expenses'] == pytest.approx(0.0)
    assert s['prior_period_expenses'] == pytest.approx(400.0)
    assert s['net_profit'] == pytest.approx(600.0)


# ── 5. prior_period_expenses is None EXACTLY when expenses is None ─────────

def test_no_cashbook_rows_makes_both_figures_none(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2025-11-05', 'IV107', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2025-11-01', '2025-11-30')
    assert s['expenses'] is None
    assert s['prior_period_expenses'] is None
    assert s['prior_period_lines'] == []


@pytest.mark.parametrize('stamp', [None, '2025'])
def test_any_qualifying_row_makes_both_figures_not_none(empty_db, stamp):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, belongs_to_period=stamp)
    _mk_sale(conn, '2026-03-05', 'IV108', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert s['expenses'] is not None
    assert s['prior_period_expenses'] is not None


# ── 6. A prior-period row still proves that account was keyed that month ───

def test_prior_period_row_counts_as_presence_for_incomplete_months(empty_db):
    """_incomplete_months is deliberately UNCHANGED by this work: the point of
    the reading is "has anyone keyed this account yet", and a stamped row
    means they have. Its only row in the target month being a prior-period
    one must NOT flip the month to incomplete — contrast
    test_accounting_incomplete_month.py case 8, where a non-opex category in
    the same slot DOES read missing."""
    conn = _conn(empty_db)
    p = _mk_account(conn, 'P', display_name='บัญชี P')
    for ym in ('2026-03', '2026-05', '2026-07'):   # 3 of the previous 6 (Mar-Aug)
        _mk_expense(conn, p, f'{ym}-10', 100.0)
    _mk_expense(conn, p, '2026-09-10', 400.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')          # its ONLY row in the target month
    _mk_sale(conn, '2026-09-05', 'IV109', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-09-01', '2026-09-30')
    assert s['incomplete_months'] == []
    assert s['expense_status'] == 'complete'
    assert s['prior_period_expenses'] == pytest.approx(400.0)
    assert s['net_profit'] == pytest.approx(600.0)


# ── 7. The population is NOT widened: a stamped row on a transfer account
#       stays out of BOTH figures (this is why ADR 0017 is what surfaces the
#       ฿167,500 on account 904, not this change) ────────────────────────────

def test_stamped_row_on_a_transfer_account_stays_out_of_both_figures(empty_db):
    conn = _conn(empty_db)
    t = _mk_account(conn, '904', is_transfer=1)
    _mk_expense(conn, t, '2026-01-31', 167500.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')
    op = _mk_account(conn, 'OP')                       # control: a row that DOES count
    _mk_expense(conn, op, '2026-01-15', 100.0)
    _mk_sale(conn, '2026-01-05', 'IV110', net=1000.0)
    conn.commit()
    conn.close()

    s = models.get_accounting_summary('2026-01-01', '2026-01-31')
    assert s['expenses'] == pytest.approx(100.0)       # control landed
    assert s['prior_period_expenses'] == pytest.approx(0.0)
    assert s['prior_period_lines'] == []
    assert s['net_profit'] == pytest.approx(900.0)


# ── 8. The CHECK is story 11 in the schema — accept and reject matrix ──────

_ACCEPTED_PERIODS = [None, '2025', '2024', '2025-12', '2025-01', '2026-02']
_REJECTED_PERIODS = ['2026', '2026-03', '2026-04', '25', '2025-13', '2025-00',
                     '2025-1', 'ไม่แน่ใจ', '']


@pytest.mark.parametrize('value', _ACCEPTED_PERIODS)
def test_check_accepts_a_period_strictly_earlier_than_the_payment_month(empty_db, value):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-09', 1.0, belongs_to_period=value)
    conn.commit()
    conn.close()


@pytest.mark.parametrize('value', _REJECTED_PERIODS)
def test_check_rejects_a_non_period_or_a_period_not_before_the_payment_month(empty_db, value):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    with pytest.raises(sqlite3.IntegrityError):
        _mk_expense(conn, a, '2026-03-09', 1.0, belongs_to_period=value)
    conn.close()


# ── 9. Editing a row through /cashbook must not clear the stamp ────────────

def test_cashbook_edit_preserves_belongs_to_period(empty_db):
    """Passes today because txn_edit's UPDATE names its columns. It is a guard
    against the next person rewriting that statement as a column-list sweep —
    this repo has been bitten by exactly that shape (#534/#540)."""
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    txn_id = _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                         description='โบนัสปี 68', belongs_to_period='2025')
    conn.commit()
    conn.close()

    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['display_name'] = 'Test Admin'
        sess['role'] = 'admin'
    resp = c.post(f'/cashbook/txn/{txn_id}/edit', data={
        'account_id': str(a), 'txn_date': '2026-03-09', 'direction': 'expense',
        'category': 'จ่ายค่าโบนัส', 'amount': '450', 'description': 'โบนัสปี 68 (แก้ยอด)',
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]

    conn = _conn(empty_db)
    row = conn.execute(
        "SELECT amount, description, belongs_to_period FROM cashbook_transactions"
        " WHERE id=?", (txn_id,)).fetchone()
    conn.close()
    # the edit really landed (a 302 is what a REFUSAL returns here too)...
    assert row['amount'] == pytest.approx(450.0)
    assert row['description'] == 'โบนัสปี 68 (แก้ยอด)'
    # ...and it left the stamp alone
    assert row['belongs_to_period'] == '2025'


# ── 10. Render ────────────────────────────────────────────────────────────

@pytest.fixture
def admin_client_empty(empty_db):
    """Same shape as test_accounting_incomplete_month.py's fixture — a
    data-less schema-only DB (empty_db), authed as admin."""
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
    a NESTED div cannot truncate the fragment. The incomplete-month helper's
    non-greedy `.*?</div>` is only correct for an element with no inner div,
    and this card has several — a truncated fragment would make an assertion
    pass or fail for a reason that has nothing to do with the code under
    test. Returns None when the id is absent."""
    m = re.search(r'<div[^>]*id="' + re.escape(elem_id) + r'"[^>]*>', html)
    if not m:
        return None
    depth = 0
    for tok in re.finditer(r'<div\b|</div>', html[m.start():]):
        depth += 1 if tok.group(0) != '</div>' else -1
        if depth == 0:
            return html[m.start():m.start() + tok.end()]
    raise AssertionError(f'unbalanced <div> after id="{elem_id}"')


def test_render_prior_period_card_is_present_with_its_rows(empty_db, admin_client_empty):
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400000.0, category='จ่ายค่าโบนัส',
                description='โบนัสปี 68', belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV111', net=1000.0)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/accounting?date_from=2026-03-01&date_to=2026-03-31')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    elem = _extract_element(html, 'prior-period-expense')
    assert elem is not None
    assert 'ค่าใช้จ่ายของงวดก่อน' in elem
    assert '400,000.00' in elem
    assert 'โบนัสปี 68' in elem
    assert '2025' in elem
    # the operating figure is the NARROWED one, on the page itself
    assert '<div class="stat-card-label">ค่าใช้จ่ายดำเนินงาน</div>' in html


def test_render_prior_period_card_is_absent_when_there_are_none(empty_db, admin_client_empty):
    """Paired with the case above: same page, same route, no stamped row."""
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_sale(conn, '2026-03-05', 'IV112', net=1000.0)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/accounting?date_from=2026-03-01&date_to=2026-03-31')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    assert _extract_element(html, 'prior-period-expense') is None
    # control: the page really rendered its expense side
    assert '<div class="stat-card-label">ค่าใช้จ่ายดำเนินงาน</div>' in html


def test_page_never_says_pid_duean_or_pid_banchi(empty_db, admin_client_empty):
    """Story 16 — ปิดเดือน / ปิดบัญชี name the annual statutory act
    (พ.ร.บ.การบัญชี ม.10) and must appear nowhere on this statement."""
    conn = _conn(empty_db)
    a = _mk_account(conn, 'A')
    _mk_expense(conn, a, '2026-03-10', 100.0, category='ค่าเช่า')
    _mk_expense(conn, a, '2026-03-09', 400.0, category='จ่ายค่าโบนัส',
                belongs_to_period='2025')
    _mk_sale(conn, '2026-03-05', 'IV113', net=1000.0)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/accounting?date_from=2026-03-01&date_to=2026-03-31')
    html = resp.data.decode('utf-8')
    assert 'ปิดเดือน' not in html
    assert 'ปิดบัญชี' not in html
    assert 'ค่าใช้จ่ายของงวดก่อน' in html    # control: the page under test did render
