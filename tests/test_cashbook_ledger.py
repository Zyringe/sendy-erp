"""Card F PR-1: `cashbook_ledger`, unit-tested on the scenario DB (plan §3b, §5 PR-1).

No route or writer calls the module yet, so "a pure move" is proven here writer
by writer: the CURRENT code (route / hr / commission / mirror) runs on one copy
of the scenario DB, the ledger function on an identical copy, and the resulting
rows (every column but `created_at`) and explicit `audit_log` rows must be equal.
Refusals are compared the same way, by message text.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import ast
import json
import math
import sqlite3

import pytest

import cashbook_seam_scenario as scn
import database

import cashbook_ledger as ledger

ADMIN = {'user_id': 1, 'username': 'admin', 'display_name': 'Administrator', 'role': 'admin'}
ACTOR = ADMIN['display_name']


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def template(tmp_path_factory):
    path = str(tmp_path_factory.mktemp('scenario') / 'template.db')
    scn.build_db(path)
    c = database._connect(path)
    ids = scn.seed(c)
    c.close()
    return path, ids


def _copy(src, dst):
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    s.close()
    d.close()
    return dst


@pytest.fixture
def db(template, tmp_path, monkeypatch):
    """One scenario copy, every module snapshot redirected at it."""
    path = _copy(template[0], str(tmp_path / 'a.db'))
    scn.redirect(monkeypatch.setattr, path)
    return path, template[1]


@pytest.fixture
def pair(template, tmp_path, monkeypatch):
    """Two identical copies: `old` (redirected, where today's code runs) and
    `new` (where the ledger runs through its own connection)."""
    old = _copy(template[0], str(tmp_path / 'old.db'))
    new = _copy(template[0], str(tmp_path / 'new.db'))
    scn.redirect(monkeypatch.setattr, old)
    return old, new, template[1]


def _conn(path, fk=True):
    c = database._connect(path)
    if not fk:
        c.execute("PRAGMA foreign_keys = OFF")
    return c


def _client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(ADMIN)
    return c


def _state(path):
    """Every row the seam can touch, `created_at` stripped, ids kept."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = {}
    try:
        for table, where in (('cashbook_transactions', ''), ('salary_advances', ''),
                             ('commission_payouts', ''),
                             ('audit_log', "WHERE table_name = 'cashbook_transactions'")):
            rows = c.execute(f"SELECT * FROM {table} {where} ORDER BY id").fetchall()
            out[table] = [{k: r[k] for k in r.keys() if k != 'created_at'} for r in rows]
    finally:
        c.close()
    return out


def _assert_same_state(old, new):
    a, b = _state(old), _state(new)
    for table in a:
        assert a[table] == b[table], f"{table} differs between today's code and the ledger"


def _manual_row(path, ids, **over):
    vals = dict(account_id=ids['acct_392'], txn_date='2026-08-20', direction='expense',
                category=scn.MANUAL_CATEGORY, user_category='ร้านเอ', amount=250.0,
                description='เติมน้ำมัน', note=None, created_by=ACTOR)
    vals.update(over)
    c = sqlite3.connect(path)
    cols = ', '.join(vals)
    txn = c.execute(f"INSERT INTO cashbook_transactions ({cols}) VALUES "
                    f"({', '.join('?' * len(vals))})", tuple(vals.values())).lastrowid
    c.commit()
    c.close()
    return txn


def _write(path, sql, params=()):
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(sql, params)
    c.commit()
    c.close()


def _ledger(path, fn, *args, fk=True, **kw):
    """Run one ledger writer the way PR-2/PR-3 callers will: inside
    database.immediate on a connection of its own."""
    c = _conn(path, fk=fk)
    try:
        with database.immediate(c):
            return fn(c, *args, **kw)
    finally:
        c.close()


# ── module shape ────────────────────────────────────────────────────────────

def test_ledger_imports_no_hr_commission_blueprint_mirror_or_flask():
    """§3a: any ledger → hr edge is an import cycle; the ledger has no Flask."""
    tree = ast.parse(open(ledger.__file__, encoding='utf-8').read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or '').split('.')[0])
    assert names, "the AST walk found no imports at all"
    assert names <= {'__future__', 'json', 'math', 're', 'sqlite3', 'datetime', 'typing',
                     'database'}, names


def test_constants_and_exception_are_shared_not_copied():
    import cashbook_payout_mirror as mirror
    from blueprints import cashbook as bp
    assert mirror.CashbookPayoutMirrorError is ledger.CashbookPayoutMirrorError
    for name in ('PLATFORM_ACCOUNT_CODE', 'PLATFORM_LABEL_TH', 'PAYOUT_CATEGORY',
                 'PAYOUT_CREATED_BY', 'MIN_DEPOSIT_DATE'):
        assert getattr(mirror, name) is getattr(ledger, name), name
    for name in ('ADVANCE_CATEGORY', 'SALARY_CATEGORY', 'COMMISSION_CATEGORY'):
        assert getattr(bp, name) is getattr(ledger, name), name
    assert bp.TRANSFER_CATEGORIES == ("เงินทุน/เงินโอน",), "TRANSFER_CATEGORIES stays in the blueprint"
    assert (scn.ADVANCE_CATEGORY, scn.SALARY_CATEGORY, scn.COMMISSION_CATEGORY,
            scn.PAYOUT_CATEGORY) == (ledger.ADVANCE_CATEGORY, ledger.SALARY_CATEGORY,
                                     ledger.COMMISSION_CATEGORY, ledger.PAYOUT_CATEGORY)


