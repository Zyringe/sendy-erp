"""TDD — financial-health pace panel money math (`models/financial_health.py`).

See projects/financial-health-page/design.md for the full design (locked
decisions Q11, S1, S2). v1 = break-even pace check only, NOT a P&L.

Fixture: `empty_db_conn` (full live schema, zero rows) — every test inserts
its own synthetic, deterministic rows. Never assert against the live DB (it
drifts day to day).

Money-math conventions pinned by these tests:
  - margin = trailing-3-complete-months (Σnet − Σqty*cost_price) / Σnet
  - salary_floor/full = latest `employee_salary_history` row (as of the given
    date) per active on_payroll employee, floor EXCLUDES OWNER_EMP_CODES
  - overhead = MEDIAN of trailing-3-complete-months non-salary cashbook opex
  - break_even_* = fixed_base_* / margin — guarded against ZeroDivisionError
"""
from datetime import date

import pytest


# ── seed helpers ─────────────────────────────────────────────────────────────

def _mk_employee(conn, emp_code, full_name, monthly_salary,
                  effective_date='2026-01-01', is_active=1, on_payroll=1):
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, is_active, on_payroll)
           VALUES (?, ?, ?, ?)""",
        (emp_code, full_name, is_active, on_payroll),
    )
    eid = cur.lastrowid
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, ?, ?, 'initial')""",
        (eid, effective_date, monthly_salary),
    )
    return eid


def _mk_sale(conn, date_iso, net, cost_price, qty=1):
    """One product + one sales_transactions line so qty*cost_price is exact."""
    cur = conn.execute(
        "INSERT INTO products (product_name, cost_price) VALUES ('t', ?)",
        (cost_price,))
    pid = cur.lastrowid
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, product_id, qty, unit_price, net, total)
           VALUES (?, 'IV1', ?, ?, ?, ?, ?)""",
        (date_iso, pid, qty, net / qty, net, net))


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


# ── break-even: the worked example from the design/task spec ────────────────

def test_get_break_even_worked_example(empty_db_conn):
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)

    # 2 employees: EMP001 = owner (excluded from floor), EMP999 = staff.
    _mk_employee(conn, 'EMP001', 'Owner', 30000.0)
    _mk_employee(conn, 'EMP999', 'Staff', 20000.0)

    # 3 complete trailing months (Apr/May/Jun 2026) → Σnet=100000, Σcost=55000
    # → margin exactly 0.45.
    _mk_sale(conn, '2026-04-15', net=40000.0, cost_price=22000.0)
    _mk_sale(conn, '2026-05-15', net=30000.0, cost_price=16500.0)
    _mk_sale(conn, '2026-06-15', net=30000.0, cost_price=16500.0)

    # Non-salary overhead, one account, 3 trailing months → median(10000,
    # 30000, 20000) == 20000.
    acct = _mk_account(conn)
    _mk_expense(conn, acct, '2026-04-20', 10000.0)
    _mk_expense(conn, acct, '2026-05-20', 30000.0)
    _mk_expense(conn, acct, '2026-06-20', 20000.0)
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)

    assert result['margin'] == pytest.approx(0.45)
    assert result['salary_floor'] == pytest.approx(20000.0)
    assert result['salary_full'] == pytest.approx(50000.0)
    assert result['overhead'] == pytest.approx(20000.0)
    assert result['fixed_base_floor'] == pytest.approx(40000.0)
    assert result['fixed_base_full'] == pytest.approx(70000.0)
    assert result['break_even_floor'] == pytest.approx(40000.0 / 0.45)
    assert result['break_even_full'] == pytest.approx(70000.0 / 0.45)
    assert len(result['trailing_months']) == 3
    assert [m['month'] for m in result['trailing_months']] == [4, 5, 6]
    assert result['trailing_months'][0]['revenue'] == pytest.approx(40000.0)


def test_break_even_excludes_inactive_and_off_payroll(empty_db_conn):
    """Salary sums only count is_active=1 AND on_payroll=1 employees."""
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)

    _mk_employee(conn, 'EMP999', 'Staff', 20000.0)
    _mk_employee(conn, 'EMP998', 'Inactive', 99999.0, is_active=0)
    _mk_employee(conn, 'EMP997', 'OffPayroll', 88888.0, on_payroll=0)
    _mk_sale(conn, '2026-04-15', net=40000.0, cost_price=22000.0)
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)
    assert result['salary_floor'] == pytest.approx(20000.0)
    assert result['salary_full'] == pytest.approx(20000.0)


def test_break_even_uses_latest_salary_as_of_date(empty_db_conn):
    """A raise effective AFTER as_of must not be picked up yet."""
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)

    eid = _mk_employee(conn, 'EMP999', 'Staff', 20000.0, effective_date='2026-01-01')
    conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2026-08-01', 99999.0, 'raise')""",
        (eid,))
    _mk_sale(conn, '2026-04-15', net=40000.0, cost_price=22000.0)
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)
    assert result['salary_full'] == pytest.approx(20000.0)


