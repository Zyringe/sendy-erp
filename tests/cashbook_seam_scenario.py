"""Card F scenario DB: a versioned, fully-redirected database for the cashbook seam.

PR-1 shipped `build_db`, `redirect` and `seed` (the ledger unit tests and the
oracle break-once run on this DB). PR-2 adds `run`, `dump` and `main`. `run`
already drives the hr, commission and mirror operations as well (through
today's code in both trees): break-once (a) needs the salary rows, and PR-3
then moves those writers behind the seam without touching the scenario.

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


# ── PR-2: run / dump / main (plan §3f) ───────────────────────────────────────

SCENARIO_VERSION = 1

# Rows per kind at dump time (plan §3f row arithmetic). Changes with `run`.
EXPECTED_COUNTS = {"manual": 3, "advance": 2, "salary": 1, "commission": 1, "payout": 1}
EXPECTED_TABLE_COUNTS = {"commission_payouts": 2, "salary_advances": 2}
# Explicit, actor-attributed audit_log rows per table (user NOT NULL; the
# mig-076 triggers write user NULL). cashbook_transactions: salary post x2 +
# void x1, commission record x2 + cancel x1, cashbook delete x2 (the manual row
# and the un-deducted advance), one changed edit. commission_payouts: record x3
# (mode 1, mode 2, the account_id=None backfill).
EXPECTED_ATTRIBUTED_AUDIT = {'cashbook_transactions': 9, 'commission_payouts': 3}

ADMIN_SESSION = {'user_id': 1, 'username': 'admin', 'display_name': 'Administrator',
                 'role': 'admin'}

_DUMP_TABLES = ('cashbook_transactions', 'salary_advances', 'commission_payouts')


def _row_errors(html):
    """The per-row error lines /cashbook/new renders (new.html)."""
    import re
    return [re.sub(r'<[^>]+>', '', m).strip()
            for m in re.findall(r'<td colspan="8" class="small">(.*?)</td>', html, re.S)]


def run(client, conn, ids):
    """Every operation, each with an explicit date. Returns one record per
    request: status, redirect target, every flash (message, category) and, for
    a re-rendered /cashbook/new, its per-row errors. `conn` reads ids and
    drives the mirror; the routes open their own connections."""
    import cashbook_payout_mirror as mirror
    import commission
    import database
    from flask import message_flashed
    from app import app as flask_app

    steps, flashes = [], []

    def on_flash(sender, message, category, **_):
        flashes.append([str(message), category])

    def post(name, path, data=None):
        del flashes[:]
        resp = client.post(path, data=data or {})
        rec = {'step': name, 'status': resp.status_code,
               'location': resp.headers.get('Location'), 'flashes': list(flashes)}
        if resp.status_code == 200:
            rec['row_errors'] = _row_errors(resp.get_data(as_text=True))
        steps.append(rec)
        return rec

    def last_id():
        return conn.execute("SELECT MAX(id) FROM cashbook_transactions").fetchone()[0]

    def note(name, value):
        steps.append({'step': name, 'result': value})

    a392, lex, spx = str(ids['acct_392']), str(ids['acct_LEX']), str(ids['acct_SPX'])

    def row(i, **kw):
        base = {'direction': 'expense', 'category': MANUAL_CATEGORY, 'user_category': '',
                'employee_id': '', 'amount': '', 'description': '', 'note': '', 'txn_date': ''}
        base.update(kw)
        return {f'rows-{i}-{k}': v for k, v in base.items()}

    def edit(txn, **kw):
        form = {'account_id': a392, 'txn_date': '2026-08-20', 'direction': 'expense',
                'category': MANUAL_CATEGORY, 'user_category': 'ร้านเอ', 'amount': '250',
                'description': 'เติมน้ำมัน', 'note': ''}
        form.update(kw)
        return post(f'edit {txn} {sorted(kw)}', f'/cashbook/txn/{txn}/edit', form)

    message_flashed.connect(on_flash, flask_app)
    try:
        # ── manual: single, multi-error (N2), bulk, duplicate gate ──────────
        single = {'account_id': a392, 'txn_date': '2026-08-20',
                  **row(0, user_category='ร้านเอ', amount='250', description='เติมน้ำมัน')}
        post('new single', '/cashbook/new', single)
        single_id = last_id()
        # N2: the ledger raises only the first refusal; the route validators
        # must still report EVERY error, form-level and per row, and save nothing.
        post('new multi-error', '/cashbook/new', {
            'account_id': '9999', 'txn_date': '2026-08-20', 'bulk_mode': '1',
            **row(0, amount='0', direction='out', category=''),
            **row(1, amount='5', txn_date='31/08/2569'),
            **row(2, amount='abc', category=SALARY_CATEGORY)})
        post('new bulk', '/cashbook/new', {
            'account_id': spx, 'txn_date': '2026-08-20', 'bulk_mode': '1',
            **row(0, direction='income', category=PAYOUT_CATEGORY, amount='820',
                  txn_date='2026-08-18', description='ยอดโอน Shopee คีย์มือ'),
            **row(1),
            **row(3, amount='60', user_category='สมชาย', description='ค่าน้ำมัน')})
        post('new new-category (unconfirmed)', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-08-20',
            **row(0, category='ค่าทางด่วน', amount='40')})
        post('new policy-blocked salary', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-08-20',
            **row(0, category=SALARY_CATEGORY, amount='100')})
        post('new commission filed elsewhere (unconfirmed)', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-08-20',
            **row(0, user_category='เจียรนัย', amount='400', description='ค่าคอมมิชชั่น')})
        post('new duplicate (unconfirmed)', '/cashbook/new', single)
        post('new duplicate (confirmed)', '/cashbook/new', {**single, 'confirm_duplicates': '1'})
        dup_id = last_id()

        # ── advances ────────────────────────────────────────────────────────
        post('new advance E1 (unconfirmed cap)', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-09-05',
            **row(0, category=ADVANCE_CATEGORY, employee_id=str(ids['emp1']), amount='2000')})
        post('new advance E1', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-09-05', 'confirm_advance_cap': '1',
            **row(0, category=ADVANCE_CATEGORY, employee_id=str(ids['emp1']), amount='2000')})
        post('new advance E2', '/cashbook/new', {
            'account_id': a392, 'txn_date': '2026-09-06', 'confirm_advance_cap': '1',
            **row(0, category=ADVANCE_CATEGORY, employee_id=str(ids['emp2']), amount='1500')})
        adv2_id = last_id()

        # ── salary (hr routes, ADR 0006) ────────────────────────────────────
        run_id = ids['run']
        for item, name in ((ids['item1'], 'pay item 1'), (ids['item2'], 'pay item 2'),
                           (ids['item1'], 'pay item 1 again')):
            post(name, f'/hr/payroll/{run_id}/item/{item}/pay',
                 {'account_id': a392, 'pay_date': '2026-08-31'})
        post('unpay item 2', f'/hr/payroll/{run_id}/item/{ids["item2"]}/unpay')
        salary_id = conn.execute("SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?",
                                 (ids['item1'],)).fetchone()[0]

        # ── commission (mode 1, mode 2, backfill, cancel) ───────────────────
        post('commission mode 1', '/commission/payout', {
            'month': '2026-08', 'paid_date': '2026-09-01', 'account_id': a392,
            'sp_code': ids['sp'], 'invoice_no': 'IV6901', 'amount_IV6901': '450'})
        mode1 = conn.execute("SELECT MAX(id) FROM commission_payouts").fetchone()[0]
        post('commission mode 2', '/commission/payout', {
            'month': '2026-08', 'paid_date': '2026-09-02', 'account_id': a392,
            f'amount_{ids["sp"]}': '300', 'sp_code': ids['sp']})
        commission.record_payout(year_month='2026-07', salesperson_code=ids['sp'],
                                 amount_paid=100.0, paid_date='2026-09-03',
                                 paid_by='Administrator')
        note('commission backfill (account_id=None)', 'ok')
        post('commission cancel mode 1', f'/commission/payout/{mode1}/delete')
        commission_id = conn.execute(
            "SELECT id FROM cashbook_transactions WHERE commission_payout_id IS NOT NULL"
        ).fetchone()[0]

        # ── payout mirror (insert both, drop one, describe, conflict skip) ──
        def mirror_run(name):
            c = database.get_connection()
            try:
                note(name, mirror.mirror_platform(c, 'shopee'))
            finally:
                c.close()
        conn.commit()
        mirror_run('mirror insert')
        conn.execute("DELETE FROM marketplace_payouts WHERE platform = 'shopee' AND n_orders = 3")
        conn.commit()
        mirror_run('mirror drop one + describe')
        conn.execute("INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders) "
                     "VALUES ('shopee', '2026-08-19', 820.0, 1)")
        conn.commit()
        mirror_run('mirror conflict skip')
        payout_id = conn.execute(
            "SELECT id FROM cashbook_transactions WHERE payout_platform IS NOT NULL"
        ).fetchone()[0]

        # ── edits ───────────────────────────────────────────────────────────
        edit(single_id, amount='275', txn_date='2026-08-22', account_id=lex)
        edit(single_id, amount='0', txn_date='bad-date')             # N2: two flashes
        edit(single_id, account_id=lex, txn_date='2026-08-22', amount='275',
             category=ADVANCE_CATEGORY)
        edit(single_id, account_id=lex, txn_date='2026-08-22', amount='275',
             category=SALARY_CATEGORY)
        for txn in (ids['advance_txn'], salary_id, commission_id, payout_id):
            edit(txn)
        post('edit missing row', '/cashbook/txn/99999/edit', {})

        # ── deletes ─────────────────────────────────────────────────────────
        post('delete confirmed duplicate', f'/cashbook/txn/{dup_id}/delete')
        post('delete un-deducted advance E2', f'/cashbook/txn/{adv2_id}/delete')
        post('delete deducted advance', f'/cashbook/txn/{ids["advance_txn"]}/delete')
        for txn in (salary_id, commission_id, payout_id):
            post(f'delete locked {txn}', f'/cashbook/txn/{txn}/delete')
    finally:
        message_flashed.disconnect(on_flash, flask_app)
    return steps


def kind_counts(conn):
    counts = {k: 0 for k in EXPECTED_COUNTS}
    for r in conn.execute("SELECT payroll_item_id, salary_advance_id, commission_payout_id, "
                          "payout_platform FROM cashbook_transactions"):
        if r[0] is not None:
            counts['salary'] += 1
        elif r[1] is not None:
            counts['advance'] += 1
        elif r[2] is not None:
            counts['commission'] += 1
        elif r[3] is not None:
            counts['payout'] += 1
        else:
            counts['manual'] += 1
    return counts


def dump(conn, steps=None):
    """Header + every row the seam touches, ids kept, `created_at` stripped
    (also inside the audit triggers' JSON)."""
    import json
    import platform
    import sqlite3

    def strip(r):
        d = {k: r[k] for k in r.keys() if k != 'created_at'}
        if isinstance(d.get('changed_fields'), str):
            try:
                cf = json.loads(d['changed_fields'])
            except ValueError:
                cf = None
            if isinstance(cf, dict):
                cf.pop('created_at', None)
                d['changed_fields'] = cf
        return d

    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        mig = conn.execute("SELECT MAX(filename), COUNT(*) FROM applied_migrations").fetchone()
        out = {'header': {'migrations_max': mig[0], 'migrations_count': mig[1],
                          'python': platform.python_version(),
                          'scenario_version': SCENARIO_VERSION}}
        for table in _DUMP_TABLES:
            out[table] = [strip(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        ph = ','.join('?' * len(_DUMP_TABLES))
        out['audit_log'] = [strip(r) for r in conn.execute(
            f"SELECT * FROM audit_log WHERE table_name IN ({ph}) ORDER BY id", _DUMP_TABLES)]
        out['kind_counts'] = kind_counts(conn)
        out['steps'] = steps
        return out
    finally:
        conn.row_factory = prev


def main(db_path):
    """The differential entry point, run in each tree from inventory_app/:
    build → redirect → seed → run (admin session) → dump as JSON to stdout."""
    import json
    import sys
    import tempfile
    os.environ['SKIP_DB_INIT'] = '1'           # before `import app` (app.py:204)
    os.environ['WTF_CSRF_ENABLED'] = 'False'
    os.environ.setdefault('SECRET_KEY', 'parity-only')
    os.environ.setdefault('ADMIN_PASSWORD', 'parity-only')
    # Belt and braces: anything NOT redirected lands in a throwaway dir, never
    # in this tree's instance/ DB.
    os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='cashbook-parity-unredirected-')
    import contextlib
    # stdout carries the dump only: init_db's migration log goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        import database
        from app import app as flask_app
        build_db(db_path)
        redirect(setattr, db_path)
        conn = database._connect(db_path)
        try:
            ids = seed(conn)
            flask_app.config['TESTING'] = True
            client = flask_app.test_client()
            with client.session_transaction() as s:
                s.update(ADMIN_SESSION)
            steps = run(client, conn, ids)
            conn.commit()
            out = dump(conn, steps)
        finally:
            conn.close()
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return out