# ── row_kind / lock_reason ─────────────────────────────────────────────────

_LINKS = {
    'manual': {},
    'salary': {'payroll_item_id': 1},
    'advance': {'salary_advance_id': 1},
    'commission': {'commission_payout_id': 1},
    'payout': {'payout_platform': 'shopee'},
}


def _row(kind):
    base = {'payroll_item_id': None, 'salary_advance_id': None,
            'commission_payout_id': None, 'payout_platform': None}
    base.update(_LINKS[kind])
    return base


@pytest.mark.parametrize('kind', list(_LINKS))
def test_row_kind(kind):
    assert ledger.row_kind(_row(kind)) == kind


def _blueprint_flash(reject, row, role='admin'):
    """What today's `_reject_if_*` flashes for `row`, or None if it lets it through."""
    import flask
    from werkzeug.exceptions import Forbidden
    from app import app as flask_app
    with flask_app.test_request_context():
        flask.session['role'] = role
        try:
            reject(row)
        except Forbidden:
            msgs = flask.get_flashed_messages()
            assert len(msgs) == 1
            return msgs[0]
        assert flask.get_flashed_messages() == []
        return None


@pytest.mark.parametrize('role', ['admin', 'manager'])
@pytest.mark.parametrize('kind', list(_LINKS))
@pytest.mark.parametrize('action', ['edit', 'delete'])
def test_lock_reason_matches_the_blueprint_byte_for_byte(action, kind, role):
    import access_control
    from blueprints import cashbook as bp
    rejects = ([bp._reject_if_salary_row, bp._reject_if_advance_edit,
                bp._reject_if_commission_row, bp._reject_if_payout_row] if action == 'edit'
               else [bp._reject_if_salary_row, bp._reject_if_commission_row,
                     bp._reject_if_payout_row])
    row = _row(kind)
    today = None
    for reject in rejects:
        today = _blueprint_flash(reject, row, role)
        if today is not None:
            break
    can_cancel = access_control.role_can_post(role, 'commission.commission_delete_payout')
    got = ledger.lock_reason(row, action, can_cancel_commission=can_cancel)
    assert got == today
    if kind == 'manual' or (kind == 'advance' and action == 'delete'):
        assert got is None
    else:
        assert got, "a linked row must be locked"


def test_commission_lock_wording_differs_by_role():
    """Control for the parametrised test: both wordings exist and differ, so
    the role really reaches the text."""
    row = _row('commission')
    a = ledger.lock_reason(row, 'edit', can_cancel_commission=True)
    b = ledger.lock_reason(row, 'edit', can_cancel_commission=False)
    assert a != b and 'ยกเลิกได้ที่หน้าคอมมิชชั่นเท่านั้น' in a and 'ให้แอดมินยกเลิก' in b


# ── every writer refuses to run outside a transaction ──────────────────────

_WRITERS = {
    'post_manual': lambda c: ledger.post_manual(
        c, account_id=1, txn_date='2026-08-01', direction='expense', category='x',
        user_category='', raw_user_category='', amount=1, description='', note='', actor='a'),
    'post_advance': lambda c: ledger.post_advance(
        c, account_id=1, txn_date='2026-08-01', employee_id=1, amount=1, description='',
        note='', actor='a'),
    'amend_manual': lambda c: ledger.amend_manual(
        c, 1, account_id=1, txn_date='2026-08-01', direction='expense', category='x',
        user_category='', raw_user_category='', amount=1, description='', note='', actor='a'),
    'cancel_manual': lambda c: ledger.cancel_manual(c, 1, actor='a'),
    'post_salary': lambda c: ledger.post_salary(c, item_id=1, account_id=1, pay_date=None, actor='a'),
    'cancel_salary': lambda c: ledger.cancel_salary(c, item_id=1, actor='a'),
    'post_commission': lambda c: ledger.post_commission(c, payout_id=1, account_id=1, actor='a'),
    'cancel_commission': lambda c: ledger.cancel_commission(c, payout_id=1, actor='a'),
    'post_payout': lambda c: ledger.post_payout(
        c, platform='shopee', deposit_date='2026-08-15', amount=1500.0, occurrence=1),
    'cancel_payout': lambda c: ledger.cancel_payout(c, txn_id=1),
    'set_payout_description': lambda c: ledger.set_payout_description(c, txn_id=1),
}


