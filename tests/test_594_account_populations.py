"""WHICH population each `is_transfer` reader answers, pinned by behaviour.

The census (test_is_transfer_population_coverage.py) lists every site and
the population it declares. A count cannot tell those populations apart, so
this file drives the REAL function behind each OPERATING / PAYABLE / EXPECTED
site against three accounts and asserts which of them it lets through:

  OP   active,   not a transfer account   an ordinary operating account
  RET  inactive, not a transfer account   904 after #594 (ADR 0017)
  TR   active,   a transfer account       a conduit

  OPERATING -> {OP, RET}  a retired account's money still happened
  PAYABLE   -> {OP}       only an active non-conduit account takes new money
  EXPECTED  -> {OP}       a retired account is never expected to be keyed

Each direction of drift turns a probe red: an OPERATING read that gains
`is_active = 1` loses RET (904's recovered expense vanishes again), a PAYABLE
read that loses it admits RET (904 becomes a payment target), and either one
losing `is_transfer` admits TR.

Amounts are powers of two (OP 1, RET 2, TR 4) so a summed figure decodes to
exactly the set of accounts it included. Fixture: `empty_db` (full live schema,
zero rows) — never `tmp_db`, whose rows would leak into every sum.
"""
import sqlite3

import pytest

OP, RET, TR = 'OP', 'RET', 'TR'
WEIGHT = {OP: 1, RET: 2, TR: 4}

OPERATING_SET = {OP, RET}
PAYABLE_SET = {OP}
EXPECTED_SET = {OP}

MONTH = '2026-02'                 # the month every summary probe reads
PRIOR_MONTH = '2026-03'           # holds the prior-period (belongs_to_period) rows
KEYED_MONTHS = ('2026-02', '2026-03', '2026-04', '2026-05')
UNKEYED_MONTH = '2026-06'         # nobody keyed it: the expected accounts go missing


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _decode(total):
    """{codes} whose weights sum to `total` — the inverse of WEIGHT."""
    n = round(total)
    assert abs(total - n) < 1e-9 and 0 <= n < 8, total
    return {code for code, w in WEIGHT.items() if n & w}


@pytest.fixture
def seeded(empty_db, monkeypatch):
    """Three accounts, an expense in every keyed month for each, a
    prior-period row for each, and the HR/commission rows the pay paths need.
    Returns (db_path, {code: account_id})."""
    # empty_db patches config/database only; these two modules snapshot
    # DATABASE_PATH at import and open their own connection when called
    # without one (the /commission/payout route does exactly that) — without
    # this the route probe would write into the worktree's live DB.
    import commission
    import hr
    monkeypatch.setattr(commission, 'DATABASE_PATH', empty_db)
    monkeypatch.setattr(hr, 'DATABASE_PATH', empty_db)

    conn = _conn(empty_db)
    ids = {}
    for code, active, transfer in ((OP, 1, 0), (RET, 0, 0), (TR, 1, 1)):
        ids[code] = conn.execute(
            'INSERT INTO cashbook_accounts (code, is_active, is_transfer) VALUES (?, ?, ?)',
            (code, active, transfer)).lastrowid
    for code, aid in ids.items():
        for ym in KEYED_MONTHS:
            conn.execute(
                """INSERT INTO cashbook_transactions
                     (account_id, txn_date, direction, category, user_category, amount)
                   VALUES (?, ?, 'expense', 'ค่าเช่า', 'โกดัง', ?)""",
                (aid, f'{ym}-10', float(WEIGHT[code])))
        conn.execute(
            """INSERT INTO cashbook_transactions
                 (account_id, txn_date, direction, category, amount, belongs_to_period)
               VALUES (?, ?, 'expense', 'จ่ายค่าโบนัส', ?, '2025')""",
            (aid, f'{PRIOR_MONTH}-09', float(WEIGHT[code])))

    # The salary pay-event refuses anything but a finalized run's item with
    # net_pay > 0, so each account gets its own item to be tried against.
    company = conn.execute(
        "INSERT INTO companies (code, name_th) VALUES ('T594', 'บริษัททดสอบ')").lastrowid
    run = conn.execute(
        """INSERT INTO payroll_runs (year_month, company_id, status, run_date, created_by)
           VALUES ('2026-09', ?, 'finalized', '2026-09-28', 1)""", (company,)).lastrowid
    for code in ids:
        emp = conn.execute(
            """INSERT INTO employees
                 (emp_code, full_name, nickname, gender, company_id, start_date,
                  probation_days, sso_enrolled, diligence_allowance, is_active)
               VALUES (?, ?, ?, 'M', ?, '2026-01-01', 90, 0, 0, 1)""",
            (f'T594{code}', f'ทดสอบ {code}', code, company)).lastrowid
        conn.execute(
            """INSERT INTO payroll_items (run_id, employee_id, salary_rate, base_amount, net_pay)
               VALUES (?, ?, 1000, 1000, 1000)""", (run, emp))
    # commission_payouts.salesperson_code is a foreign key.
    conn.execute("INSERT INTO salespersons (code, name) VALUES ('T594', 'ทดสอบ /T594')")
    conn.commit()

    # CONTROL: the three shapes really are the three shapes.
    got = {r['code']: (r['is_active'], r['is_transfer']) for r in conn.execute(
        'SELECT code, is_active, is_transfer FROM cashbook_accounts')}
    assert got == {OP: (1, 0), RET: (0, 0), TR: (1, 1)}, got
    conn.close()
    return empty_db, ids


