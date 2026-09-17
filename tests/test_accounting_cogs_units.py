"""TDD — COGS converts the bill's unit to the product's base unit.

`sales_transactions.qty` is denominated in the unit written on the BILL
(โหล, กล่อง, ซอง), while `products.cost_price` is per `products.unit_type`.
A COGS that multiplies raw `qty` by `cost_price` therefore costs one dozen as
one piece. The stock ledger already converts: prod row IV6901440-1 sold
16 โหล of pid 134 and `transactions` id 358869 moved **-192 ตัว**, while
`/accounting` costed the same line at 16 × ฿12. Measured understatement on
prod, Jan–Aug 2026: ฿189,535 (Aug alone ฿13,678 on 66 lines).

Conversion follows `price_lookup._bill_ratio`, the resolver's own bill-unit
lookup:
  * the row's own `unit_conversions` ratio;
  * 1.0 when the bill unit IS the base unit — a short-circuit that wins over
    any `unit_conversions` row written for that unit.

One deliberate divergence, and it is the reason `unknown_ratio_lines` exists:
`_bill_ratio` returns None for an unratioed unit and the price resolver SKIPS
that row. Dropping the line from COGS would understate it further, so COGS
costs it at ratio 1 (what the page did for every line before this change) and
counts it, the same way `no_cost_lines` is counted and still summed at 0.

Fixture: `empty_db_conn` (full live schema, zero rows) — never assert against
the live DB, it drifts day to day.
"""
import sqlite3

import pytest

import models


# ── seed helpers ─────────────────────────────────────────────────────────────

def _mk_product(conn, cost_price, unit_type='ตัว', brand_id=None):
    cur = conn.execute(
        "INSERT INTO products (product_name, unit_type, cost_price, brand_id) "
        "VALUES ('t', ?, ?, ?)",
        (unit_type, cost_price, brand_id))
    return cur.lastrowid


def _mk_ratio(conn, product_id, bsn_unit, ratio):
    conn.execute(
        "INSERT INTO unit_conversions (product_id, bsn_unit, ratio) VALUES (?, ?, ?)",
        (product_id, bsn_unit, ratio))