@pytest.mark.parametrize('name', sorted(_WRITERS))
def test_every_writer_refuses_outside_a_transaction(db, name):
    path, _ = db
    before = _state(path)
    c = _conn(path)
    try:
        assert not c.in_transaction
        with pytest.raises(ledger.NotInTransaction):
            _WRITERS[name](c)
        assert not c.in_transaction, "the writer must not have opened a transaction"
    finally:
        c.close()
    assert _state(path) == before


# ── manual ─────────────────────────────────────────────────────────────────

def _new_form(ids, **row):
    form = {'account_id': str(ids['acct_392']), 'txn_date': '2026-08-20',
            'rows-0-direction': 'expense', 'rows-0-category': scn.MANUAL_CATEGORY,
            'rows-0-user_category': 'ร้านเอ', 'rows-0-amount': '250',
            'rows-0-description': 'เติมน้ำมัน', 'rows-0-note': '', 'rows-0-employee_id': '',
            'confirm_advance_cap': '1'}
    form.update({f'rows-0-{k}': v for k, v in row.items()})
    return form


def test_post_manual_equals_the_route(pair):
    old, new, ids = pair
    r = _client().post('/cashbook/new', data=_new_form(ids, note='หมายเหตุ'))
    assert r.status_code == 302, r.data[:300]
    assert len(_state(old)['cashbook_transactions']) == 2, "the route must have inserted a row"
    _ledger(new, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
            direction='expense', category=scn.MANUAL_CATEGORY, user_category='ร้านเอ',
            raw_user_category='ร้านเอ', amount=250.0, description='เติมน้ำมัน', note='หมายเหตุ',
            actor=ACTOR)
    _assert_same_state(old, new)


def test_post_manual_blank_fields_store_null_like_the_route(pair):
    old, new, ids = pair
    r = _client().post('/cashbook/new', data=_new_form(ids, user_category='', description=''))
    assert r.status_code == 302
    _ledger(new, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
            direction='expense', category=scn.MANUAL_CATEGORY, user_category='',
            raw_user_category='', amount=250.0, description='', note='', actor=ACTOR)
    _assert_same_state(old, new)
    assert _state(new)['cashbook_transactions'][-1]['user_category'] is None


def test_post_manual_writes_no_explicit_audit_row(db):
    path, ids = db
    txn = _ledger(path, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
                  direction='expense', category=scn.MANUAL_CATEGORY, user_category='',
                  raw_user_category='', amount=1.0, description='', note='', actor=ACTOR)
    audit = [a for a in _state(path)['audit_log'] if a['row_id'] == txn]
    assert len(audit) == 1, "the mig-076 trigger row, and nothing else"
    assert audit[0]['user'] is None


@pytest.mark.parametrize('category,raw_tag,kind', [
    (scn.SALARY_CATEGORY, '', 'salary'),
    (scn.ADVANCE_CATEGORY, 'ชาย', 'advance_on_manual'),
    (scn.COMMISSION_CATEGORY, 'เจียรนัย', 'commission_in_engine'),
])
def test_post_manual_backstops(db, category, raw_tag, kind):
    path, ids = db
    before = _state(path)
    with pytest.raises(ledger.PolicyBlocked) as caught:
        _ledger(path, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
                direction='expense', category=category, user_category=raw_tag,
                raw_user_category=raw_tag, amount=100.0, description='', note='', actor=ACTOR)
    assert caught.value.kind == kind
    if kind == 'commission_in_engine':
        assert caught.value.rep['code'] == '06'
    assert _state(path) == before


def test_post_manual_off_system_commission_passes(db):
    """Control for the backstop: the hybrid rule lets an off-system rep through."""
    path, ids = db
    txn = _ledger(path, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
                  direction='expense', category=scn.COMMISSION_CATEGORY,
                  user_category='อัคเรศ', raw_user_category='อัคเรศ', amount=100.0,
                  description='', note='', actor=ACTOR)
    assert txn


def test_policy_texts_match_the_blueprint():
    """The ledger's plain text is the blueprint's reason; the blueprint adds the
    Markup link to the commission page after it."""
    from app import app as flask_app
    from blueprints import cashbook as bp
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE salespersons (code TEXT, name TEXT, is_active INT, real_name TEXT)")
    c.execute("INSERT INTO salespersons VALUES ('06', 'TOU /06', 1, 'เจียรนัย')")
    with flask_app.test_request_context():
        today = bp._policy_blocked_reason(c, {'category': scn.SALARY_CATEGORY, 'user_category': ''})
        assert str(ledger.policy_block(c, scn.SALARY_CATEGORY, '')) == today
        today = str(bp._policy_blocked_reason(
            c, {'category': scn.COMMISSION_CATEGORY, 'user_category': 'เจียรนัย'}))
        got = ledger.policy_block(c, scn.COMMISSION_CATEGORY, 'เจียรนัย')
        assert today.startswith(str(got) + " — "), (today, str(got))
        today = bp._edit_policy_blocked_reason(c, scn.ADVANCE_CATEGORY, 'ชาย')
        got = ledger.edit_policy_block(c, scn.ADVANCE_CATEGORY, 'ชาย')
        assert str(got) == today and got.kind == 'advance_on_edit'
        assert ledger.policy_block(c, scn.MANUAL_CATEGORY, 'เจียรนัย') is None
        assert ledger.policy_block(c, scn.COMMISSION_CATEGORY, 'อัคเรศ') is None