# ── OPERATING probes: which accounts' money the figure includes ─────────────

def _accounting_expenses(db, ids):
    import models
    s = models.get_accounting_summary(f'{MONTH}-01', f'{MONTH}-28')
    return _decode(s['expenses'])


def _accounting_prior_period(db, ids):
    import models
    s = models.get_accounting_summary(f'{PRIOR_MONTH}-01', f'{PRIOR_MONTH}-31')
    return _decode(s['prior_period_expenses'])


def _financial_health_overhead(db, ids):
    """as_of 2026-05-15 -> trailing Feb, Mar, Apr. Each month holds the same
    ค่าเช่า rows (March also the bonus rows, which this figure does not split
    out), so the MEDIAN is one month's ค่าเช่า total."""
    from datetime import date
    from models import financial_health as fh
    conn = _conn(db)
    try:
        return _decode(fh._trailing_overhead(conn, date(2026, 5, 15)))
    finally:
        conn.close()


def _cashbook(db, fn):
    conn = _conn(db)
    try:
        return fn(conn)
    finally:
        conn.close()


def _cashbook_monthly(db, ids):
    from blueprints import cashbook as cb
    # the argument the dashboard passes (dashboard(): exclude_transfer=True)
    rows = _cashbook(db, lambda c: cb._get_monthly_summary(c, exclude_transfer=True))
    return _decode(next(r['expense'] for r in rows if r['month'] == MONTH))


def _cashbook_category(db, ids):
    from blueprints import cashbook as cb
    _inc, exp = _cashbook(db, lambda c: cb._get_category_summary(c, MONTH))
    return _decode(sum(e['total'] for e in exp))


def _cashbook_tag(db, ids):
    from blueprints import cashbook as cb
    rows = _cashbook(db, lambda c: cb._get_tag_summary(c, MONTH))
    return _decode(sum(r['total'] for r in rows))


def _cashbook_range(db, ids):
    from blueprints import cashbook as cb
    got = _cashbook(db, lambda c: cb._expense_by_category_range(c, f'{MONTH}-01', f'{MONTH}-28'))
    return _decode(sum(got.values()))


def _cashbook_headline(db, ids):
    from blueprints import cashbook as cb
    return _decode(_cashbook(db, lambda c: cb._get_operating_totals(c, MONTH))['expense'])


def _cashbook_detail(db, ids):
    from blueprints import cashbook as cb
    rows, _summary = _cashbook(db, lambda c: cb._get_detail_rows(c, 'month', MONTH))
    return {r['account_code'] for r in rows}


# ── EXPECTED probe: which accounts a month is judged incomplete without ─────

def _accounting_expected(db, ids):
    """Every account keyed Feb-May and nobody keyed June, so June's `missing`
    list is exactly the set of accounts the reading EXPECTS."""
    from models import accounting
    conn = _conn(db)
    try:
        months = accounting._incomplete_months(conn, f'{UNKEYED_MONTH}-01', f'{UNKEYED_MONTH}-30')
    finally:
        conn.close()
    assert [m['ym'] for m in months] == [UNKEYED_MONTH], months
    return set(months[0]['missing'])


# ── PAYABLE probes: which accounts new money may be posted to ───────────────

def _pay_from_picker(db, ids):
    import hr_queries as hrq
    return _cashbook(db, lambda c: {r['code'] for r in
                                    hrq.get_active_cashbook_accounts(c, non_transfer_only=True)})


