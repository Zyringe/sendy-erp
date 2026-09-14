"""Employee identity values are STORED canonical (#464, spec #460).

The display half shipped in #463: `filters.py` turns bare digits into what a
human reads. This is the storage half. Whatever an admin types into the bank
account, phone or national-ID box — dashes or not, spaces, parentheses, a
line pasted from a chat — what reaches `employees` is bare digits, and a field
left blank is SQL NULL, never ''. Before this, "never entered" (NULL) and
"cleared" ('') were two states no query could tell apart, and nothing
normalized on write, so new drift kept arriving.

One function decides it — `hr_queries._identity_to_digits`, mutating the
mapping in place exactly like its neighbour `_blank_dates_to_none` — and both
employee write paths call it from inside the data-access layer, so no caller
can reach the SQL around it.

Every value here is invented. None is a real national ID, phone or account.
"""
import os

os.environ.setdefault('SKIP_DB_INIT', '1')

import pytest

import hr_queries as hrq

FIELDS = ('national_id', 'phone', 'bank_account_no')


# ── the normalizer, tested directly on the data mapping ─────────────────────

@pytest.mark.parametrize('field, typed, stored', [
    ('national_id',     '1-2345-67890-12-3',  '1234567890123'),
    ('national_id',     '1 2345 67890 12 3',  '1234567890123'),
    ('phone',           '081-234-5678',       '0812345678'),
    ('phone',           '(02) 123 4567',      '021234567'),
    ('phone',           'โทร 081-234-5678',   '0812345678'),   # pasted from a chat
    ('bank_account_no', '123-4-56789-0',      '1234567890'),
    ('bank_account_no', '123.4.56789.0',      '1234567890'),
])
def test_whatever_was_typed_is_stored_as_bare_digits(field, typed, stored):
    data = {field: typed}
    hrq._identity_to_digits(data)
    assert data[field] == stored


@pytest.mark.parametrize('field, value', [
    ('national_id', '1234567890123'),
    ('phone', '0812345678'),
    ('bank_account_no', '1234567890'),
])
def test_an_already_canonical_value_is_left_exactly_as_it_is(field, value):
    data = {field: value}
    hrq._identity_to_digits(data)
    assert data[field] == value


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('blank', ['', '   ', '-', '( )', None])
def test_a_cleared_field_becomes_null_never_an_empty_string(field, blank):
    """'' and NULL were two spellings of 'not recorded'. Only NULL survives.
    A box holding nothing but separators is blank too — there is no number
    in it to keep."""
    data = {field: blank}
    hrq._identity_to_digits(data)
    assert data[field] is None


def test_thai_numerals_are_stored_as_the_same_digits():
    """A Thai document or a Thai keyboard can hand over ๐-๙. Those are digits,
    not separators: stripping them would silently store NULL (or half a
    number) for an ID that was typed correctly."""
    data = {'national_id': '๑-๒๓๔๕-๖๗๘๙๐-๑๒-๓', 'phone': '๐๘๑-๒๓๔-๕๖๗๘'}
    hrq._identity_to_digits(data)
    assert data == {'national_id': '1234567890123', 'phone': '0812345678'}


def test_only_the_three_identity_fields_are_touched():
    """Digits inside a name, an address or an account holder's name are part
    of that text. The CONTROL is the identity field changing in the same call,
    so an implementation that touched nothing cannot pass."""
    data = {'full_name': 'นาย ทดสอบ 2', 'bank_account_name': 'บ-ช 1',
            'address': '12/3 ม.4', 'emp_code': 'EMP-01', 'note': '',
            'national_id': '1-2345-67890-12-3'}
    hrq._identity_to_digits(data)
    assert data['national_id'] == '1234567890123', "CONTROL — the call did run"
    assert data == {'full_name': 'นาย ทดสอบ 2', 'bank_account_name': 'บ-ช 1',
                    'address': '12/3 ม.4', 'emp_code': 'EMP-01', 'note': '',
                    'national_id': '1234567890123'}


# ── both write paths are wired to it ────────────────────────────────────────
# `tmp_db_conn` clones the dev DB WITH its employees, so every test makes its
# own row and reads back only that row's id. The row's identity values are
# written with raw SQL when the test needs a known "before" state, because the
# app's own writer is the thing under test.

DASHED = {'national_id': '1-2345-67890-12-3', 'phone': '(081) 234-5678',
          'bank_account_no': '123-4-56789-0'}
CANONICAL = {'national_id': '1234567890123', 'phone': '0812345678',
             'bank_account_no': '1234567890'}

_code = [0]


def _fresh_code(prefix):
    """A non-EMP code: autoincrement picks the id, no collision with the
    Phase-2 id==EMP-number rule or with the cloned dev DB's real staff."""
    _code[0] += 1
    return f'T464{prefix}{_code[0]}'


def _identity(conn, emp_id):
    row = conn.execute(
        "SELECT national_id, phone, bank_account_no FROM employees WHERE id=?",
        (emp_id,)).fetchone()
    assert row is not None, "CONTROL — the employee row exists"
    return {k: row[k] for k in FIELDS}


