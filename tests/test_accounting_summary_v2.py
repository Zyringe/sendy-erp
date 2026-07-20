"""TDD — `/accounting` P&L v2 money math (`models/accounting.py::get_accounting_summary`).

See `projects/financial-health-page/design.md` ("V2 — /accounting P&L honesty")
for the full spec. This locks down:
  1. Revenue nets out SR (return) rows at the REPORTING layer only — SR rows
     stay stored POSITIVE in sales_transactions (they sync to stock as an IN;
     do NOT touch storage — erp-engineering-discipline.md).
  2. Expenses = cashbook opex (direction='expense', non-transfer account,
     category NOT IN ('เงินทุน/เงินโอน','ซื้อสินค้า')) — replaces the dead
     expense_log (always 0 rows).
  3. Commission is NOT subtracted a second time — cashbook opex already
     includes the จ่ายค่าคอมมิชชั่น category (design.md Q5). commission_total
     (commission_payouts) is dropped from the P&L entirely.
  4. Coverage guard: a period with ZERO cashbook opex ROWS (not sum) →
     expenses/net_profit = None, revenue/COGS/gross-profit still computed.

Fixture: `empty_db` (full live schema, zero rows, file path — NOT a live
connection) — every test opens its own throwaway connection to insert
synthetic rows, commits, closes, then calls `models.get_accounting_summary()`
plain (no conn arg) exactly like the existing
`test_default_period_smoke.py::test_get_accounting_summary_date_from_only`
does. Never assert against the live DB.
"""
import sqlite3

import pytest

import models


# ── seed helpers ─────────────────────────────────────────────────────────────