def _commission_record_payout(db, ids):
    import commission
    accepted = set()
    for code, aid in ids.items():
        conn = _conn(db)
        try:
            commission.record_payout(
                year_month='2026-09', salesperson_code='T594', amount_paid=100.0,
                paid_date='2026-09-19', account_id=aid, conn=conn)
            accepted.add(code)
        except ValueError:
            pass
        finally:
            conn.close()
    return accepted


def _salary_pay_event(db, ids):
    import hr
    conn = _conn(db)
    items = {r['nickname']: r['id'] for r in conn.execute(
        'SELECT pi.id, e.nickname FROM payroll_items pi JOIN employees e ON e.id = pi.employee_id')}
    conn.close()
    assert set(items) == set(ids), items
    accepted = set()
    for code, aid in ids.items():
        conn = _conn(db)
        try:
            hr.post_salary_payment(items[code], aid, '2026-09-28', 'test', conn=conn)
            accepted.add(code)
        except ValueError:
            pass
        finally:
            conn.close()
    return accepted


# The route's OWN refusal wording. record_payout refuses with a different
# sentence, so a route that lost its check would fall through to that one —
# which is exactly what this probe must be able to tell apart.
_ROUTE_REFUSAL = 'กรุณาเลือกบัญชีจ่ายเงินที่ถูกต้องและยังใช้งานอยู่'


def _commission_route(db, ids):
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    passed = set()
    for code, aid in ids.items():
        c = flask_app.test_client()
        with c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'test-admin'
            sess['display_name'] = 'Test'
            sess['role'] = 'admin'
        resp = c.post('/commission/payout', data={
            'month': '2026-09', 'sp_code': 'T594', 'amount_T594': '100',
            'paid_date': '2026-09-19', 'account_id': str(aid)}, follow_redirects=False)
        assert resp.status_code == 302, (code, resp.status_code)
        with c.session_transaction() as sess:
            flashes = [msg for _cat, msg in sess.get('_flashes', [])]
        assert flashes, f'{code}: the route flashed nothing, so it proved nothing'
        if _ROUTE_REFUSAL not in flashes:
            passed.add(code)
    return passed


PROBES = {
    'accounting_expenses':        ('OPERATING', _accounting_expenses),
    'accounting_prior_period':    ('OPERATING', _accounting_prior_period),
    'financial_health_overhead':  ('OPERATING', _financial_health_overhead),
    'cashbook_monthly':           ('OPERATING', _cashbook_monthly),
    'cashbook_category':          ('OPERATING', _cashbook_category),
    'cashbook_tag':               ('OPERATING', _cashbook_tag),
    'cashbook_range':             ('OPERATING', _cashbook_range),
    'cashbook_detail':            ('OPERATING', _cashbook_detail),
    'cashbook_headline':          ('OPERATING', _cashbook_headline),
    'accounting_expected':        ('EXPECTED', _accounting_expected),
    'pay_from_picker':            ('PAYABLE', _pay_from_picker),
    'commission_record_payout':   ('PAYABLE', _commission_record_payout),
    'salary_pay_event':           ('PAYABLE', _salary_pay_event),
    'commission_route':           ('PAYABLE', _commission_route),
}

EXPECTED_SETS = {'OPERATING': OPERATING_SET, 'PAYABLE': PAYABLE_SET, 'EXPECTED': EXPECTED_SET}


@pytest.mark.parametrize('probe', sorted(PROBES))
def test_each_site_reads_its_population(seeded, probe):
    db, ids = seeded
    population, fn = PROBES[probe]
    got = fn(db, ids)
    assert got == EXPECTED_SETS[population], (
        f'{probe} is declared {population} and should let through '
        f'{sorted(EXPECTED_SETS[population])}, but let through {sorted(got)}. '
        'RET is 904 after #594 (inactive, not a transfer account); TR is a '
        'conduit. See test_is_transfer_population_coverage.py.')


def test_the_route_probe_wrote_into_this_fixture(seeded):
    """CONTROL for the route probe: the one payout it accepts (OP) must land
    in THIS fixture's DB. A route writing somewhere else would leave the probe
    reading 'accepted' off a flash while the money went to the wrong file."""
    db, ids = seeded
    assert _commission_route(db, ids) == {OP}
    conn = _conn(db)
    rows = conn.execute(
        """SELECT ca.code FROM cashbook_transactions ct
             JOIN cashbook_accounts ca ON ca.id = ct.account_id
            WHERE ct.category = 'จ่ายค่าคอมมิชชั่น'""").fetchall()
    conn.close()
    assert [r['code'] for r in rows] == [OP]
