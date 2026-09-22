"""TDD — `/financial-health` carries the งบกองรวม label in EVERY page state
(#635).

CONTEXT.md -> "Internal P&L" -> งบกองรวม (pooled statement): the break-even
floor's revenue, margin and salary all come from `sales_transactions` /
`employee_salary_history` (BSN only), but its overhead (`_trailing_overhead`)
is the median of `cashbook_transactions`, which has no company column and
pools BSN+SD (same shared pool ADR 0016 describes for `/accounting`). The
floor therefore sits above BSN's own break-even, and a below-floor reading
overstates the shortfall. `/accounting` got this label in #633 (#593 stories
13, 14); this page had the same shape and no label.

`/financial-health` has no date_from/date_to query params — `as_of_date`
defaults to `date.today()` inside `models.get_break_even()` /
`get_current_month_pace()` — so every fixture below seeds relative to the
REAL current date via `models.financial_health._trailing_month_starts`,
the same helper the route itself uses to pick its 3 trailing months.

Fixture: `empty_db` (schema-only clone, zero rows) — never `tmp_db`, whose
live-DB copy would leak real cashbook/salary/sales rows into the trailing
window and change the floor out from under the test. Helpers are copied
locally (repo convention for this test seam, e.g.
test_accounting_statement_labels.py), not imported from sibling test files.
"""
import os
import re
import sqlite3
from datetime import date

import pytest

import models.financial_health as fh


AS_OF = date.today()
TRAILING = fh._trailing_month_starts(AS_OF, 3)  # [(y, m), ...] oldest first, same as the route

_FORBIDDEN = ['ปิดเดือน', 'ปิดบัญชี', 'งบการเงิน', 'งบรวม', 'งบ BSN']

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'inventory_app', 'templates', 'financial_health.html',
)


# ── seed helpers (same shape as test_financial_health.py) ───────────────────

def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _mk_employee(conn, emp_code, monthly_salary, effective_date='2020-01-01'):
    cur = conn.execute(
        "INSERT INTO employees (emp_code, full_name, is_active, on_payroll) "
        "VALUES (?, ?, 1, 1)", (emp_code, emp_code))
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, ?, ?, 'initial')""",
        (eid, effective_date, monthly_salary))
    return eid


def _mk_sale(conn, date_iso, net, cost_price, doc_no):
    cur = conn.execute(
        "INSERT INTO products (product_name, cost_price) VALUES ('t', ?)",
        (cost_price,))
    pid = cur.lastrowid
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, doc_base, product_id, qty, unit_price, net, total)
           VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
        (date_iso, doc_no, doc_no, pid, net, net, net))


def _mk_account(conn, code='OP'):
    cur = conn.execute(
        "INSERT INTO cashbook_accounts (code, is_active, is_transfer) VALUES (?, 1, 0)",
        (code,))
    return cur.lastrowid


def _mk_expense(conn, account_id, txn_date, amount, category='ค่าเช่า'):
    conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount)
           VALUES (?, ?, 'expense', ?, ?)""",
        (account_id, txn_date, category, amount))