def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _mk_brand(conn, code, name_th, is_own_brand=0, sort_order=100):
    cur = conn.execute(
        "INSERT INTO brands (code, name, name_th, is_own_brand, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        (code, name_th, name_th, is_own_brand, sort_order))
    return cur.lastrowid


def _mk_product(conn, cost_price=0.0, brand_id=None):
    cur = conn.execute(
        "INSERT INTO products (product_name, cost_price, brand_id) VALUES ('t', ?, ?)",
        (cost_price, brand_id))
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


def _mk_account(conn, code, is_transfer=0):
    cur = conn.execute(
        "INSERT INTO cashbook_accounts (code, is_active, is_transfer) VALUES (?, 1, ?)",
        (code, is_transfer))
    return cur.lastrowid


def _mk_expense(conn, account_id, txn_date, amount, category, direction='expense'):
    conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount)
           VALUES (?, ?, ?, ?, ?)""",
        (account_id, txn_date, direction, category, amount))


def _mk_salesperson(conn, code='00'):
    conn.execute("INSERT OR IGNORE INTO salespersons (code, name) VALUES (?, ?)", (code, code))


def _mk_commission_payout(conn, year_month, amount_paid, salesperson_code='00'):
    _mk_salesperson(conn, salesperson_code)
    conn.execute(
        """INSERT INTO commission_payouts
             (year_month, salesperson_code, amount_paid, paid_date)
           VALUES (?, ?, ?, ?)""",
        (year_month, salesperson_code, amount_paid, year_month + '-01'))


# ── 1. Revenue nets out SR (return) rows ────────────────────────────────────

def test_revenue_nets_out_sr_rows(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-10', 'IV001', net=1000.0)
    _mk_sale(conn, '2026-06-15', 'SR001', net=200.0)  # stored POSITIVE on purpose
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert summary['sales_net'] == pytest.approx(800.0)


def test_brand_breakdown_also_nets_out_sr_rows(empty_db):
    conn = _conn(empty_db)
    brand_id = _mk_brand(conn, 'GL', 'สิงห์ทอง', is_own_brand=1)
    pid = _mk_product(conn, cost_price=0.0, brand_id=brand_id)
    _mk_sale(conn, '2026-06-10', 'IV002', net=1000.0, product_id=pid)
    _mk_sale(conn, '2026-06-15', 'SR002', net=200.0, product_id=pid)
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    rows = {r['brand_label']: r for r in summary['brand_breakdown']}
    assert rows['สิงห์ทอง']['sales_net'] == pytest.approx(800.0)


# ── 2. Expenses from cashbook opex ──────────────────────────────────────────

def test_expenses_include_commission_exclude_purchases_and_transfers(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-05', 'IV010', net=5000.0)  # so the period has revenue too

    op = _mk_account(conn, 'OP', is_transfer=0)
    tr = _mk_account(conn, 'TR', is_transfer=1)

    _mk_expense(conn, op, '2026-06-10', 500.0, 'จ่ายค่าคอมมิชชั่น')   # included
    _mk_expense(conn, op, '2026-06-11', 300.0, 'ค่าเช่า')             # included
    _mk_expense(conn, op, '2026-06-12', 999.0, 'ซื้อสินค้า')          # excluded (COGS)
    _mk_expense(conn, op, '2026-06-13', 999.0, 'เงินทุน/เงินโอน')     # excluded (transfer cat)
    _mk_expense(conn, tr, '2026-06-14', 999.0, 'ค่าเช่า')             # excluded (transfer acct)
    _mk_expense(conn, op, '2026-06-15', 999.0, 'ค่าเช่า', direction='income')  # excluded (income)
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert summary['expenses'] == pytest.approx(800.0)

    by_cat = {r['category_name']: r['total'] for r in summary['expenses_by_category']}
    assert by_cat.get('จ่ายค่าคอมมิชชั่น') == pytest.approx(500.0)
    assert by_cat.get('ค่าเช่า') == pytest.approx(300.0)
    assert 'ซื้อสินค้า' not in by_cat
    assert 'เงินทุน/เงินโอน' not in by_cat


# ── 3. Commission double-count guard ────────────────────────────────────────

def test_net_profit_has_no_separate_commission_subtraction(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-05', 'IV020', net=10000.0, product_id=_mk_product(conn, cost_price=4000.0))

    op = _mk_account(conn, 'OP', is_transfer=0)
    _mk_expense(conn, op, '2026-06-10', 500.0, 'จ่ายค่าคอมมิชชั่น')
    _mk_expense(conn, op, '2026-06-11', 300.0, 'ค่าเช่า')

    # A commission_payouts row for the SAME month, much bigger than the
    # cashbook commission line — if this leaked into net_profit as a SECOND
    # subtraction, net_profit would be far off gross_profit - expenses.
    _mk_commission_payout(conn, '2026-06', amount_paid=999999.0)
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert 'commission_total' not in summary

    expected_gross_profit = 10000.0 - (4000.0 * 1)  # qty=1 * cost_price
    assert summary['gross_profit'] == pytest.approx(expected_gross_profit)
    assert summary['expenses'] == pytest.approx(800.0)
    assert summary['net_profit'] == pytest.approx(expected_gross_profit - 800.0)


# ── 4. Coverage guard ────────────────────────────────────────────────────────

def test_coverage_guard_zero_cashbook_rows_nulls_expenses_and_net_profit(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2025-11-05', 'IV030', net=10000.0, product_id=_mk_product(conn, cost_price=4000.0))
    # No cashbook rows at all in this period (pre-cashbook era, cashbook starts 2026-03).
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2025-11-01', '2025-11-30')
    assert summary['expenses'] is None
    assert summary['net_profit'] is None
    assert summary['expenses_by_category'] == []
    # Revenue/COGS/gross-profit must still render — valid back to 2024.
    assert summary['sales_net'] == pytest.approx(10000.0)
    assert summary['cogs'] == pytest.approx(4000.0)
    assert summary['gross_profit'] == pytest.approx(6000.0)


def test_coverage_guard_does_not_fire_when_opex_rows_exist(empty_db):
    """A period WITH at least one qualifying cashbook opex row must NOT be
    nulled, even if the only cashbook activity is a small commission line."""
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-05', 'IV040', net=1000.0)
    op = _mk_account(conn, 'OP', is_transfer=0)
    _mk_expense(conn, op, '2026-06-10', 100.0, 'จ่ายค่าคอมมิชชั่น')
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert summary['expenses'] == pytest.approx(100.0)
    assert summary['net_profit'] is not None


# ── 5. March 2026 giveaway anomaly note (date-overlap check, no hard-coded docs) ──

def test_note_march_anomaly_set_when_period_overlaps_march_2026(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-03-15', 'IV050', net=1000.0)
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-03-01', '2026-03-31')
    assert summary['note_march_anomaly'] is True


def test_note_march_anomaly_not_set_for_other_periods(empty_db):
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-15', 'IV051', net=1000.0)
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert summary['note_march_anomaly'] is False


# ── 6. expense_log / expense_categories are no longer read ──────────────────

def test_expenses_ignore_dead_expense_log_rows(empty_db):
    """A row in the dead expense_log table (if any ever exist again) must NOT
    feed into the P&L — the source of truth is now cashbook only."""
    conn = _conn(empty_db)
    _mk_sale(conn, '2026-06-05', 'IV060', net=1000.0)
    op = _mk_account(conn, 'OP', is_transfer=0)
    _mk_expense(conn, op, '2026-06-10', 100.0, 'ค่าเช่า')
    # Poison the dead table with a huge amount — if it's still read, this
    # test fails loudly instead of silently.
    company_id = conn.execute(
        "INSERT INTO companies (code, name_th) VALUES ('BSN', 'test')").lastrowid
    category_id = conn.execute(
        "INSERT INTO expense_categories (code, name_th) VALUES ('X', 'test')").lastrowid
    conn.execute(
        "INSERT INTO expense_log (company_id, category_id, date_iso, amount_pre_vat) "
        "VALUES (?, ?, '2026-06-10', 999999.0)", (company_id, category_id))
    conn.commit()
    conn.close()

    summary = models.get_accounting_summary('2026-06-01', '2026-06-30')
    assert summary['expenses'] == pytest.approx(100.0)
