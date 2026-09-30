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

SCENARIO_VERSION = 2

# Rows per kind at dump time (plan §3f row arithmetic). Changes with `run`.
# The first part of `run` leaves manual 3, advance 2, salary 1, commission 1,
# payout 1 (plan §3f table). `_run_route_edges` adds manual 8 (two confirmed
# new categories, the partial bulk, the off-system commission, the confirmed
# commission-elsewhere, the bulk advance+manual's manual row, the seeded
# retired-category row, the inf amount; the manager's row is deleted again),
# advance 3 (the bulk one, the income-direction one, the seeded orphan whose
# delete is refused) and commission 1 (the seeded link the manager cannot
# touch). The G8 block adds commission 2: two forms, each sent twice, the
# second of each refused (D-4 A; before it, 4).
EXPECTED_COUNTS = {"manual": 11, "advance": 5, "salary": 1, "commission": 4, "payout": 1}
# commission_payouts: mode 2 + the backfill (mode 1 is cancelled), the seeded
# link, and the G8 block's 2. salary_advances: the seeded deducted one, E1's,
# the bulk one, the income-direction one (E2's first is deleted).
# cashbook_categories: 5 seeded + ค่าทางด่วน both directions + the retired one.
EXPECTED_TABLE_COUNTS = {"commission_payouts": 5, "salary_advances": 4, "cashbook_categories": 8}
# Explicit, actor-attributed audit_log rows per table (user NOT NULL; the
# mig-076 triggers write user NULL). cashbook_transactions: salary post x2 +
# void x1, commission record x2 + cancel x1, cashbook delete x2 (the manual row
# and the un-deducted advance), one changed edit; the edges add three changed
# edits and the manager's delete; G8 adds 2 commission records.
# commission_payouts: record x3 (mode 1, mode 2, the account_id=None backfill),
# the seeded link, G8's 2.
EXPECTED_ATTRIBUTED_AUDIT = {'cashbook_transactions': 15, 'commission_payouts': 6}

ADMIN_SESSION = {'user_id': 1, 'username': 'admin', 'display_name': 'Administrator',
                 'role': 'admin'}

_DUMP_TABLES = ('cashbook_transactions', 'salary_advances', 'commission_payouts',
                'cashbook_categories')


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

    def post(name, path, data=None, cl=None):
        del flashes[:]
        resp = (cl or client).post(path, data=data or {})
        rec = {'step': name, 'status': resp.status_code,
               'location': resp.headers.get('Location'), 'flashes': list(flashes)}
        if resp.status_code == 200:
            rec['row_errors'] = _row_errors(resp.get_data(as_text=True))
        steps.append(rec)
        return rec

    def last_id():
        return conn.execute("SELECT MAX(id) FROM cashbook_transactions").fetchone()[0]

    def id_of(sql, params=()):
        """The id a later step targets, or None when the step that should
        have written it did not (e.g. a writer that escaped the redirect):
        the run carries on so the dump's counts, not a crash, show the gap."""
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def note(name, value):
        steps.append({'step': name, 'result': value})

    a392, lex, spx = str(ids['acct_392']), str(ids['acct_LEX']), str(ids['acct_SPX'])

    def row(i, **kw):
        base = {'direction': 'expense', 'category': MANUAL_CATEGORY, 'user_category': '',
                'employee_id': '', 'amount': '', 'description': '', 'note': '', 'txn_date': ''}
        base.update(kw)
        return {f'rows-{i}-{k}': v for k, v in base.items()}

    def edit(txn, name=None, cl=None, **kw):
        form = {'account_id': a392, 'txn_date': '2026-08-20', 'direction': 'expense',
                'category': MANUAL_CATEGORY, 'user_category': 'ร้านเอ', 'amount': '250',
                'description': 'เติมน้ำมัน', 'note': ''}
        form.update(kw)
        return post(name or f'edit {txn} {sorted(kw)}', f'/cashbook/txn/{txn}/edit', form, cl=cl)

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
        salary_id = id_of("SELECT id FROM cashbook_transactions WHERE payroll_item_id = ?",
                          (ids['item1'],))

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
        commission_id = id_of(
            "SELECT id FROM cashbook_transactions WHERE commission_payout_id IS NOT NULL")

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
        payout_id = id_of(
            "SELECT id FROM cashbook_transactions WHERE payout_platform IS NOT NULL")

        # ── edits ───────────────────────────────────────────────────────────
        edit(single_id, amount='275', txn_date='2026-08-22', account_id=lex)
        edit(single_id, amount='0', txn_date='bad-date')             # N2: two flashes
        edit(single_id, account_id=lex, txn_date='2026-08-22', amount='275',
             category=ADVANCE_CATEGORY)
        edit(single_id, account_id=lex, txn_date='2026-08-22', amount='275',
             category=SALARY_CATEGORY)
        for txn in (ids['advance_txn'], salary_id, commission_id, payout_id):
            if txn is None:
                note('edit locked: row missing', None)
                continue
            edit(txn)
        post('edit missing row', '/cashbook/txn/99999/edit', {})

        # ── deletes ─────────────────────────────────────────────────────────
        post('delete confirmed duplicate', f'/cashbook/txn/{dup_id}/delete')
        post('delete un-deducted advance E2', f'/cashbook/txn/{adv2_id}/delete')
        post('delete deducted advance', f'/cashbook/txn/{ids["advance_txn"]}/delete')
        for txn in (salary_id, commission_id, payout_id):
            if txn is None:
                note('delete locked: row missing', None)
                continue
            post(f'delete locked {txn}', f'/cashbook/txn/{txn}/delete')

        _run_route_edges(post, edit, row, last_id, conn, ids)

        # ── G8: each commission form sent twice (a double-click) ────────────
        # LAST on purpose: recorded twice before D-4 A, refused from D-4 A on,
        # and nothing after it gets an id, so the D-4 differential shows only
        # these two refusals (their payout + cashbook + audit rows and flashes).
        for name, form in (
                ('G8 mode 1', {'month': '2026-08', 'paid_date': '2026-09-04', 'account_id': a392,
                               'sp_code': ids['sp'], 'invoice_no': 'IV6902',
                               'amount_IV6902': '275.5'}),
                ('G8 mode 2', {'month': '2026-08', 'paid_date': '2026-09-04', 'account_id': a392,
                               f'amount_{ids["sp"]}': '180', 'sp_code': ids['sp']})):
            post(name, '/commission/payout', form)
            post(f'{name} double-submit', '/commission/payout', form)
    finally:
        message_flashed.disconnect(on_flash, flask_app)
    return steps