@pytest.fixture
def admin_client_empty(empty_db):
    """Same shape as test_accounting_statement_labels.py's fixture — a
    data-less schema-only DB (empty_db), authed as admin (financial_health
    gates admin/manager/shareholder)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id'] = 1
        sess['username'] = 'test-admin'
        sess['role'] = 'admin'
    return c


def _extract_div(html, elem_id):
    """Pull the id="<elem_id>" DIV out by itself, counting <div> depth so a
    NESTED div cannot truncate the fragment. Returns None when absent.
    Copied from test_accounting_statement_labels.py."""
    m = re.search(r'<div[^>]*id="' + re.escape(elem_id) + r'"[^>]*>', html)
    if not m:
        return None
    depth = 0
    for tok in re.finditer(r'<div\b|</div>', html[m.start():]):
        depth += 1 if tok.group(0) != '</div>' else -1
        if depth == 0:
            return html[m.start():m.start() + tok.end()]
    raise AssertionError(f'unbalanced <div> after id="{elem_id}"')


def _extract_span(html, elem_id):
    m = re.search(
        r'<span[^>]*id="' + re.escape(elem_id) + r'"[^>]*>(.*?)</span>',
        html, re.S)
    return m.group(1) if m else None


def _floor_value(html):
    """The rendered value inside the FLOOR kpi card — digits when computed,
    '—' when margin is None."""
    m = re.search(
        r'ต้องขายให้ถึง \(ขั้นต่ำ ไม่รวมเจ้าของ\)</div>\s*'
        r'<div class="stat-card-value[^"]*"[^>]*>\s*(.*?)\s*</div>',
        html, re.S)
    assert m is not None
    return m.group(1)


# ── per-state seeding + control assertion ───────────────────────────────────
# Each seed function inserts one non-owner employee (salary_floor=20000) and
# one 3-month overhead run (10000/30000/20000 -> median 20000), so
# fixed_base_floor is 40000 in every state that reaches it. Each control
# asserts the ONE thing only its intended state can emit, so a wrong branch
# cannot satisfy it by accident.

def _seed_overhead_and_salary(conn):
    _mk_employee(conn, 'EMP999', 20000.0)
    acct = _mk_account(conn)
    for (y, m), amt in zip(TRAILING, (10000.0, 30000.0, 20000.0)):
        _mk_expense(conn, acct, f'{y:04d}-{m:02d}-10', amt)


def _seed_below_floor(conn):
    """margin 0.5 (cost = net/2), every trailing month's revenue (30000) sits
    BELOW the resulting floor (40000/0.5 = 80000) -> all_below_floor True."""
    _seed_overhead_and_salary(conn)
    for i, (y, m) in enumerate(TRAILING):
        net = 30000.0
        _mk_sale(conn, f'{y:04d}-{m:02d}-15', net=net, cost_price=net * 0.5,
                 doc_no=f'IVB{i}')


def _control_below_floor(html):
    assert re.search(r'\d', _floor_value(html)) is not None, \
        'floor did not render a number'
    assert 'ทุกเดือน &lt; floor' in html


def _seed_above_floor(conn):
    """Same margin (0.5) and same floor (80000), but the THIRD trailing
    month's revenue (90000) sits ABOVE it -> not every month is below ->
    all_below_floor False."""
    _seed_overhead_and_salary(conn)
    for i, ((y, m), net) in enumerate(zip(TRAILING, (30000.0, 30000.0, 90000.0))):
        _mk_sale(conn, f'{y:04d}-{m:02d}-15', net=net, cost_price=net * 0.5,
                 doc_no=f'IVA{i}')


def _control_above_floor(html):
    assert re.search(r'\d', _floor_value(html)) is not None, \
        'floor did not render a number'
    assert 'ทุกเดือน &lt; floor' not in html


def _seed_margin_missing(conn):
    """No sales at all in the trailing window -> margin None -> floor None.
    Salary/overhead still get seeded — those two labels must STILL render
    unconditionally (issue item 3)."""
    _seed_overhead_and_salary(conn)


def _control_margin_missing(html):
    assert re.search(r'\d', _floor_value(html)) is None, \
        f'floor rendered a number with no trailing revenue: {_floor_value(html)!r}'
    assert 'ทุกเดือน &lt; floor' not in html


_STATES = {
    'below_floor': (_seed_below_floor, _control_below_floor),
    'above_floor': (_seed_above_floor, _control_above_floor),
    'margin_missing': (_seed_margin_missing, _control_margin_missing),
}


# ── 1. Every page state carries the pooled label and never the forbidden
#      words ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('state', sorted(_STATES))
def test_every_page_state_carries_the_pooled_label(empty_db, admin_client_empty, state):
    seed, control = _STATES[state]
    conn = _conn(empty_db)
    seed(conn)
    conn.commit()
    conn.close()

    resp = admin_client_empty.get('/financial-health')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')

    # the state's own control FIRST -- proves this fixture actually reached
    # the branch it claims to, before anything else is asserted about it
    control(html)

    floor_note = _extract_div(html, 'floor-overhead-note')
    assert floor_note is not None, f'id="floor-overhead-note" absent in state={state}'
    assert 'สมุดรับ-จ่าย BSN+SD ปนกัน' in floor_note

    full_note = _extract_div(html, 'full-overhead-note')
    assert full_note is not None, f'id="full-overhead-note" absent in state={state}'
    assert 'สมุดรับ-จ่าย BSN+SD ปนกัน' in full_note

    table_note = _extract_span(html, 'table-overhead-note')
    assert table_note is not None, f'id="table-overhead-note" absent in state={state}'
    assert 'BSN+SD ปนกัน' in table_note

    statement = _extract_div(html, 'pooled-floor-statement')
    assert statement is not None, f'id="pooled-floor-statement" absent in state={state}'
    assert 'งบกองรวม' in statement
    assert 'BSN กับ SD' in statement, \
        f'pooled-explanation sentence (which inputs are pooled) missing in state={state}'
    assert 'ยอดที่ขาดจริงของ BSN จะน้อยกว่าที่เห็น' in statement, \
        f'pooled-explanation sentence (which way the floor is biased) missing in state={state}'

    for word in _FORBIDDEN:
        assert word not in html, f'{word!r} found on the page in state={state}'


# ── 2. Source guard over the template, so a forbidden word inside a branch
#      no fixture above renders still gets caught ───────────────────────────

def test_source_has_no_forbidden_word_outside_a_jinja_comment():
    with open(_TEMPLATE_PATH, encoding='utf-8') as f:
        src = f.read()
    stripped = re.sub(r'\{#.*?#\}', '', src, flags=re.S)

    # CONTROL, asserted first: the strip did not eat the template. A strip
    # helper that returned '' would otherwise make every assertion below
    # vacuously pass.
    for marker in ('ยังคำนวณจุดคุ้มทุนไม่ได้', 'โครงสร้างต้นทุนคงที่', 'งบกองรวม'):
        assert marker in stripped, \
            f'control marker {marker!r} missing from stripped source -- strip ate the template'

    for word in _FORBIDDEN:
        assert word not in stripped, f'{word!r} found outside any Jinja comment'
