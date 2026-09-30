"""Card F PR-3: hr, commission and the payout mirror take BEGIN IMMEDIATE before
every decision read (plan §3c, §3g probes A and T).

Probe A: the lock is taken for real. `database.begin_immediate` is patched to
call the real one, then try a competing INSERT on a second connection with
`timeout=0`; the writer must have made it fail with "locked".

Probe T: BEGIN IMMEDIATE precedes every read. The connection the writer uses
is traced at the point it is obtained: `hr._connect` for the /hr pay and unpay
routes, `commission._connect` for the /commission routes (owned connection),
and the test's own traced connection for `record_payout` / `delete_payout`
(borrowed-clean) and `mirror_platform`.

The one-line changes that turn these red are in the PR body (break-once).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

import commission  # noqa: F401  (imported before redirect(): see test_cashbook_seam_parity)
import hr  # noqa: F401
from tests import cashbook_seam_scenario as scn
import database

ADMIN = {'user_id': 1, 'username': 'admin', 'display_name': 'Administrator', 'role': 'admin'}


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    path = str(tmp_path_factory.mktemp('seam-locks') / 'template.db')
    scn.build_db(path)
    c = database._connect(path)
    ids = scn.seed(c)
    c.close()
    return path, ids


@pytest.fixture
def db(template, tmp_path, monkeypatch):
    path = str(tmp_path / 'a.db')
    s, d = sqlite3.connect(template[0]), sqlite3.connect(path)
    s.backup(d)
    s.close()
    d.close()
    scn.redirect(monkeypatch.setattr, path)
    return path, template[1]


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(ADMIN)
    return c


def _q(path, sql, params=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _competing(path, ids, table):
    """One write another worker would make; returns 'blocked' | 'went through'."""
    other = sqlite3.connect(path, timeout=0)
    try:
        if table == 'commission_payouts':
            other.execute("INSERT INTO commission_payouts (year_month, salesperson_code, "
                          "amount_paid, paid_date) VALUES ('2026-01', 'probe', 1, '2026-01-01')")
        else:
            other.execute("INSERT INTO cashbook_transactions (account_id, txn_date, direction, "
                          "category, amount) VALUES (?, '2026-01-01', 'expense', 'probe', 1)",
                          (ids['acct_392'],))
        other.commit()
        return 'went through'
    except sqlite3.OperationalError as err:
        return 'blocked' if 'locked' in str(err) else f'other error: {err}'
    finally:
        other.close()


def _probe_a(monkeypatch, path, ids, table):
    real = database.begin_immediate
    seen = []

    def probe(conn):
        real(conn)
        seen.append(_competing(path, ids, table))

    monkeypatch.setattr(database, 'begin_immediate', probe)
    return seen


def _trace_factory(monkeypatch, module, name):
    """Patch module.<name> (a connection factory) so every connection it
    hands out records its statements. Returns the list of traces."""
    real = getattr(module, name)
    traces = []

    def traced(*a, **kw):
        conn = real(*a, **kw)
        stmts = []
        conn.set_trace_callback(stmts.append)
        traces.append(stmts)
        return conn

    monkeypatch.setattr(module, name, traced)
    return traces


def _writer_trace(traces, table):
    """The one traced connection that wrote `table`, PRAGMAs dropped."""
    def writes(stmts):
        return any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                   and table.upper() in s.upper() for s in stmts)
    writers = [t for t in traces if writes(t)]
    assert len(writers) == 1, f"expected one connection writing {table}, got {len(writers)}"
    stmts = [s.strip() for s in writers[0] if not s.strip().upper().startswith('PRAGMA')]
    assert stmts, "control: the trace saw nothing"
    return stmts


def _assert_begin_first(stmts, label):
    assert stmts[0] == 'BEGIN IMMEDIATE', f"{label}: first statement was {stmts[0][:120]!r}"


# ── hr: /hr/payroll/<run>/item/<item>/pay | unpay ────────────────────────────

def _hr_post(path, ids, kind):
    """Drive pay (kind='pay') or pay-then-unpay (kind='unpay'); the unpay's
    preceding pay is done directly so only the route call is probed."""
    run, item = ids['run'], ids['item1']
    if kind == 'unpay':
        c = hr._connect(path)
        try:
            hr.post_salary_payment(item, ids['acct_392'], '2026-08-31', 'setup', conn=c)
        finally:
            c.close()
        return _client().post(f'/hr/payroll/{run}/item/{item}/unpay')
    return _client().post(f'/hr/payroll/{run}/item/{item}/pay',
                          data={'account_id': str(ids['acct_392']), 'pay_date': '2026-08-31'})


def _hr_paid(path, ids):
    return _q(path, "SELECT COUNT(*) FROM cashbook_transactions WHERE payroll_item_id = ?",
              (ids['item1'],))[0][0]


@pytest.mark.parametrize('kind', ['pay', 'unpay'])
def test_probe_a_hr_route_holds_the_write_lock(db, monkeypatch, kind):
    path, ids = db
    if kind == 'unpay':
        _hr_post(path, ids, 'pay')          # setup through the unprobed route
    seen = _probe_a(monkeypatch, path, ids, 'cashbook_transactions')
    resp = (_client().post(f'/hr/payroll/{ids["run"]}/item/{ids["item1"]}/unpay')
            if kind == 'unpay' else _hr_post(path, ids, 'pay'))
    assert resp.status_code == 302
    assert _hr_paid(path, ids) == (1 if kind == 'pay' else 0), "control: the route wrote"
    assert seen == ['blocked'], f"{kind}: {seen}"


@pytest.mark.parametrize('kind', ['pay', 'unpay'])
def test_probe_t_hr_route_begins_before_reading(db, monkeypatch, kind):
    path, ids = db
    if kind == 'unpay':
        _hr_post(path, ids, 'pay')
    traces = _trace_factory(monkeypatch, hr, '_connect')
    resp = (_client().post(f'/hr/payroll/{ids["run"]}/item/{ids["item1"]}/unpay')
            if kind == 'unpay' else _hr_post(path, ids, 'pay'))
    assert resp.status_code == 302
    assert _hr_paid(path, ids) == (1 if kind == 'pay' else 0), "control: the route wrote"
    _assert_begin_first(_writer_trace(traces, 'cashbook_transactions'), kind)


def test_hr_unpay_refuses_a_caller_transaction(db):
    """void_salary_payment now locks like post_salary_payment: a borrowed
    connection with a transaction already open is refused (hr's contract)."""
    path, ids = db
    c = hr._connect(path)
    try:
        hr.post_salary_payment(ids['item1'], ids['acct_392'], '2026-08-31', 'setup', conn=c)
        c.execute("BEGIN")
        with pytest.raises(hr.CallerTransactionInFlight):
            hr.void_salary_payment(ids['item1'], 'x', conn=c)
        c.rollback()
    finally:
        c.close()
    assert _hr_paid(path, ids) == 1


# ── commission: record_payout / delete_payout and their routes ───────────────

def _payouts(path):
    return _q(path, "SELECT COUNT(*) FROM commission_payouts")[0][0]


def _record(ids, conn=None, **kw):
    args = dict(year_month='2026-08', salesperson_code=ids['sp'], amount_paid=450.0,
                paid_date='2026-09-01', paid_by='Administrator', invoice_no='IV6901',
                account_id=ids['acct_392'])
    args.update(kw)
    return commission.record_payout(conn=conn, **args)


def _route_mode(ids, mode):
    form = {'month': '2026-08', 'paid_date': '2026-09-01', 'account_id': str(ids['acct_392']),
            'sp_code': ids['sp']}
    if mode == 1:
        form.update({'invoice_no': 'IV6901', 'amount_IV6901': '450'})
    else:
        form[f'amount_{ids["sp"]}'] = '300'
    return _client().post('/commission/payout', data=form)


_COMMISSION_KINDS = ['record owned', 'record borrowed-clean', 'delete owned',
                     'delete borrowed-clean', 'route mode 1', 'route mode 2', 'route delete']


def _drive_commission(path, ids, kind):
    """Setup before probing: a payout to delete for the delete kinds."""
    if 'delete' in kind:
        return _record(ids)
    return None


def _do_commission(path, ids, kind, pid, conn):
    if kind == 'record owned':
        _record(ids)
    elif kind == 'record borrowed-clean':
        _record(ids, conn=conn)
    elif kind == 'delete owned':
        commission.delete_payout(pid, actor='x')
    elif kind == 'delete borrowed-clean':
        commission.delete_payout(pid, actor='x', conn=conn)
    elif kind == 'route delete':
        assert _client().post(f'/commission/payout/{pid}/delete').status_code == 302
    else:
        assert _route_mode(ids, int(kind[-1])).status_code == 302


@pytest.mark.parametrize('kind', _COMMISSION_KINDS)
def test_probe_a_commission_holds_the_write_lock(db, monkeypatch, kind):
    path, ids = db
    pid = _drive_commission(path, ids, kind)
    before = _payouts(path)
    conn = commission._connect(path) if 'borrowed' in kind else None
    try:
        seen = _probe_a(monkeypatch, path, ids, 'commission_payouts')
        _do_commission(path, ids, kind, pid, conn)
    finally:
        if conn is not None:
            conn.close()
    want = before - 1 if 'delete' in kind else before + 1
    assert _payouts(path) == want, "control: the write landed and was committed"
    assert seen == ['blocked'], f"{kind}: {seen}"


@pytest.mark.parametrize('kind', _COMMISSION_KINDS)
def test_probe_t_commission_begins_before_reading(db, monkeypatch, kind):
    path, ids = db
    pid = _drive_commission(path, ids, kind)
    if 'borrowed' in kind:
        conn = commission._connect(path)
        stmts = []
        conn.set_trace_callback(stmts.append)
        traces = [stmts]
    else:
        conn = None
        traces = _trace_factory(monkeypatch, commission, '_connect')
    try:
        _do_commission(path, ids, kind, pid, conn)
    finally:
        if conn is not None:
            conn.close()
    stmts = _writer_trace(traces, 'commission_payouts')
    _assert_begin_first(stmts, kind)
    if kind.startswith('record') or kind.startswith('route mode'):
        up = [s.upper() for s in stmts]
        acct = next(i for i, s in enumerate(up) if s.startswith('SELECT')
                    and 'FROM CASHBOOK_ACCOUNTS' in s)
        ins_p = next(i for i, s in enumerate(up) if s.startswith('INSERT INTO COMMISSION_PAYOUTS'))
        ins_c = next(i for i, s in enumerate(up) if s.startswith('INSERT INTO CASHBOOK_TRANSACTIONS'))
        assert 0 < acct < ins_p < ins_c, (kind, acct, ins_p, ins_c)


def test_commission_borrowed_in_flight_is_adopted_never_ended(db):
    """A caller's open transaction: no BEGIN, no COMMIT, no ROLLBACK (the #589
    script's shape); its writes stay the caller's to commit or roll back."""
    path, ids = db
    conn = commission._connect(path)
    stmts = []
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.set_trace_callback(stmts.append)
        pid = _record(ids, conn=conn)
        assert conn.in_transaction, "the caller's transaction must still be open"
        conn.set_trace_callback(None)
        conn.rollback()
    finally:
        conn.close()
    up = [s.strip().upper() for s in stmts]
    assert pid and any(s.startswith('INSERT INTO COMMISSION_PAYOUTS') for s in up), "control"
    assert not [s for s in up if s.startswith(('BEGIN', 'COMMIT', 'ROLLBACK', 'END'))], up
    assert _payouts(path) == 0, "the caller's rollback took the payout with it"


def test_commission_refusal_in_flight_writes_nothing_and_leaves_the_txn(db):
    path, ids = db
    conn = commission._connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='บัญชีที่เลือกไม่ถูกต้อง'):
            _record(ids, conn=conn, account_id=9999)
        assert conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0] == 0
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize('owned', [True, False])
def test_commission_refusal_releases_the_lock(db, owned):
    path, ids = db
    conn = None if owned else commission._connect(path)
    try:
        with pytest.raises(ValueError, match='บัญชีที่เลือกไม่ถูกต้อง'):
            _record(ids, conn=conn, account_id=9999)
        if conn is not None:
            assert not conn.in_transaction, "borrowed-clean: the refusal rolled back"
    finally:
        if conn is not None:
            conn.close()
    assert _competing(path, ids, 'commission_payouts') == 'went through'
    assert _payouts(path) == 1          # only the competing row
