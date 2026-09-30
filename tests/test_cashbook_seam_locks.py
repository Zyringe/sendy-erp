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