MANAGER_SESSION = {'user_id': 2, 'username': 'mgr', 'display_name': 'Manager',
                   'role': 'manager'}


def _run_route_edges(post, edit, row, last_id, conn, ids):
    """The /cashbook paths the first `run` skipped (PR-2 review W1): the
    places a route gate and a ledger backstop could most plausibly disagree,
    which PR-4 (D-1) will change. From the reviewer's `extra_scenario.py`."""
    from app import app as flask_app
    mgr = flask_app.test_client()
    with mgr.session_transaction() as s:
        s.update(MANAGER_SESSION)
    a392, lex = str(ids['acct_392']), str(ids['acct_LEX'])

    # a brand-new category, confirmed (the route upserts it, the ledger re-checks it),
    # then the same name in the other direction (a second (name, direction) row)
    post('new category confirmed', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', 'confirm_new_categories': '1',
        **row(0, category='ค่าทางด่วน', amount='40')})
    newcat_id = last_id()
    post('new category other direction confirmed', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', 'confirm_new_categories': '1',
        **row(0, category='ค่าทางด่วน', direction='income', amount='41')})
    # bulk: one policy-blocked salary row + one valid row: the rest is saved
    post('bulk partial policy block', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', 'bulk_mode': '1',
        **row(0, category=SALARY_CATEGORY, amount='100'),
        **row(1, amount='70', description='น้ำมัน')})
    # in-engine commission: blocked with the link; an off-system recipient: saved
    post('new in-engine commission blocked', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21',
        **row(0, category=COMMISSION_CATEGORY, user_category='เจียรนัย', amount='500')})
    post('new off-system commission', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21',
        **row(0, category=COMMISSION_CATEGORY, user_category='อัคเรศ', amount='510')})
    post('commission elsewhere confirmed', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', 'confirm_commission_elsewhere': '1',
        **row(0, user_category='เจียรนัย', amount='400', description='ค่าคอมมิชชั่น')})
    # an in-batch duplicate pair, unconfirmed: both rows flagged, nothing saved
    post('in-batch dup pair', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', 'bulk_mode': '1',
        **row(0, amount='15'), **row(1, amount='15')})
    # bulk advance + manual: cap unconfirmed, then confirmed
    mixed = {'account_id': a392, 'txn_date': '2026-09-07', 'bulk_mode': '1',
             **row(0, category=ADVANCE_CATEGORY, employee_id=str(ids['emp2']),
                   amount='900', note='เบิก'),
             **row(1, amount='20', txn_date='2026-09-08', user_category='สมหญิง')}
    post('bulk advance+manual unconfirmed', '/cashbook/new', mixed)
    post('bulk advance+manual confirmed', '/cashbook/new', {**mixed, 'confirm_advance_cap': '1'})
    post('advance bad employee', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-09-07',
        **row(0, category=ADVANCE_CATEGORY, employee_id='9999', amount='100')})
    # an advance row submitted as income: the route forces expense
    post('advance income direction', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-09-09', 'confirm_advance_cap': '1',
        **row(0, category=ADVANCE_CATEGORY, direction='income',
              employee_id=str(ids['emp1']), amount='10')})
    # a retired category: refused on new; an existing row in it stays editable
    # while the category is unchanged; editing INTO it (or a missing one) is refused
    conn.execute("INSERT INTO cashbook_categories(name, direction, source, is_active) "
                 "VALUES ('เก่า', 'expense', 'setup', 0)")
    conn.commit()
    post('new retired category', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', **row(0, category='เก่า', amount='10')})
    rid = conn.execute(
        "INSERT INTO cashbook_transactions(account_id, txn_date, direction, category, amount, "
        "created_by) VALUES (?, '2026-08-01', 'expense', 'เก่า', 33.0, 'admin')",
        (ids['acct_392'],)).lastrowid
    conn.commit()
    edit(rid, 'edit keep retired category', category='เก่า', amount='34', txn_date='2026-08-01',
         user_category='', description='')
    edit(newcat_id, 'edit into retired category', category='เก่า')
    edit(newcat_id, 'edit into missing category', category='ไม่มีหมวดนี้')
    # tag resolution to a nickname; nothing changed; two errors; whitespace → NULL
    edit(newcat_id, 'edit tag resolves', category='ค่าทางด่วน', user_category='สมชาย')
    edit(newcat_id, 'edit no change', category='ค่าทางด่วน', user_category='ชาย',
         txn_date='2026-08-20')
    edit(newcat_id, 'edit bad account blank date', account_id='abc', txn_date='')
    edit(newcat_id, 'edit whitespace fields', category='ค่าทางด่วน', user_category='ชาย',
         description='   ', note='  ')
    # a commission-linked row: the manager's lock wording differs from admin's
    pid = conn.execute(
        "INSERT INTO commission_payouts(year_month, salesperson_code, amount_paid, paid_date, "
        "paid_by, invoice_no) VALUES ('2026-08', '06', 123.0, '2026-09-01', 'admin', 'IV1')"
    ).lastrowid
    cid = conn.execute(
        "INSERT INTO cashbook_transactions(account_id, txn_date, direction, category, amount, "
        "created_by, commission_payout_id) VALUES (?, '2026-09-01', 'expense', ?, 123.0, "
        "'admin', ?)", (ids['acct_392'], COMMISSION_CATEGORY, pid)).lastrowid
    conn.commit()
    edit(cid, 'edit commission as manager', cl=mgr)
    post('delete commission as manager', f'/cashbook/txn/{cid}/delete', cl=mgr)
    edit(cid, 'edit commission as admin')
    post('delete missing row', '/cashbook/txn/99999/delete')
    post('new empty form', '/cashbook/new', {'account_id': a392, 'txn_date': '2026-08-21'})
    # 'inf' passes today's `> 0` rule (D-1, parked, changes this)
    post('new inf amount', '/cashbook/new', {
        'account_id': a392, 'txn_date': '2026-08-21', **row(0, amount='inf')})
    # a manager creates and deletes a manual row
    post('new as manager', '/cashbook/new', {
        'account_id': lex, 'txn_date': '2026-08-23', **row(0, amount='7')}, cl=mgr)
    post('delete as manager', f'/cashbook/txn/{last_id()}/delete', cl=mgr)
    # an advance row whose salary_advances parent is gone (FKs off to seed it;
    # unreachable on prod, pinned so a change of its flash is visible)
    conn.execute("PRAGMA foreign_keys = OFF")
    orphan = conn.execute(
        "INSERT INTO cashbook_transactions(account_id, txn_date, direction, category, amount, "
        "created_by, salary_advance_id) VALUES (?, '2026-09-10', 'expense', ?, 5.0, 'admin', "
        "99999)", (ids['acct_392'], ADVANCE_CATEGORY)).lastrowid
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    post('delete advance without parent', f'/cashbook/txn/{orphan}/delete')


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