@pytest.mark.parametrize('field,value,text', [
    ('amount', 0, "จำนวนเงินต้องมากกว่า 0"),
    ('amount', -5, "จำนวนเงินต้องมากกว่า 0"),
    ('amount', 'abc', "จำนวนเงินต้องมากกว่า 0"),
    ('txn_date', '31/08/2569', "รูปแบบวันที่ไม่ถูกต้อง"),
    ('txn_date', '', "กรุณาระบุวันที่"),
    ('direction', 'out', "ประเภทไม่ถูกต้อง"),
    ('category', '', "กรุณาระบุหมวดหมู่"),
    ('account_id', 9999, "กรุณาเลือกบัญชีที่ถูกต้องและยังใช้งานอยู่"),
    ('category', 'หมวดที่ไม่มี', None),
])
def test_post_manual_field_rules_are_todays(db, field, value, text):
    path, ids = db
    kw = dict(account_id=ids['acct_392'], txn_date='2026-08-20', direction='expense',
              category=scn.MANUAL_CATEGORY, user_category='', raw_user_category='',
              amount=1.0, description='', note='', actor=ACTOR)
    kw[field] = value
    before = _state(path)
    with pytest.raises(ledger.CashbookError) as caught:
        _ledger(path, ledger.post_manual, **kw)
    if text:
        assert str(caught.value) == text
    assert _state(path) == before


def test_post_manual_refuses_an_inactive_category(db):
    path, ids = db
    _write(path, "UPDATE cashbook_categories SET is_active = 0 WHERE name = ?",
           (scn.MANUAL_CATEGORY,))
    with pytest.raises(ledger.CashbookError):
        _ledger(path, ledger.post_manual, account_id=ids['acct_392'], txn_date='2026-08-20',
                direction='expense', category=scn.MANUAL_CATEGORY, user_category='',
                raw_user_category='', amount=1.0, description='', note='', actor=ACTOR)


def test_post_manual_inf_passes_and_nan_raises_as_today(db):
    """Today's rule is `not amount <= 0`: inf passes; nan passes the check,
    binds NULL and the NOT NULL constraint raises (plan §3b, D-1 parked)."""
    path, ids = db
    kw = dict(account_id=ids['acct_392'], txn_date='2026-08-20', direction='expense',
              category=scn.MANUAL_CATEGORY, user_category='', raw_user_category='',
              description='', note='', actor=ACTOR)
    txn = _ledger(path, ledger.post_manual, amount=float('inf'), **kw)
    c = sqlite3.connect(path)
    assert math.isinf(c.execute("SELECT amount FROM cashbook_transactions WHERE id=?",
                                (txn,)).fetchone()[0])
    c.close()
    with pytest.raises(sqlite3.IntegrityError):
        _ledger(path, ledger.post_manual, amount=float('nan'), **kw)


# ── advance ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('emp_key', ['emp1', 'emp2'])
def test_post_advance_equals_the_route(pair, emp_key):
    """emp2 has nickname '': COALESCE keeps '' (not the full name), and the
    route then stores NULL for the tag."""
    old, new, ids = pair
    form = _new_form(ids, category=scn.ADVANCE_CATEGORY, employee_id=str(ids[emp_key]),
                     user_category='', amount='700', note='ขอเบิก', description='')
    form['txn_date'] = '2026-09-05'
    r = _client().post('/cashbook/new', data=form)
    assert r.status_code == 302, r.data[:300]
    assert len(_state(old)['salary_advances']) == 2, "the route must have written the advance"
    adv_id, txn_id = _ledger(new, ledger.post_advance, account_id=ids['acct_392'],
                             txn_date='2026-09-05', employee_id=ids[emp_key], amount=700.0,
                             description='', note='ขอเบิก', actor=ACTOR)
    _assert_same_state(old, new)
    st = _state(new)
    assert st['cashbook_transactions'][-1]['salary_advance_id'] == adv_id
    assert st['cashbook_transactions'][-1]['user_category'] == ('ชาย' if emp_key == 'emp1' else None)


def test_post_advance_refuses_an_inactive_employee(db):
    path, ids = db
    _write(path, "UPDATE employees SET is_active = 0 WHERE id = ?", (ids['emp2'],))
    before = _state(path)
    with pytest.raises(ledger.CashbookError, match='กรุณาเลือกพนักงานสำหรับรายการเบิกล่วงหน้า'):
        _ledger(path, ledger.post_advance, account_id=ids['acct_392'], txn_date='2026-09-05',
                employee_id=ids['emp2'], amount=700.0, description='', note='', actor=ACTOR)
    assert _state(path) == before


# ── amend / cancel manual ──────────────────────────────────────────────────

