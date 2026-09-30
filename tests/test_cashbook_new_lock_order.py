"""Card F PR-2: the /cashbook writers take BEGIN IMMEDIATE before every decision
read (plan §3c, §3g probes A and T).

Probe A: the lock is taken for real. `database.begin_immediate` is patched to
call the real one, then try a competing INSERT on a second connection with
`timeout=0`; the route must have made it fail with "locked". Parameterised over
every confirm flag of /cashbook/new (a lock taken only on the unconfirmed path
is the bug this catches), an advance row, txn_edit and txn_delete.

Probe T: BEGIN IMMEDIATE precedes every read. Every connection
`database.get_connection` hands out is traced; the one that writes
`cashbook_transactions` must open with `BEGIN IMMEDIATE`, no SELECT before it.

The one-line changes that turn these red are in the PR body (break-once).
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3

import pytest

from tests import cashbook_seam_scenario as scn
import database

ADMIN = {'user_id': 1, 'username': 'admin', 'display_name': 'Administrator', 'role': 'admin'}


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    path = str(tmp_path_factory.mktemp('lock-order') / 'template.db')
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


def _manual_row(path, ids):
    c = sqlite3.connect(path)
    txn = c.execute(
        "INSERT INTO cashbook_transactions (account_id, txn_date, direction, category, "
        "user_category, amount, created_by) VALUES (?, '2026-08-20', 'expense', ?, "
        "'ร้านเอ', 250.0, 'Administrator')", (ids['acct_392'], scn.MANUAL_CATEGORY)).lastrowid
    c.commit()
    c.close()
    return txn


def _new_form(ids, **extra):
    form = {'account_id': str(ids['acct_392']), 'txn_date': '2026-08-20',
            'rows-0-direction': 'expense', 'rows-0-category': scn.MANUAL_CATEGORY,
            'rows-0-user_category': 'ร้านเอ', 'rows-0-amount': '250',
            'rows-0-description': 'เติมน้ำมัน', 'rows-0-note': '', 'rows-0-employee_id': ''}
    form.update(extra)
    return form


_NEW_VARIANTS = {
    'no-confirm': {},
    'confirm_duplicates': {'confirm_duplicates': '1'},
    'confirm_new_categories': {'confirm_new_categories': '1'},
    'confirm_advance_cap': {'confirm_advance_cap': '1'},
    'confirm_commission_elsewhere': {'confirm_commission_elsewhere': '1'},
}


def _post(kind, path, ids):
    """Drive one successful write through the route; returns the response."""
    cl = _client()
    if kind.startswith('new:'):
        variant = kind[4:]
        if variant == 'advance':
            form = _new_form(ids, **{'rows-0-category': scn.ADVANCE_CATEGORY,
                                     'rows-0-employee_id': str(ids['emp1']),
                                     'rows-0-user_category': '', 'confirm_advance_cap': '1'})
        else:
            form = _new_form(ids, **_NEW_VARIANTS[variant])
        return cl.post('/cashbook/new', data=form)
    txn = _manual_row(path, ids)
    if kind == 'edit':
        return cl.post(f'/cashbook/txn/{txn}/edit', data={
            'account_id': str(ids['acct_392']), 'txn_date': '2026-08-21', 'direction': 'expense',
            'category': scn.MANUAL_CATEGORY, 'user_category': 'ร้านเอ', 'amount': '300',
            'description': '', 'note': ''})
    assert kind == 'delete'
    return cl.post(f'/cashbook/txn/{txn}/delete')


_KINDS = [f'new:{v}' for v in _NEW_VARIANTS] + ['new:advance', 'edit', 'delete']


def _wrote(path, kind, before_max):
    """Control, asserted on STATE: the write the route exists to make landed."""
    c = sqlite3.connect(path)
    try:
        if kind.startswith('new:'):
            return c.execute("SELECT COUNT(*) FROM cashbook_transactions WHERE id > ? "
                             "AND category != 'probe'", (before_max,)).fetchone()[0] == 1
        row = c.execute("SELECT amount FROM cashbook_transactions WHERE id = ?",
                        (before_max + 1,)).fetchone()
        return row is None if kind == 'delete' else row[0] == 300.0
    finally:
        c.close()


def _max_id(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT MAX(id) FROM cashbook_transactions").fetchone()[0]
    finally:
        c.close()


@pytest.mark.parametrize('kind', _KINDS)
def test_probe_a_the_route_holds_the_write_lock(db, monkeypatch, kind):
    path, ids = db
    real = database.begin_immediate
    seen = {}
    calls = []

    def probe(conn):
        real(conn)
        calls.append(1)
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO cashbook_transactions (account_id, txn_date, direction, "
                          "category, amount) VALUES (?, '2026-01-01', 'expense', 'probe', 1)",
                          (ids['acct_392'],))
            other.commit()
            seen['blocked'] = False
        except sqlite3.OperationalError as err:
            seen['blocked'] = 'locked' in str(err)
        finally:
            other.close()

    monkeypatch.setattr(database, 'begin_immediate', probe)
    before_max = _max_id(path)
    resp = _post(kind, path, ids)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:600]
    assert _wrote(path, kind, before_max), "control: the route must have written"
    assert seen == {'blocked': True}, f"{kind}: the lock was not held ({seen}, calls={len(calls)})"
    assert len(calls) == 1


@pytest.mark.parametrize('kind', _KINDS)
def test_probe_t_begin_immediate_precedes_every_read(db, monkeypatch, kind):
    path, ids = db
    real = database.get_connection
    traces = []

    def traced():
        conn = real()
        stmts = []
        conn.set_trace_callback(stmts.append)
        traces.append(stmts)
        return conn

    monkeypatch.setattr(database, 'get_connection', traced)
    before_max = _max_id(path)
    resp = _post(kind, path, ids)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:600]
    assert _wrote(path, kind, before_max), "control: the route must have written"

    def writes(stmts):
        return any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                   and 'CASHBOOK_TRANSACTIONS' in s.upper() for s in stmts)

    writers = [t for t in traces if writes(t)]
    assert len(writers) == 1, f"expected one writing connection, got {len(writers)} of {len(traces)}"
    stmts = [s.strip() for s in writers[0] if not s.strip().upper().startswith('PRAGMA')]
    assert stmts, "control: the trace saw nothing"
    assert stmts[0] == 'BEGIN IMMEDIATE', f"{kind}: first statement was {stmts[0][:120]!r}"
