"""Phase 2 — id ↔ emp_code renumber (HIGH RISK).

Synthetic fixture test: proves the migration aligns id==emp_code and
preserves attribution (content keyed by emp_code is unchanged) across all
FK-holding tables, with no row count change. No live DB needed.
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