def test_break_even_no_sales_returns_none_not_zerodivisionerror(empty_db_conn):
    """No trailing revenue at all → margin/break_even None, no crash."""
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)
    _mk_employee(conn, 'EMP999', 'Staff', 20000.0)
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)
    assert result['margin'] is None
    assert result['break_even_floor'] is None
    assert result['break_even_full'] is None
    # fixed costs still computable even with no revenue
    assert result['salary_floor'] == pytest.approx(20000.0)


def test_break_even_zero_margin_returns_none_break_even(empty_db_conn):
    """Revenue exists but margin == 0 (net == cost) → break_even None, not inf."""
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)
    _mk_employee(conn, 'EMP999', 'Staff', 20000.0)
    _mk_sale(conn, '2026-04-15', net=10000.0, cost_price=10000.0)
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)
    assert result['margin'] == pytest.approx(0.0)
    assert result['break_even_floor'] is None
    assert result['break_even_full'] is None


# ── current-month pace ────────────────────────────────────────────────────────

def test_get_current_month_pace(empty_db_conn):
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)

    # Two rows before as_of (count toward MTD), one after (must NOT count
    # toward mtd_revenue but DOES count toward data_as_of freshness).
    _mk_sale(conn, '2026-07-05', net=2000.0, cost_price=1000.0)
    _mk_sale(conn, '2026-07-12', net=3000.0, cost_price=1000.0)
    _mk_sale(conn, '2026-07-20', net=9000.0, cost_price=1000.0)
    conn.commit()

    result = fh.get_current_month_pace(as_of_date=as_of, conn=conn)

    assert result['mtd_revenue'] == pytest.approx(5000.0)
    assert result['data_as_of'] == '2026-07-20'
    assert result['day_of_month'] == 15
    assert result['days_in_month'] == 31
    assert result['month_label'] == 'กรกฎาคม 2569'


def test_get_current_month_pace_no_data(empty_db_conn):
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)
    conn.commit()

    result = fh.get_current_month_pace(as_of_date=as_of, conn=conn)
    assert result['mtd_revenue'] == pytest.approx(0.0)
    assert result['data_as_of'] is None


# ── trailing months ────────────────────────────────────────────────────────────

def test_get_trailing_months(empty_db_conn):
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)
    _mk_sale(conn, '2026-04-15', net=172692.0, cost_price=0.0)
    _mk_sale(conn, '2026-05-15', net=295640.0, cost_price=0.0)
    _mk_sale(conn, '2026-06-15', net=255182.0, cost_price=0.0)
    # July itself must NOT show up as a "complete" trailing month.
    _mk_sale(conn, '2026-07-05', net=999999.0, cost_price=0.0)
    conn.commit()

    months = fh.get_trailing_months(n=3, conn=conn, as_of_date=as_of)
    assert [m['month'] for m in months] == [4, 5, 6]
    assert months[0]['revenue'] == pytest.approx(172692.0)
    assert months[1]['revenue'] == pytest.approx(295640.0)
    assert months[2]['revenue'] == pytest.approx(255182.0)
    assert months[0]['month_label'] == 'เม.ย.'


def test_overhead_excludes_salary_transfer_and_cogs_categories(empty_db_conn):
    """Overhead must ignore เงินทุน/เงินโอน, ซื้อสินค้า, เงินเดือน AND transfer accounts.

    The SAME poison set (excluded categories + a transfer-account row) is
    injected into ALL 3 trailing months with a large, identical amount. If any
    of it leaked into the sum, the median would shift by that leaked amount in
    every month (median is monotonic under an equal per-month shift) — so a
    single-month poison placement (which a median can hide) would NOT catch
    this, but an equal-every-month placement does.
    """
    import models.financial_health as fh

    conn = empty_db_conn
    as_of = date(2026, 7, 15)
    _mk_employee(conn, 'EMP999', 'Staff', 20000.0)
    _mk_sale(conn, '2026-04-15', net=40000.0, cost_price=22000.0)

    op_acct = _mk_account(conn, code='OP')
    tr_acct_id = conn.execute(
        "INSERT INTO cashbook_accounts (code, is_active, is_transfer) VALUES ('TR', 1, 1)"
    ).lastrowid

    legit = {'2026-04-10': 10000.0, '2026-05-10': 30000.0, '2026-06-10': 20000.0}
    for d, amt in legit.items():
        _mk_expense(conn, op_acct, d, amt, category='ค่าเช่า')
        # Poison: excluded categories on a normal account.
        _mk_expense(conn, op_acct, d, 999999.0, category='เงินทุน/เงินโอน')
        _mk_expense(conn, op_acct, d, 999999.0, category='ซื้อสินค้า')
        _mk_expense(conn, op_acct, d, 999999.0, category='เงินเดือน')
        # Poison: transfer ACCOUNT with an otherwise-normal category.
        _mk_expense(conn, tr_acct_id, d, 999999.0, category='ค่าเช่า')
    conn.commit()

    result = fh.get_break_even(conn=conn, as_of_date=as_of)
    # median(10000, 30000, 20000) == 20000 — unaffected iff every poison
    # category/account above was correctly excluded from all 3 months.
    assert result['overhead'] == pytest.approx(20000.0)