def _seed(conn, **identity):
    """An employee whose identity columns hold EXACTLY `identity`, written
    around the app so the starting state is not itself normalized."""
    code = _fresh_code('S')
    cur = conn.execute(
        "INSERT INTO employees (emp_code, full_name, company_id, national_id,"
        " phone, bank_name, bank_account_no) VALUES (?,?,?,?,?,?,?)",
        (code, 'ทดสอบ ตัวตน', 1, identity.get('national_id'),
         identity.get('phone'), 'ธนาคารกสิกรไทย', identity.get('bank_account_no')))
    conn.commit()
    return cur.lastrowid, code


def test_create_employee_stores_bare_digits(tmp_db_conn):
    emp_id = hrq.create_employee(
        {'emp_code': _fresh_code('C'), 'full_name': 'ทดสอบ สร้าง',
         'company_id': 1, **DASHED}, conn=tmp_db_conn)
    assert _identity(tmp_db_conn, emp_id) == CANONICAL


def test_create_employee_with_initial_salary_stores_bare_digits(tmp_db_conn):
    """The function the new-employee page actually calls — it shares
    `_insert_employee` with `create_employee`, which is the point."""
    emp_id = hrq.create_employee_with_initial_salary(
        {'emp_code': _fresh_code('W'), 'full_name': 'ทดสอบ สร้างพร้อมเงินเดือน',
         'company_id': 1, 'start_date': '2026-03-01', **DASHED},
        '15000', '2026-03-01', conn=tmp_db_conn)
    tmp_db_conn.commit()
    assert _identity(tmp_db_conn, emp_id) == CANONICAL


def test_create_employee_stores_null_for_blank_fields(tmp_db_conn):
    emp_id = hrq.create_employee(
        {'emp_code': _fresh_code('B'), 'full_name': 'ทดสอบ ว่าง',
         'company_id': 1, 'national_id': '', 'phone': ' ', 'bank_account_no': ''},
        conn=tmp_db_conn)
    row = tmp_db_conn.execute(
        "SELECT typeof(national_id) t1, typeof(phone) t2, typeof(bank_account_no) t3"
        "  FROM employees WHERE id=?", (emp_id,)).fetchone()
    assert (row['t1'], row['t2'], row['t3']) == ('null', 'null', 'null')


def test_update_employee_stores_bare_digits_and_null(tmp_db_conn):
    emp_id, code = _seed(tmp_db_conn, national_id='9999999999999',
                         phone='0899999999', bank_account_no='9999999999')
    hrq.update_employee(emp_id, {
        'emp_code': code, 'full_name': 'ทดสอบ ตัวตน', 'company_id': 1,
        'national_id': DASHED['national_id'], 'phone': '',
        'bank_account_no': DASHED['bank_account_no']}, conn=tmp_db_conn)
    assert _identity(tmp_db_conn, emp_id) == {
        'national_id': CANONICAL['national_id'], 'phone': None,
        'bank_account_no': CANONICAL['bank_account_no']}


# ── the routes, end to end ──────────────────────────────────────────────────
# A 302 from a Sendy write route is not evidence the write happened: success,
# a caught exception and a refused request all redirect. Each test therefore
# asserts a change only the route could have made — a row that did not exist,
# or a value replaced by the one this POST carried.

def _admin_client():
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['user_id'] = 1
        s['username'] = 'test-admin'
        s['role'] = 'admin'
    return c


def _open(tmp_db):
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    return conn


def test_new_employee_page_stores_what_was_typed_as_bare_digits(tmp_db):
    code = _fresh_code('R')
    resp = _admin_client().post('/hr/employees/new', data={
        'emp_code': code, 'full_name': 'ทดสอบ หน้าเพิ่ม', 'company_id': '1',
        'start_date': '2026-03-01', 'initial_salary': '',
        'national_id': DASHED['national_id'], 'phone': '',
        'bank_name': 'ธนาคารกสิกรไทย', 'bank_account_no': DASHED['bank_account_no'],
    })
    assert resp.status_code == 302
    conn = _open(tmp_db)
    try:
        row = conn.execute("SELECT id FROM employees WHERE emp_code=?", (code,)).fetchone()
        assert row is not None, "the route created no employee — nothing below means anything"
        assert _identity(conn, row['id']) == {
            'national_id': CANONICAL['national_id'], 'phone': None,
            'bank_account_no': CANONICAL['bank_account_no']}
    finally:
        conn.close()


def test_edit_employee_page_stores_what_was_typed_as_bare_digits(tmp_db):
    conn = _open(tmp_db)
    try:
        emp_id, code = _seed(conn, national_id='9999999999999',
                             phone='0899999999', bank_account_no='9999999999')
    finally:
        conn.close()

    resp = _admin_client().post(f'/hr/employees/{emp_id}/edit', data={
        'emp_code': code, 'full_name': 'ทดสอบ ตัวตน', 'company_id': '1',
        'employment_type': 'monthly', 'sso_enrolled': '1', 'is_active': '1',
        'national_id': DASHED['national_id'], 'phone': '',
        'bank_name': 'ธนาคารกสิกรไทย', 'bank_account_no': DASHED['bank_account_no'],
    })
    assert resp.status_code == 302

    conn = _open(tmp_db)
    try:
        got = _identity(conn, emp_id)
    finally:
        conn.close()
    # The national ID moving off the seeded 999… is the positive change that
    # proves the route wrote at all; the missing dashes prove it normalized.
    assert got['national_id'] == CANONICAL['national_id']
    assert got['bank_account_no'] == CANONICAL['bank_account_no']
    assert got['phone'] is None, "a cleared field is NULL, not ''"
