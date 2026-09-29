"""Card F scenario DB: a versioned, fully-redirected database for the cashbook seam.

PR-1 ships `build_db`, `redirect` and `seed` (the ledger unit tests and the
oracle break-once run on this DB). `run`, `dump` and `main` join in PR-2; the
hr/commission/mirror operations in PR-3 (plan §3f).

Not a test module (no `test_` prefix): pytest does not collect it, and the PR-2
differential script imports it from a detached worktree, so it must stay
importable with nothing but the app modules on the path.
"""
import os

# The seven module snapshots `tmp_db` patches (tests/conftest.py). A module that
# did `from config import DATABASE_PATH` at import time keeps writing to its own
# copy unless it is patched too: that is how `/hr/.../pay` and
# `/commission/payout` escaped the round-2 parity run.
REDIRECTED_MODULES = ('config', 'database', 'hr', 'commission', 'cashflow',
                      'payments_alloc', 'revenue')

ADVANCE_CATEGORY = "เงินเดือน (เบิกล่วงหน้า)"
SALARY_CATEGORY = "เงินเดือน"
COMMISSION_CATEGORY = "จ่ายค่าคอมมิชชั่น"
PAYOUT_CATEGORY = "ยอดขายของ"
MANUAL_CATEGORY = "ค่าน้ำมัน"


def _fail(msg):
    import pytest
    pytest.fail(msg)


def build_db(path):
    """`database.init_db(path)` on a NEW file: builds from the versioned
    `data/schema.sql`, inserts `admin`, and backfills every migration as applied,
    so the DB sits at this tree's own migration level by construction. Fails,
    never skips, when that did not happen."""
    import database
    if os.path.exists(path):
        _fail(f"build_db wants a new file, {path} exists")
    if not os.path.exists(database.SCHEMA_SQL_PATH):
        _fail(f"data/schema.sql is missing ({database.SCHEMA_SQL_PATH})")
    database.init_db(path)
    conn = database._connect(path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM applied_migrations").fetchone()[0]
    finally:
        conn.close()
    if not n:
        _fail("build_db: applied_migrations is empty, the bootstrap path did not run")
    return path


def redirect(setter, path):
    """Point every module snapshot of DATABASE_PATH at `path`. `setter(module,
    name, value)` is `monkeypatch.setattr` in tests, plain `setattr` in a script."""
    import importlib
    for name in REDIRECTED_MODULES:
        setter(importlib.import_module(name), 'DATABASE_PATH', path)


def seed(conn):
    """Explicit rows (schema.sql carries no cashbook seed data). Commits.
    Returns the ids the tests need.

    - accounts `392`, `LEX`, `SPX` (active, non-transfer)
    - the four system categories + one manual category
    - 2 employees: E1 with a nickname, E2 with nickname '' (the COALESCE case)
    - 1 finalized run R (2026-08) with 2 items, net_pay 15000 and 12000
    - 1 advance for E1 deducted in R, with its linked ADVANCE_CATEGORY row
    - 1 salesperson with real_name
    - 2 shopee marketplace_payouts sharing (deposit_date, amount)
    """
    ids = {}
    for code in ('392', 'LEX', 'SPX'):
        ids[f'acct_{code}'] = conn.execute(
            "INSERT INTO cashbook_accounts (code, display_name) VALUES (?, ?)",
            (code, f'บัญชี {code}')).lastrowid
    for name, direction in ((ADVANCE_CATEGORY, 'expense'), (SALARY_CATEGORY, 'expense'),
                            (COMMISSION_CATEGORY, 'expense'), (PAYOUT_CATEGORY, 'income'),
                            (MANUAL_CATEGORY, 'expense')):
        conn.execute("INSERT INTO cashbook_categories (name, direction, source) "
                     "VALUES (?, ?, 'setup')", (name, direction))
    ids['emp1'] = conn.execute(
        "INSERT INTO employees (emp_code, full_name, nickname) VALUES (?, ?, ?)",
        ('E1', 'สมชาย ใจดี', 'ชาย')).lastrowid
    ids['emp2'] = conn.execute(
        "INSERT INTO employees (emp_code, full_name, nickname) VALUES (?, ?, ?)",
        ('E2', 'สมหญิง รักงาน', '')).lastrowid
    ids['run'] = conn.execute(
        "INSERT INTO payroll_runs (year_month, status, finalized_at) "
        "VALUES ('2026-08', 'finalized', '2026-08-31 18:00:00')").lastrowid
    ids['item1'] = conn.execute(
        "INSERT INTO payroll_items (run_id, employee_id, salary_rate, base_amount, gross, net_pay) "
        "VALUES (?, ?, 15000, 15000, 15000, 15000)", (ids['run'], ids['emp1'])).lastrowid
    ids['item2'] = conn.execute(
        "INSERT INTO payroll_items (run_id, employee_id, salary_rate, base_amount, gross, net_pay) "
        "VALUES (?, ?, 12000, 12000, 12000, 12000)", (ids['run'], ids['emp2'])).lastrowid
    ids['advance'] = conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, from_account_id, "
        "deducted_in_run_id) VALUES (?, '2026-08-10', 3000.0, ?, ?)",
        (ids['emp1'], ids['acct_392'], ids['run'])).lastrowid
    ids['advance_txn'] = conn.execute(
        "INSERT INTO cashbook_transactions (account_id, txn_date, direction, category, "
        "user_category, amount, created_by, salary_advance_id) "
        "VALUES (?, '2026-08-10', 'expense', ?, 'ชาย', 3000.0, 'admin', ?)",
        (ids['acct_392'], ADVANCE_CATEGORY, ids['advance'])).lastrowid
    conn.execute("INSERT INTO salespersons (code, name, real_name) "
                 "VALUES ('06', 'TOU /06', 'เจียรนัย')")
    ids['sp'] = '06'
    for n_orders in (3, 4):
        conn.execute("INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders) "
                     "VALUES ('shopee', '2026-08-15', 1500.0, ?)", (n_orders,))
    conn.commit()
    return ids