def _edit_form(ids, **over):
    form = {'account_id': str(ids['acct_LEX']), 'txn_date': '2026-08-21',
            'direction': 'expense', 'category': scn.MANUAL_CATEGORY,
            'user_category': 'ร้านบี', 'amount': '260.5', 'description': 'แก้แล้ว', 'note': ''}
    form.update(over)
    return form


def _amend_kw(ids, **over):
    kw = dict(account_id=ids['acct_LEX'], txn_date='2026-08-21', direction='expense',
              category=scn.MANUAL_CATEGORY, user_category='ร้านบี', raw_user_category='ร้านบี',
              amount=260.5, description='แก้แล้ว', note='', actor=ACTOR)
    kw.update(over)
    return kw


def test_amend_manual_equals_the_route(pair):
    old, new, ids = pair
    t_old, t_new = _manual_row(old, ids), _manual_row(new, ids)
    assert t_old == t_new
    r = _client().post(f'/cashbook/txn/{t_old}/edit', data=_edit_form(ids))
    assert r.status_code == 302
    changed = _ledger(new, ledger.amend_manual, t_new, **_amend_kw(ids))
    _assert_same_state(old, new)
    assert set(changed) == {'account_id', 'txn_date', 'user_category', 'amount', 'description'}
    explicit = [a for a in _state(new)['audit_log'] if a['user'] == ACTOR]
    assert len(explicit) == 1 and json.loads(explicit[0]['changed_fields']) == changed


def test_amend_manual_unchanged_writes_no_explicit_audit_row(pair):
    old, new, ids = pair
    t_old, t_new = _manual_row(old, ids), _manual_row(new, ids)
    same = dict(account_id=str(ids['acct_392']), txn_date='2026-08-20', user_category='ร้านเอ',
                amount='250', description='เติมน้ำมัน')
    r = _client().post(f'/cashbook/txn/{t_old}/edit', data=_edit_form(ids, **same))
    assert r.status_code == 302
    changed = _ledger(new, ledger.amend_manual, t_new, **_amend_kw(
        ids, account_id=ids['acct_392'], txn_date='2026-08-20', user_category='ร้านเอ',
        raw_user_category='ร้านเอ', amount=250.0, description='เติมน้ำมัน'))
    assert changed == {}
    _assert_same_state(old, new)
    assert not [a for a in _state(new)['audit_log'] if a['user'] == ACTOR]


@pytest.mark.parametrize('kind', ['salary', 'advance', 'commission', 'payout'])
def test_amend_manual_refuses_a_locked_row(db, kind):
    path, ids = db
    if kind == 'advance':
        txn = ids['advance_txn']
    else:
        link = {'salary': ('payroll_item_id', ids['item1']), 'commission':
                ('commission_payout_id', None), 'payout': ('payout_platform', 'shopee')}[kind]
        if kind == 'commission':
            c = sqlite3.connect(path)
            pid = c.execute("INSERT INTO commission_payouts (year_month, salesperson_code, "
                            "amount_paid, paid_date) VALUES ('2026-08', '06', 10, '2026-08-20')"
                            ).lastrowid
            c.commit()
            c.close()
            link = ('commission_payout_id', pid)
        txn = _manual_row(path, ids, **{link[0]: link[1]})
    before = _state(path)
    with pytest.raises(ledger.LockedRow) as caught:
        _ledger(path, ledger.amend_manual, txn, **_amend_kw(ids))
    assert caught.value.kind == kind
    assert _state(path) == before


def test_amend_manual_refuses_editing_into_the_advance_category(db):
    path, ids = db
    txn = _manual_row(path, ids)
    with pytest.raises(ledger.PolicyBlocked) as caught:
        _ledger(path, ledger.amend_manual, txn, **_amend_kw(ids, category=scn.ADVANCE_CATEGORY))
    assert caught.value.kind == 'advance_on_edit'


def test_amend_manual_category_rule_only_on_change(db):
    """Rows sitting in a retired category stay editable (#532)."""
    path, ids = db
    txn = _manual_row(path, ids)
    _write(path, "UPDATE cashbook_categories SET is_active = 0 WHERE name = ?",
           (scn.MANUAL_CATEGORY,))
    _ledger(path, ledger.amend_manual, txn, **_amend_kw(ids))            # unchanged category: ok
    with pytest.raises(ledger.CashbookError, match='หมวดหมู่นี้ไม่มีอยู่หรือถูกปิดใช้งานแล้ว'):
        _ledger(path, ledger.amend_manual, txn, **_amend_kw(ids, category='หมวดที่ไม่มี'))


def test_cancel_manual_equals_the_route(pair):
    old, new, ids = pair
    t_old, t_new = _manual_row(old, ids), _manual_row(new, ids)
    r = _client().post(f'/cashbook/txn/{t_old}/delete')
    assert r.status_code == 302
    row = _ledger(new, ledger.cancel_manual, t_new, actor=ACTOR)
    assert row['id'] == t_new
    _assert_same_state(old, new)
    assert len([a for a in _state(new)['audit_log'] if a['user'] == ACTOR]) == 1


