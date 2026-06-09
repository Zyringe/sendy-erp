"""Tests for the multi-key employee resolver (change B).

Resolver precedence: emp_code → first name (first token of full_name) → nickname
→ full name, first hit wins, ambiguous keys dropped. Fixes the blank-nickname
gap (วฤทธิ์ EMP001 / สันติ EMP003) where advance matching previously failed.
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

from import_cashbook import _build_employee_resolver, _resolve_employee_id


def _emp(conn, code, full, nick):
    conn.execute(
        "INSERT INTO employees (emp_code, full_name, nickname) VALUES (?,?,?)",
        (code, full, nick))


def _ids(conn):
    return {r['emp_code']: r['id'] for r in conn.execute("SELECT id, emp_code FROM employees")}


def test_resolver_all_keys(empty_db_conn):
    c = empty_db_conn
    _emp(c, 'EMP001', 'วฤทธิ์ ปลื้มวรสวัสดิ์', None)   # blank nickname
    _emp(c, 'EMP003', 'สันติ เลิศประเสริญวงศ์', None)   # blank nickname
    _emp(c, 'EMP005', 'วุฒิพงษ์ แปงนุจา', 'บอล')
    _emp(c, 'EMP004', 'วิภา ขมสันเทียะ', 'หลุย')
    c.commit()
    ids = _ids(c)
    R = _build_employee_resolver(c)

    # emp_code (case-insensitive)
    assert _resolve_employee_id(R, 'EMP001') == ids['EMP001']
    assert _resolve_employee_id(R, 'emp001') == ids['EMP001']
    # first name — fixes the blank-nickname gap Put found
    assert _resolve_employee_id(R, 'วฤทธิ์') == ids['EMP001']
    assert _resolve_employee_id(R, 'สันติ') == ids['EMP003']
    assert _resolve_employee_id(R, 'วิภา') == ids['EMP004']
    # nickname still works (บอล/หลุย habits)
    assert _resolve_employee_id(R, 'บอล') == ids['EMP005']
    assert _resolve_employee_id(R, 'หลุย') == ids['EMP004']
    # full name
    assert _resolve_employee_id(R, 'วฤทธิ์ ปลื้มวรสวัสดิ์') == ids['EMP001']
    # whitespace tolerated
    assert _resolve_employee_id(R, '  วิภา  ') == ids['EMP004']
    # misses
    assert _resolve_employee_id(R, 'ไม่มีคนนี้') is None
    assert _resolve_employee_id(R, '') is None
    assert _resolve_employee_id(R, None) is None


def test_first_name_beats_nickname(empty_db_conn):
    c = empty_db_conn
    _emp(c, 'E1', 'บอล ใจดี', None)        # first name = บอล
    _emp(c, 'E2', 'สมหญิง รักงาน', 'บอล')  # nickname = บอล
    c.commit()
    ids = _ids(c)
    R = _build_employee_resolver(c)
    assert _resolve_employee_id(R, 'บอล') == ids['E1']   # first-name precedence


def test_emp_code_beats_first_name(empty_db_conn):
    c = empty_db_conn
    _emp(c, 'E1', 'somchai a', None)
    # second employee's FIRST NAME collides with E1's emp_code 'e1'
    _emp(c, 'X9', 'e1 surname', None)
    c.commit()
    ids = _ids(c)
    R = _build_employee_resolver(c)
    assert _resolve_employee_id(R, 'e1') == ids['E1']    # emp_code wins over first name


def test_ambiguous_first_name_unmatched(empty_db_conn):
    c = empty_db_conn
    _emp(c, 'E1', 'สมชาย ก', None)
    _emp(c, 'E2', 'สมชาย ข', None)
    c.commit()
    R = _build_employee_resolver(c)
    assert _resolve_employee_id(R, 'สมชาย') is None   # same first name → no guess