def _mk_sale(conn, date_iso, doc_no, product_id, qty, unit, net):
    conn.execute(
        """INSERT INTO sales_transactions
             (date_iso, doc_no, doc_base, product_id, qty, unit, unit_price, net, total)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (date_iso, doc_no, doc_no, product_id, qty, unit, net / qty, net, net))


def _mk_expense(conn, txn_date, amount, category='ค่าเช่า'):
    cur = conn.execute(
        "INSERT OR IGNORE INTO cashbook_accounts (code, is_active, is_transfer) "
        "VALUES ('OP', 1, 0)")
    acct = cur.lastrowid or conn.execute(
        "SELECT id FROM cashbook_accounts WHERE code = 'OP'").fetchone()['id']
    conn.execute(
        """INSERT INTO cashbook_transactions
             (account_id, txn_date, direction, category, amount)
           VALUES (?, ?, 'expense', ?, ?)""",
        (acct, txn_date, category, amount))


def _summary(conn):
    conn.commit()
    return models.get_accounting_summary('2026-08-01', '2026-08-31')


# ── the bug ──────────────────────────────────────────────────────────────────

def test_dozen_line_costs_twelve_pieces(empty_db_conn):
    """2 โหล of a ฿10/ตัว product costs ฿240, not ฿20."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0, unit_type='ตัว')
    _mk_ratio(conn, pid, 'โหล', 12.0)
    _mk_sale(conn, '2026-08-05', 'IV1-1', pid, qty=2, unit='โหล', net=500.0)

    s = _summary(conn)

    assert s['sales_net'] == pytest.approx(500.0)      # control: the row is in scope
    assert s['cogs'] == pytest.approx(240.0)
    assert s['gross_profit'] == pytest.approx(260.0)
    assert s['unknown_ratio_lines'] == 0


def test_net_profit_carries_the_conversion(empty_db_conn):
    """The page's bottom line moves with it — this is the number Put reads."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0, unit_type='ตัว')
    _mk_ratio(conn, pid, 'โหล', 12.0)
    _mk_sale(conn, '2026-08-05', 'IV1-1', pid, qty=2, unit='โหล', net=500.0)
    _mk_expense(conn, '2026-08-10', 100.0)

    s = _summary(conn)

    assert s['expenses'] == pytest.approx(100.0)       # control: opex coverage exists
    assert s['net_profit'] == pytest.approx(160.0)


def test_base_unit_line_is_not_rescaled(empty_db_conn):
    """A line sold in the product's own unit keeps ratio 1 — including when a
    unit_conversions row exists for that unit (price_lookup._bill_ratio
    short-circuits before the lookup, so a rogue row must not reach COGS)."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0, unit_type='ตัว')
    _mk_ratio(conn, pid, 'ตัว', 5.0)
    _mk_sale(conn, '2026-08-05', 'IV1-1', pid, qty=3, unit='ตัว', net=90.0)

    s = _summary(conn)

    assert s['cogs'] == pytest.approx(30.0)
    assert s['unknown_ratio_lines'] == 0


def test_unit_without_a_ratio_is_costed_at_one_and_counted(empty_db_conn):
    """No unit_conversions row → cost at 1 (unchanged), but say so."""
    conn = empty_db_conn
    pid = _mk_product(conn, cost_price=10.0, unit_type='อัน')
    _mk_sale(conn, '2026-08-05', 'IV1-1', pid, qty=4, unit='ช3', net=200.0)

    s = _summary(conn)

    assert s['cogs'] == pytest.approx(40.0)
    assert s['unknown_ratio_lines'] == 1


def test_brand_breakdown_cogs_is_converted(empty_db_conn):
    """The per-brand margins on the same page read from a second query."""
    conn = empty_db_conn
    cur = conn.execute(
        "INSERT INTO brands (code, name, name_th, is_own_brand, sort_order) "
        "VALUES ('SD', 'Sendai', 'เซ็นได', 1, 30)")
    brand_id = cur.lastrowid
    pid = _mk_product(conn, cost_price=10.0, unit_type='ตัว', brand_id=brand_id)
    _mk_ratio(conn, pid, 'โหล', 12.0)
    _mk_sale(conn, '2026-08-05', 'IV1-1', pid, qty=2, unit='โหล', net=500.0)

    s = _summary(conn)

    rows = [r for r in s['brand_breakdown'] if r['brand_label'] == 'เซ็นได']
    assert len(rows) == 1                              # control: the brand row rendered
    assert rows[0]['cogs_approx'] == pytest.approx(240.0)
    assert rows[0]['gross_profit'] == pytest.approx(260.0)


def test_unmapped_and_zero_cost_counts_still_work(empty_db_conn):
    """Conversion must not disturb the counters the page already discloses.

    `products.cost_price` is NOT NULL, so `no_cost_lines` only ever comes from
    a line with no product at all — and such a line is not an unratioed line,
    it is an unmapped one, already disclosed under its own name.
    """
    conn = empty_db_conn
    zero_cost = _mk_product(conn, cost_price=0.0, unit_type='ตัว')
    _mk_sale(conn, '2026-08-05', 'IV1-1', None, qty=1, unit='โหล', net=100.0)
    _mk_sale(conn, '2026-08-05', 'IV1-2', zero_cost, qty=1, unit='ตัว', net=50.0)

    s = _summary(conn)

    assert s['sales_net'] == pytest.approx(150.0)      # control: both rows in scope
    assert s['cogs'] == pytest.approx(0.0)
    assert s['no_cost_lines'] == 1
    assert s['zero_cost_lines'] == 1
    assert s['unknown_ratio_lines'] == 0