def test_cancel_manual_cascades_an_undeducted_advance_like_the_route(pair):
    old, new, ids = pair
    for path in (old, new):
        _ledger(path, ledger.post_advance, account_id=ids['acct_392'], txn_date='2026-09-05',
                employee_id=ids['emp2'], amount=700.0, description='', note='', actor=ACTOR)
    txn = _state(old)['cashbook_transactions'][-1]['id']
    r = _client().post(f'/cashbook/txn/{txn}/delete')
    assert r.status_code == 302
    assert len(_state(old)['salary_advances']) == 1, "the route must have cascaded"
    _ledger(new, ledger.cancel_manual, txn, actor=ACTOR)
    _assert_same_state(old, new)


def test_cancel_manual_refuses_a_deducted_advance(db):
    path, ids = db
    before = _state(path)
    with pytest.raises(ledger.LockedRow) as caught:
        _ledger(path, ledger.cancel_manual, ids['advance_txn'], actor=ACTOR)
    assert caught.value.kind == 'advance_deducted'
    assert str(caught.value) == "รายการเบิกล่วงหน้านี้ถูกหักในรอบเงินเดือนแล้ว — ลบไม่ได้"
    assert _state(path) == before


@pytest.mark.parametrize('kind', ['salary', 'payout'])
def test_cancel_manual_refuses_other_locked_kinds(db, kind):
    path, ids = db
    link = {'salary': {'payroll_item_id': ids['item1']}, 'payout': {'payout_platform': 'shopee'}}
    txn = _manual_row(path, ids, **link[kind])
    with pytest.raises(ledger.LockedRow) as caught:
        _ledger(path, ledger.cancel_manual, txn, actor=ACTOR)
    assert caught.value.kind == kind


# ── salary ─────────────────────────────────────────────────────────────────

def test_post_salary_equals_hr(pair):
    old, new, ids = pair
    import hr
    hr.post_salary_payment(ids['item1'], ids['acct_392'], '2026-09-01', ACTOR)
    assert len(_state(old)['cashbook_transactions']) == 2
    _ledger(new, ledger.post_salary, item_id=ids['item1'], account_id=ids['acct_392'],
            pay_date='2026-09-01', actor=ACTOR)
    _assert_same_state(old, new)
    row = _state(new)['cashbook_transactions'][-1]
    assert row['amount'] == 15000 and row['payroll_run_id'] == ids['run']


def test_post_salary_empty_nickname_falls_back_to_full_name_like_hr(pair):
    """hr uses `nickname or full_name` (not COALESCE): E2's '' becomes the full name."""
    old, new, ids = pair
    import hr
    hr.post_salary_payment(ids['item2'], ids['acct_392'], '2026-09-01', ACTOR)
    _ledger(new, ledger.post_salary, item_id=ids['item2'], account_id=ids['acct_392'],
            pay_date='2026-09-01', actor=ACTOR)
    _assert_same_state(old, new)
    assert _state(new)['cashbook_transactions'][-1]['user_category'] == 'สมหญิง รักงาน'


def _salary_breakers(ids):
    return {
        'missing item': (lambda p: None, 999999, 'acct_392'),
        'net_pay 0': (lambda p: _write(p, "UPDATE payroll_items SET net_pay=0 WHERE id=?",
                                       (ids['item1'],)), ids['item1'], 'acct_392'),
        'already paid': (lambda p: _manual_row(p, ids, payroll_item_id=ids['item1'],
                                               category=scn.SALARY_CATEGORY),
                         ids['item1'], 'acct_392'),
        'inactive account': (lambda p: _write(p, "UPDATE cashbook_accounts SET is_active=0 "
                                                 "WHERE id=?", (ids['acct_392'],)),
                             ids['item1'], 'acct_392'),
        'transfer account': (lambda p: _write(p, "UPDATE cashbook_accounts SET is_transfer=1 "
                                                 "WHERE id=?", (ids['acct_392'],)),
                             ids['item1'], 'acct_392'),
        'run not finalized': (lambda p: _write(p, "UPDATE payroll_runs SET status='draft' "
                                                  "WHERE id=?", (ids['run'],)),
                              ids['item1'], 'acct_392'),
        # net_pay 0 AND transfer account: today refuses on net_pay first
        'order: net_pay before account': (
            lambda p: (_write(p, "UPDATE payroll_items SET net_pay=0 WHERE id=?", (ids['item1'],)),
                       _write(p, "UPDATE cashbook_accounts SET is_transfer=1 WHERE id=?",
                              (ids['acct_392'],))), ids['item1'], 'acct_392'),
        # transfer account AND draft run: today refuses on the account first
        'order: account before run': (
            lambda p: (_write(p, "UPDATE payroll_runs SET status='draft' WHERE id=?", (ids['run'],)),
                       _write(p, "UPDATE cashbook_accounts SET is_transfer=1 WHERE id=?",
                              (ids['acct_392'],))), ids['item1'], 'acct_392'),
    }


