"""Phase 1 HR quick-win tests.

Fixtures used from conftest.py:
  - tmp_db: clones live DB → returns path; monkeypatches config.DATABASE_PATH
  - tmp_db_conn: sqlite3 connection on tmp_db

Local fixture:
  - admin_client: Flask test client with admin session, built on top of tmp_db
"""
import os
os.environ.setdefault('SKIP_DB_INIT', '1')

import sqlite3
import pytest


# ── Local fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def admin_client(tmp_db):
    """Flask test client with an admin session pre-populated.
    tmp_db must be in scope first so config.DATABASE_PATH is monkeypatched
    before `from app import app` runs (mirrors test_bp_hr_routes.py)."""
    from app import app as flask_app
    flask_app.config['TESTING'] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['user_id']  = 1
        sess['username'] = 'test-admin'
        sess['role']     = 'admin'
    return c


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ── Task 1.1: Migration 114 ───────────────────────────────────────────────────

def test_employees_has_sort_order_and_on_payroll(tmp_db):
    conn = sqlite3.connect(tmp_db)
    try:
        cols = _cols(conn, "employees")
        assert "sort_order" in cols
        assert "on_payroll" in cols
        rows = conn.execute(
            "SELECT on_payroll, sort_order FROM employees"
        ).fetchall()
        for on_payroll, sort_order in rows:
            assert on_payroll == 1
            assert sort_order is not None
    finally:
        conn.close()


# ── Task 1.2: get_employees ORDER BY sort_order ───────────────────────────────

def test_get_employees_orders_by_sort_order(tmp_db):
    import hr_queries as hrq
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute("UPDATE employees SET sort_order=5  WHERE emp_code='EMP002'")
        conn.execute("UPDATE employees SET sort_order=99 WHERE emp_code='EMP001'")
        conn.commit()
    finally:
        conn.close()
    rows = hrq.get_employees()
    codes = [r["emp_code"] for r in rows]
    # EMP002 (sort_order=5) must come before EMP001 (sort_order=99)
    assert codes.index("EMP002") < codes.index("EMP001")


# ── Task 1.3: เบี้ยขยัน default 500 ──────────────────────────────────────────

def test_new_employee_form_defaults_diligence_500(admin_client):
    resp = admin_client.get("/hr/employees/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="diligence_allowance"' in html
    assert 'value="500"' in html


# ── Task 1.4: Bank dropdown ───────────────────────────────────────────────────

def test_new_employee_form_renders_bank_dropdown(admin_client):
    resp = admin_client.get("/hr/employees/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<select name="bank_name"' in html
    assert "ธนาคารกสิกรไทย" in html
    assert "ธนาคารออมสิน" in html
    assert "อื่นๆ" in html


def test_bank_other_freeform_is_saved(admin_client, tmp_db):
    admin_client.post("/hr/employees/new", data={
        "emp_code":        "EMP900",
        "full_name":       "ทดสอบ ธนาคาร",
        "bank_name":       "__other__",
        "bank_name_other": "ธนาคารอิสลามแห่งประเทศไทย",
        "company_id":      "1",
        "employment_type": "monthly",
    })
    conn = sqlite3.connect(tmp_db)
    try:
        row = conn.execute(
            "SELECT bank_name FROM employees WHERE emp_code='EMP900'"
        ).fetchone()
        assert row is not None, "Employee EMP900 was not created"
        assert row[0] == "ธนาคารอิสลามแห่งประเทศไทย"
    finally:
        conn.close()


# ── Task 1.5: Bank name normalization helper ──────────────────────────────────

def test_existing_bank_names_normalized():
    from hr_bank import normalize_bank
    assert normalize_bank("กสิกร")     == "ธนาคารกสิกรไทย"
    assert normalize_bank("กสิกรไทย")  == "ธนาคารกสิกรไทย"
    assert normalize_bank("กรุงไทย")   == "ธนาคารกรุงไทย"
    assert normalize_bank("ไทยพาณิชย์") == "ธนาคารไทยพาณิชย์"
    assert normalize_bank("")           == ""
    assert normalize_bank(None)         == ""
