"""Card F D-4 A (+ D-4.1 A): an identical commission payout is refused, under the
lock, before the payout INSERT (Put, decisions/log.md 2026-09-30).

The key is (salesperson_code, invoice_no IS ?, year_month, amount_paid,
paid_date), raw `amount_paid` equality (the route stores `float(amt_raw)`, so a
double-submit is an exact repeat). It applies when `account_id` is given (the
route path); `account_id=None` (the pre-Feb backfill) is exempt. The SELECT
scans every existing payout, backfill rows included.

Order, pinned here and by probe T: the account check runs BEFORE the tuple
check. tests/test_594_account_populations.py and tests/test_mig188_unflag_904.py
record one tuple on several accounts and expect the refused accounts to fall
at the account rule; they catch any ValueError, so only the message test below
tells the two orders apart.
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
REFUSAL_HEAD = 'จ่ายค่าคอมรายการนี้ด้วยยอดและวันที่เดียวกันไปแล้ว (payout #'
REFUSAL_TAIL = ('): ถ้าตั้งใจจ่ายอีกครั้งในวันเดียวกันจริง ให้รวมเป็นยอดเดียว '
                'หรือระบุวันที่จ่ายจริงของครั้งที่สอง')
ACCOUNT_TEXT = 'บัญชีที่เลือกไม่ถูกต้องหรือถูกปิดใช้งานแล้ว'
TRANSFER_TEXT = 'ไม่สามารถจ่ายค่าคอมมิชชั่นเข้าบัญชีประเภทเงินโอนได้'


def refusal(payout_id):
    return f'{REFUSAL_HEAD}{payout_id}{REFUSAL_TAIL}'


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    path = str(tmp_path_factory.mktemp('d4') / 'template.db')
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


def _q(path, sql, params=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def _counts(path):
    return (_q(path, "SELECT COUNT(*) FROM commission_payouts")[0][0],
            _q(path, "SELECT COUNT(*) FROM cashbook_transactions "
                     "WHERE commission_payout_id IS NOT NULL")[0][0])


def _record(ids, **kw):
    args = dict(year_month='2026-08', salesperson_code=ids['sp'], amount_paid=450.0,
                paid_date='2026-09-01', paid_by='Administrator', invoice_no='IV6901',
                account_id=ids['acct_392'])
    args.update(kw)
    return commission.record_payout(**args)


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(ADMIN)
    return c


def _post_flashes(client, form):
    from flask import message_flashed
    from app import app as flask_app
    got = []

    def on_flash(sender, message, category, **_):
        got.append((str(message), category))
    message_flashed.connect(on_flash, flask_app)
    try:
        resp = client.post('/commission/payout', data=form)
    finally:
        message_flashed.disconnect(on_flash, flask_app)
    assert resp.status_code == 302
    return got


# ── (1) / (4): the double-submit through the real route, both modes ──────────

@pytest.mark.parametrize('mode', [1, 2])
def test_route_double_submit_is_refused(db, mode):
    path, ids = db
    form = {'month': '2026-08', 'paid_date': '2026-09-01', 'account_id': str(ids['acct_392']),
            'sp_code': ids['sp']}
    if mode == 1:
        form.update({'invoice_no': 'IV6901', 'amount_IV6901': '450'})
        ok = 'บันทึกการจ่าย commission แล้ว 1 ใบ'
    else:
        form[f'amount_{ids["sp"]}'] = '300'
        ok = 'บันทึกการจ่าย commission แล้ว 1 รายการ'
    cl = _client()
    assert _post_flashes(cl, form) == [(ok, 'success')], "control: the first submit records"
    first = _q(path, "SELECT MAX(id) FROM commission_payouts")[0][0]
    assert _counts(path) == (1, 1)
    assert _post_flashes(cl, form) == [(refusal(first), 'danger')]
    # The refused submit leaves no trace. On this owned connection a refusal
    # raised AFTER the INSERT would be rolled back and look the same (even
    # sqlite_sequence); "before the INSERT" is pinned by probe T and by the
    # in-flight test below, where nothing rolls back for the caller.
    assert _counts(path) == (1, 1)
    assert _q(path, "SELECT seq FROM sqlite_sequence WHERE name = 'commission_payouts'")[0][0] \
        == first


# ── (1b): account check first, tuple check second ────────────────────────────

@pytest.mark.parametrize('flag,text', [('is_active = 0', ACCOUNT_TEXT),
                                       ('is_transfer = 1', TRANSFER_TEXT)])
def test_account_rule_fires_before_the_tuple_rule(db, flag, text):
    path, ids = db
    _record(ids)
    c = sqlite3.connect(path)
    c.execute(f"UPDATE cashbook_accounts SET {flag} WHERE id = ?", (ids['acct_LEX'],))
    c.commit()
    c.close()
    with pytest.raises(ValueError) as e:
        _record(ids, account_id=ids['acct_LEX'])        # the identical tuple
    assert str(e.value) == text
    assert _counts(path) == (1, 1)


# ── (2) / (3) / (5): what is NOT refused ─────────────────────────────────────

@pytest.mark.parametrize('change', [{'amount_paid': 450.5}, {'paid_date': '2026-09-02'},
                                    {'invoice_no': 'IV6999'}, {'salesperson_code': '07'}])
def test_a_different_value_is_recorded(db, change):
    path, ids = db
    if change.get('salesperson_code') == '07':
        c = sqlite3.connect(path)
        c.execute("INSERT INTO salespersons (code, name) VALUES ('07', 'อื่น')")
        c.commit()
        c.close()
    _record(ids)
    _record(ids, **change)
    assert _counts(path) == (2, 2)


def test_backfill_shape_account_none_is_exempt(db):
    path, ids = db
    _record(ids, account_id=None)
    _record(ids, account_id=None)
    assert _counts(path) == (2, 0)


def test_mode2_same_values_different_year_month_both_recorded(db):
    """D-4.1 A: year_month is in the key, so two months paid the same amount
    on the same day are two payouts."""
    path, ids = db
    _record(ids, invoice_no=None, year_month='2026-07', amount_paid=300.0)
    _record(ids, invoice_no=None, year_month='2026-08', amount_paid=300.0)
    assert _counts(path) == (2, 2)


def test_mode1_and_mode2_with_the_same_values_are_different_keys(db):
    """invoice_no IS ? is NULL-safe: an invoice payout does not block the
    per-salesperson payout of the same amount and day, nor the reverse."""
    path, ids = db
    _record(ids)
    _record(ids, invoice_no=None)
    assert _counts(path) == (2, 2)


# ── the guard's population: every existing payout, backfill rows included ────

def test_a_route_repeat_of_a_backfill_tuple_is_refused(db):
    path, ids = db
    backfill = _record(ids, account_id=None)
    with pytest.raises(ValueError) as e:
        _record(ids)
    assert str(e.value) == refusal(backfill)
    assert _counts(path) == (1, 0)


# ── adopt mode (the #589 script's shape): refused, nothing written, txn left ─

def test_refusal_inside_a_caller_transaction_writes_nothing(db):
    path, ids = db
    first = _record(ids)
    conn = commission._connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError) as e:
            _record(ids, conn=conn)
        assert str(e.value) == refusal(first)
        assert conn.in_transaction, "the caller owns its transaction"
        assert conn.execute("SELECT COUNT(*) FROM commission_payouts").fetchone()[0] == 1
        conn.rollback()
    finally:
        conn.close()


# ── (6) probe T: BEGIN → account SELECT → tuple SELECT → payout INSERT → row ─

def test_probe_t_the_tuple_read_is_under_the_lock_and_before_the_insert(db):
    path, ids = db
    conn = commission._connect(path)
    stmts = []
    conn.set_trace_callback(stmts.append)
    try:
        _record(ids, conn=conn)
    finally:
        conn.close()
    up = [s.strip().upper() for s in stmts if not s.strip().upper().startswith('PRAGMA')]

    def first(pred):
        hits = [i for i, s in enumerate(up) if pred(s)]
        assert hits, "control: the statement ran"
        return hits[0]

    begin = first(lambda s: s == 'BEGIN IMMEDIATE')
    acct = first(lambda s: s.startswith('SELECT') and 'FROM CASHBOOK_ACCOUNTS' in s)
    # the FIRST commission_payouts SELECT carrying the NULL-safe key: post_commission
    # re-reads the payout by id AFTER the insert, which must not satisfy this
    tup = first(lambda s: s.startswith('SELECT') and 'FROM COMMISSION_PAYOUTS' in s
                and 'INVOICE_NO IS ' in s)
    ins_p = first(lambda s: s.startswith('INSERT INTO COMMISSION_PAYOUTS'))
    ins_c = first(lambda s: s.startswith('INSERT INTO CASHBOOK_TRANSACTIONS'))
    assert begin == 0, up[:3]
    assert begin < acct < tup < ins_p < ins_c, (begin, acct, tup, ins_p, ins_c)


# ── (6) probe A: the refusal's read and a competing write cannot interleave ──

def test_probe_a_a_competing_identical_payout_waits_for_the_lock(db, monkeypatch):
    """Another worker trying to insert the identical payout while this one
    decides must be blocked: the guard's read and write are one transaction."""
    path, ids = db
    real = database.begin_immediate
    seen = []

    def probe(conn):
        real(conn)
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO commission_payouts (year_month, salesperson_code, "
                          "amount_paid, paid_date, invoice_no) "
                          "VALUES ('2026-08', ?, 450.0, '2026-09-01', 'IV6901')", (ids['sp'],))
            other.commit()
            seen.append('went through')
        except sqlite3.OperationalError as err:
            seen.append('blocked' if 'locked' in str(err) else str(err))
        finally:
            other.close()

    monkeypatch.setattr(database, 'begin_immediate', probe)
    _record(ids)
    assert seen == ['blocked']
    assert _counts(path) == (1, 1)