@pytest.mark.parametrize('case', ['missing item', 'net_pay 0', 'already paid', 'inactive account',
                                  'transfer account', 'run not finalized',
                                  'order: net_pay before account', 'order: account before run'])
def test_post_salary_refuses_in_hrs_order_with_hrs_text(pair, case):
    old, new, ids = pair
    import hr
    setup, item, acct = _salary_breakers(ids)[case]
    setup(old)
    setup(new)
    with pytest.raises(ValueError) as today:
        hr.post_salary_payment(item, ids[acct], '2026-09-01', ACTOR)
    with pytest.raises(ledger.CashbookError) as got:
        _ledger(new, ledger.post_salary, item_id=item, account_id=ids[acct],
                pay_date='2026-09-01', actor=ACTOR)
    assert str(got.value) == str(today.value)
    _assert_same_state(old, new)


def _raise_on_insert(path, message):
    c = sqlite3.connect(path)
    c.execute(f"""CREATE TRIGGER seeded_failure BEFORE INSERT ON cashbook_transactions
                  BEGIN SELECT RAISE(ABORT, '{message}'); END""")
    c.commit()
    c.close()


def test_post_salary_maps_only_its_own_unique_failure(db):
    path, ids = db
    _raise_on_insert(path, 'UNIQUE constraint failed: cashbook_transactions.payroll_item_id')
    with pytest.raises(ledger.CashbookError) as caught:
        _ledger(path, ledger.post_salary, item_id=ids['item1'], account_id=ids['acct_392'],
                pay_date='2026-09-01', actor=ACTOR)
    assert str(caught.value) == "รายการนี้ถูกบันทึกจ่ายไปแล้ว — ต้องยกเลิกการจ่ายก่อนจึงจะบันทึกใหม่ได้"


def test_post_salary_reraises_any_other_integrity_error(db):
    path, ids = db
    _raise_on_insert(path, 'CHECK constraint failed: seeded')
    with pytest.raises(sqlite3.IntegrityError, match='CHECK constraint failed: seeded'):
        _ledger(path, ledger.post_salary, item_id=ids['item1'], account_id=ids['acct_392'],
                pay_date='2026-09-01', actor=ACTOR)


def test_cancel_salary_equals_hr(pair):
    old, new, ids = pair
    import hr
    for path in (old, new):
        _ledger(path, ledger.post_salary, item_id=ids['item1'], account_id=ids['acct_392'],
                pay_date='2026-09-01', actor=ACTOR)
    hr.void_salary_payment(ids['item1'], ACTOR)
    assert len(_state(old)['cashbook_transactions']) == 1, "hr must have voided the row"
    txn = _ledger(new, ledger.cancel_salary, item_id=ids['item1'], actor=ACTOR)
    assert txn
    _assert_same_state(old, new)
    assert _ledger(new, ledger.cancel_salary, item_id=ids['item1'], actor=ACTOR) is None


# ── commission ─────────────────────────────────────────────────────────────

def _insert_payout(path, sp='06', amount=1234.5, paid_date='2026-09-02', ym='2026-08',
                   invoice='IV6908-001', paid_by=ACTOR):
    c = sqlite3.connect(path)
    pid = c.execute("""INSERT INTO commission_payouts (year_month, salesperson_code, amount_paid,
                       paid_date, paid_method, note, paid_by, invoice_no)
                       VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)""",
                    (ym, sp, amount, paid_date, paid_by or None, invoice)).lastrowid
    c.commit()
    c.close()
    return pid


@pytest.mark.parametrize('sp,paid_by', [('06', ACTOR), ('ZZ', ''), ('06', '')])
def test_post_commission_equals_record_payout(pair, sp, paid_by):
    """'ZZ' is not in salespersons (FKs are off on commission's connection):
    the description falls back to the code, as today. paid_by '' → created_by
    NULL and audit user ''."""
    old, new, ids = pair
    import commission
    commission.record_payout('2026-08', sp, 1234.5, '2026-09-02', paid_by=paid_by,
                             invoice_no='IV6908-001', account_id=ids['acct_392'])
    assert len(_state(old)['commission_payouts']) == 1
    pid = _insert_payout(new, sp=sp, paid_by=paid_by)
    _ledger(new, ledger.post_commission, payout_id=pid, account_id=ids['acct_392'],
            actor=paid_by, fk=False)
    _assert_same_state(old, new)


@pytest.mark.parametrize('flag', ['is_active=0', 'is_transfer=1'])
def test_post_commission_account_backstop_has_commissions_text(pair, flag):
    old, new, ids = pair
    import commission
    for path in (old, new):
        _write(path, f"UPDATE cashbook_accounts SET {flag} WHERE id=?", (ids['acct_392'],))
    with pytest.raises(ValueError) as today:
        commission.record_payout('2026-08', '06', 10.0, '2026-09-02', paid_by=ACTOR,
                                 account_id=ids['acct_392'])
    pid = _insert_payout(new)
    with pytest.raises(ledger.CashbookError) as got:
        _ledger(new, ledger.post_commission, payout_id=pid, account_id=ids['acct_392'],
                actor=ACTOR, fk=False)
    assert str(got.value) == str(today.value)


