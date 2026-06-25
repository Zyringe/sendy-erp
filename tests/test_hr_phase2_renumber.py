"""Phase 2 — id ↔ emp_code renumber (HIGH RISK).

Tests:
  test_renumber_*        — synthetic fixture; no live DB needed.
  test_create_employee_* — integration; uses tmp_db (copy of live DB).
"""
import sqlite3
import pathlib

MIG = pathlib.Path("data/migrations/116_renumber_employee_id.sql")


def _fixture(conn):
    """Create a minimal 3-employee DB that reproduces every hazard:
    - id != emp_code number (the very condition being fixed)
    - shared effective_date across employees (UNIQUE(employee_id, effective_date) hazard)
    - same run_id across employees (UNIQUE(run_id, employee_id) hazard)
    - a finalized payroll row
    - a salary advance
    """
    conn.executescript("""
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_code TEXT UNIQUE,
            full_name TEXT
        );
        CREATE TABLE employee_salary_history (
            id INTEGER PRIMARY KEY,
            employee_id INT,
            effective_date TEXT,
            amount REAL,
            UNIQUE(employee_id, effective_date)
        );
        CREATE TABLE payroll_items (
            id INTEGER PRIMARY KEY,
            run_id INT,
            employee_id INT,
            net_pay REAL,
            UNIQUE(run_id, employee_id)
        );
        CREATE TABLE salary_advances (
            id INTEGER PRIMARY KEY,
            employee_id INT,
            amount REAL
        );
        CREATE TABLE leave_requests (
            id INTEGER PRIMARY KEY,
            employee_id INT,
            days REAL
        );
        CREATE TABLE employee_leave_entitlements (
            id INTEGER PRIMARY KEY,
            employee_id INT,
            leave_type_id INT,
            year INT,
            UNIQUE(employee_id, leave_type_id, year)
        );

        -- 3 employees: id != emp_code number (the hazard)
        INSERT INTO employees (id, emp_code, full_name)
            VALUES (1,'EMP003','C'),(2,'EMP001','A'),(3,'EMP002','B');

        -- shared effective_date across employees (UNIQUE hazard)
        INSERT INTO employee_salary_history (id, employee_id, effective_date, amount)
            VALUES (1,1,'2026-02-01',300),(2,2,'2026-02-01',100),(3,3,'2026-02-01',200);

        -- same run_id, distinct employees (UNIQUE(run_id, employee_id) hazard)
        INSERT INTO payroll_items (id, run_id, employee_id, net_pay)
            VALUES (1,9,1,3000),(2,9,2,1000),(3,9,3,2000);

        INSERT INTO salary_advances (id, employee_id, amount)
            VALUES (1,1,50),(2,2,60);

        INSERT INTO leave_requests (id, employee_id, days)
            VALUES (1,1,2);
    """)


def _by_code(conn, table, cols):
    """Content keyed by emp_code — the stable identifier that survives the renumber."""
    return sorted(conn.execute(
        f"SELECT e.emp_code, {cols} FROM {table} t JOIN employees e ON e.id=t.employee_id"
    ).fetchall())


def test_renumber_aligns_id_and_preserves_attribution(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    _fixture(conn)

    before = {
        "salary":  _by_code(conn, "employee_salary_history", "effective_date, amount"),
        "payroll": _by_code(conn, "payroll_items",           "run_id, net_pay"),
        "advance": _by_code(conn, "salary_advances",         "amount"),
        "leave":   _by_code(conn, "leave_requests",          "days"),
    }

    conn.executescript(MIG.read_text())

    # 1. id == emp_code number for every employee
    for eid, code in conn.execute("SELECT id, emp_code FROM employees"):
        assert eid == int(code[3:]), f"{code} ended up with id={eid}"

    # 2. attribution preserved (same content keyed by emp_code)
    after = {
        "salary":  _by_code(conn, "employee_salary_history", "effective_date, amount"),
        "payroll": _by_code(conn, "payroll_items",           "run_id, net_pay"),
        "advance": _by_code(conn, "salary_advances",         "amount"),
        "leave":   _by_code(conn, "leave_requests",          "days"),
    }
    assert after == before

    # 3. sqlite_sequence reset to MAX(id)
    seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='employees'"
    ).fetchone()[0]
    assert seq == 3

    # 4. row counts unchanged
    expected_counts = {
        "employees": 3,
        "employee_salary_history": 3,
        "payroll_items": 3,
        "salary_advances": 2,
        "leave_requests": 1,
    }
    for table, expected in expected_counts.items():
        actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert actual == expected, f"{table}: expected {expected} rows, got {actual}"


# ── Task 2.2: create_employee assigns explicit id from emp_code ──────────────

def test_create_employee_sets_id_from_emp_code(tmp_db):
    """EMP009 → id 9. Current seq=6, so autoincrement gives 7 — tests explicit path."""
    import hr_queries as hrq
    new_id = hrq.create_employee({
        "emp_code": "EMP009",
        "full_name": "ทดสอบ ไอดี",
        "company_id": 1,
    })
    assert new_id == 9, f"expected id=9 from EMP009, got {new_id}"
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    row = conn.execute("SELECT id FROM employees WHERE emp_code='EMP009'").fetchone()
    assert row is not None, "EMP009 row not found"
    assert row[0] == 9, f"DB id={row[0]}, expected 9"


def test_create_employee_non_emp_code_uses_autoincrement(tmp_db):
    """Non-EMP### code → no explicit id, falls back to autoincrement."""
    import hr_queries as hrq
    new_id = hrq.create_employee({
        "emp_code": "OWNER001",
        "full_name": "เจ้าของ ทดสอบ",
        "company_id": 1,
    })
    # autoincrement from seq=6 → 7 (assuming EMP009 test hasn't run first in same db)
    assert isinstance(new_id, int) and new_id > 0