def test_cancel_commission_equals_delete_payout(pair):
    old, new, ids = pair
    import commission
    pid = commission.record_payout('2026-08', '06', 50.0, '2026-09-02', paid_by=ACTOR,
                                   account_id=ids['acct_392'])
    pid2 = _insert_payout(new, amount=50.0, invoice=None)
    assert pid == pid2
    _ledger(new, ledger.post_commission, payout_id=pid2, account_id=ids['acct_392'],
            actor=ACTOR, fk=False)
    _assert_same_state(old, new)
    commission.delete_payout(pid, actor='ผู้ยกเลิก')
    txn = _ledger(new, ledger.cancel_commission, payout_id=pid2, actor='ผู้ยกเลิก', fk=False)
    assert txn
    _write(new, "DELETE FROM commission_payouts WHERE id=?", (pid2,))  # delete_payout's own part
    _assert_same_state(old, new)
    assert _ledger(new, ledger.cancel_commission, payout_id=pid2, actor='x', fk=False) is None


# ── payout ─────────────────────────────────────────────────────────────────

def test_payout_target_equals_the_mirrors(db):
    import cashbook_payout_mirror as mirror
    path, ids = db
    _write(path, "INSERT INTO marketplace_payouts (platform, deposit_date, amount, n_orders) "
                 "VALUES ('shopee', '2025-12-31', 99.0, 1)")   # before MIN_DEPOSIT_DATE
    c = _conn(path)
    try:
        target = ledger.payout_target(c, 'shopee')
        assert len(target) == 2, "the pre-2026 payout must be out, the duplicate pair in"
        assert target == mirror._target_payouts(c, 'shopee')
        assert ledger.payout_target(c, 'lazada') == {}
    finally:
        c.close()


def test_post_payout_equals_the_mirror(pair):
    old, new, ids = pair
    import cashbook_payout_mirror as mirror
    c = _conn(old)
    assert mirror.mirror_platform(c, 'shopee')['inserted'] == 2
    c.close()
    for occ in (1, 2):
        _ledger(new, ledger.post_payout, platform='shopee', deposit_date='2026-08-15',
                amount=1500.0, occurrence=occ)
    _assert_same_state(old, new)


@pytest.mark.parametrize('key', [('2026-08-15', 1500.0, 3), ('2026-08-16', 1500.0, 1),
                                 ('2026-08-15', 1499.99, 1)])
def test_post_payout_refuses_a_key_not_in_marketplace_payouts(db, key):
    path, _ = db
    before = _state(path)
    with pytest.raises(ledger.CashbookPayoutMirrorError):
        _ledger(path, ledger.post_payout, platform='shopee', deposit_date=key[0],
                amount=key[1], occurrence=key[2])
    assert _state(path) == before


def test_post_payout_account_texts_are_the_mirrors(db):
    import cashbook_payout_mirror as mirror
    path, ids = db
    _write(path, "UPDATE cashbook_accounts SET is_active=0 WHERE code='SPX'")
    for platform in ('shopee', 'tiktok'):
        c = _conn(path)
        with pytest.raises(mirror.CashbookPayoutMirrorError) as today:
            mirror._resolve_account_id(c, platform)
        c.close()
        with pytest.raises(ledger.CashbookPayoutMirrorError) as got:
            _ledger(path, ledger.post_payout, platform=platform, deposit_date='2026-08-15',
                    amount=1500.0, occurrence=1)
        assert str(got.value) == str(today.value)


def test_cancel_payout_and_set_description_equal_the_mirror(pair):
    """Remove one of the pair and change the survivor's n_orders, then let the
    mirror diff it; the ledger does the same by hand."""
    old, new, ids = pair
    import cashbook_payout_mirror as mirror
    for path in (old, new):
        c = _conn(path)
        mirror.mirror_platform(c, 'shopee')
        c.close()
        _write(path, "DELETE FROM marketplace_payouts WHERE id = (SELECT MAX(id) FROM "
                     "marketplace_payouts)")
        _write(path, "UPDATE marketplace_payouts SET n_orders = 9")
    c = _conn(old)
    res = mirror.mirror_platform(c, 'shopee')
    c.close()
    assert (res['deleted'], res['updated']) == (1, 1)
    rows = [r for r in _state(new)['cashbook_transactions'] if r['payout_platform']]
    assert len(rows) == 2
    _ledger(new, ledger.cancel_payout, txn_id=rows[1]['id'])
    _ledger(new, ledger.set_payout_description, txn_id=rows[0]['id'])
    _assert_same_state(old, new)


def test_payout_writers_refuse_a_non_payout_row(db):
    path, ids = db
    txn = _manual_row(path, ids)
    before = _state(path)
    for fn in (ledger.cancel_payout, ledger.set_payout_description):
        with pytest.raises(ledger.CashbookError):
            _ledger(path, fn, txn_id=txn)
    assert _state(path) == before
