"""HR CRUD query helpers — raw SQL via database.get_connection().

Kept separate from hr.py so that the tested logic module stays untouched.
All functions accept an optional `conn` kwarg; if omitted they open and
close their own connection (following the existing app.py pattern).
Python 3.9 — Optional[...] not `X | None`.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from database import get_connection


def _conn(conn: Optional[sqlite3.Connection] = None) -> tuple:
    """Return (connection, owned). If owned=True caller must close it."""
    if conn is not None:
        return conn, False
    return get_connection(), True


# ── Companies ────────────────────────────────────────────────────────────────

def get_companies(conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT id, name_th AS name FROM companies ORDER BY id"
        ).fetchall()
    finally:
        if owned:
            c.close()


# ── Leave types ──────────────────────────────────────────────────────────────

def get_leave_types(conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT * FROM leave_types WHERE is_active=1 ORDER BY sort_order, id"
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_leave_type(lt_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT * FROM leave_types WHERE id=?", (lt_id,)
        ).fetchone()
    finally:
        if owned:
            c.close()


# ── Employees ────────────────────────────────────────────────────────────────

def get_employees(active_only: bool = False,
                  conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        sql = """
            SELECT e.*, co.name_th AS company_name
              FROM employees e
              LEFT JOIN companies co ON co.id = e.company_id
        """
        if active_only:
            sql += " WHERE e.is_active = 1"
        sql += " ORDER BY e.sort_order, e.emp_code"
        return c.execute(sql).fetchall()
    finally:
        if owned:
            c.close()


def get_employee(emp_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT e.*, co.name_th AS company_name
                 FROM employees e
                 LEFT JOIN companies co ON co.id = e.company_id
                WHERE e.id = ?""",
            (emp_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


def get_employee_salary_history(emp_id: int,
                                conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT * FROM employee_salary_history
                WHERE employee_id = ?
                ORDER BY effective_date DESC, id DESC""",
            (emp_id,),
        ).fetchall()
    finally:
        if owned:
            c.close()


def create_employee(data: dict, conn: Optional[sqlite3.Connection] = None):
    """Insert a new employee row. Returns new id.

    When emp_code matches ^EMP\\d+$, sets id explicitly to the numeric suffix
    so id==emp_code is preserved on every new hire (Phase 2 invariant).
    """
    import re
    c, owned = _conn(conn)
    try:
        code = (data.get("emp_code") or "").strip()
        m = re.fullmatch(r"EMP(\d+)", code)
        explicit_id = int(m.group(1)) if m else None

        if explicit_id is not None:
            cur = c.execute(
                """INSERT INTO employees
                     (id, emp_code, full_name, nickname, national_id, gender, phone,
                      address, position, company_id, employment_type, start_date,
                      probation_days, probation_end_date, end_date, sso_enrolled,
                      diligence_allowance, bank_name, bank_branch, bank_account_no,
                      bank_account_name, salesperson_code, user_id, is_active, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    explicit_id,
                    data.get("emp_code"), data.get("full_name"),
                    data.get("nickname"), data.get("national_id"),
                    data.get("gender"), data.get("phone"),
                    data.get("address"), data.get("position"),
                    data.get("company_id"), data.get("employment_type", "monthly"),
                    data.get("start_date"), data.get("probation_days", 90),
                    data.get("probation_end_date"), data.get("end_date"),
                    int(data.get("sso_enrolled", 1)),
                    float(data.get("diligence_allowance") or 0),
                    data.get("bank_name"), data.get("bank_branch"),
                    data.get("bank_account_no"), data.get("bank_account_name"),
                    data.get("salesperson_code"), data.get("user_id"),
                    int(data.get("is_active", 1)), data.get("note"),
                ),
            )
        else:
            cur = c.execute(
                """INSERT INTO employees
                     (emp_code, full_name, nickname, national_id, gender, phone,
                      address, position, company_id, employment_type, start_date,
                      probation_days, probation_end_date, end_date, sso_enrolled,
                      diligence_allowance, bank_name, bank_branch, bank_account_no,
                      bank_account_name, salesperson_code, user_id, is_active, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.get("emp_code"), data.get("full_name"),
                    data.get("nickname"), data.get("national_id"),
                    data.get("gender"), data.get("phone"),
                    data.get("address"), data.get("position"),
                    data.get("company_id"), data.get("employment_type", "monthly"),
                    data.get("start_date"), data.get("probation_days", 90),
                    data.get("probation_end_date"), data.get("end_date"),
                    int(data.get("sso_enrolled", 1)),
                    float(data.get("diligence_allowance") or 0),
                    data.get("bank_name"), data.get("bank_branch"),
                    data.get("bank_account_no"), data.get("bank_account_name"),
                    data.get("salesperson_code"), data.get("user_id"),
                    int(data.get("is_active", 1)), data.get("note"),
                ),
            )
        c.commit()
        return cur.lastrowid
    finally:
        if owned:
            c.close()


def update_employee(emp_id: int, data: dict,
                    conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        c.execute(
            """UPDATE employees SET
                 emp_code=?, full_name=?, nickname=?, national_id=?, gender=?,
                 phone=?, address=?, position=?, company_id=?, employment_type=?,
                 start_date=?, probation_days=?, probation_end_date=?, end_date=?,
                 sso_enrolled=?, diligence_allowance=?, bank_name=?, bank_branch=?,
                 bank_account_no=?, bank_account_name=?, salesperson_code=?,
                 user_id=?, is_active=?, note=?,
                 updated_at=datetime('now','localtime')
               WHERE id=?""",
            (
                data.get("emp_code"), data.get("full_name"),
                data.get("nickname"), data.get("national_id"),
                data.get("gender"), data.get("phone"),
                data.get("address"), data.get("position"),
                data.get("company_id"), data.get("employment_type", "monthly"),
                data.get("start_date"), data.get("probation_days", 90),
                data.get("probation_end_date"), data.get("end_date"),
                int(data.get("sso_enrolled", 1)),
                float(data.get("diligence_allowance") or 0),
                data.get("bank_name"), data.get("bank_branch"),
                data.get("bank_account_no"), data.get("bank_account_name"),
                data.get("salesperson_code"), data.get("user_id"),
                int(data.get("is_active", 1)), data.get("note"),
                emp_id,
            ),
        )
        c.commit()
    finally:
        if owned:
            c.close()


def add_salary_history(emp_id: int, effective_date: str,
                       monthly_salary: float, reason: str,
                       note: Optional[str] = None,
                       conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        c.execute(
            """INSERT OR REPLACE INTO employee_salary_history
                 (employee_id, effective_date, monthly_salary, reason, note)
               VALUES (?,?,?,?,?)""",
            (emp_id, effective_date, monthly_salary, reason, note),
        )
        c.commit()
    finally:
        if owned:
            c.close()


# ── Leave entitlements ───────────────────────────────────────────────────────

def get_employee_entitlements(emp_id: int, year: int,
                              conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT e.*, lt.code, lt.name_th
                 FROM employee_leave_entitlements e
                 JOIN leave_types lt ON lt.id = e.leave_type_id
                WHERE e.employee_id = ? AND e.year = ?
                ORDER BY lt.sort_order, lt.id""",
            (emp_id, year),
        ).fetchall()
    finally:
        if owned:
            c.close()


def upsert_entitlement(emp_id: int, leave_type_id: int, year: int,
                       quota_days: Optional[float],
                       note: Optional[str] = None,
                       conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        c.execute(
            """INSERT OR REPLACE INTO employee_leave_entitlements
                 (employee_id, leave_type_id, year, quota_days, note)
               VALUES (?,?,?,?,?)""",
            (emp_id, leave_type_id, year, quota_days, note),
        )
        c.commit()
    finally:
        if owned:
            c.close()


# ── Leave requests ───────────────────────────────────────────────────────────

def get_leave_requests(employee_id: Optional[int] = None,
                       year_month: Optional[str] = None,
                       leave_type_id: Optional[int] = None,
                       conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        clauses = []
        params = []
        if employee_id:
            clauses.append("lr.employee_id = ?")
            params.append(employee_id)
        if year_month:
            clauses.append("substr(lr.start_date, 1, 7) = ?")
            params.append(year_month)
        if leave_type_id:
            clauses.append("lr.leave_type_id = ?")
            params.append(leave_type_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return c.execute(
            f"""SELECT lr.*, e.full_name, e.emp_code,
                       lt.code AS leave_code, lt.name_th AS leave_name
                  FROM leave_requests lr
                  JOIN employees e ON e.id = lr.employee_id
                  JOIN leave_types lt ON lt.id = lr.leave_type_id
                  {where}
                  ORDER BY lr.start_date DESC, lr.id DESC""",
            params,
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_leave_request(req_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT lr.*, e.full_name, e.emp_code,
                      lt.code AS leave_code, lt.name_th AS leave_name
                 FROM leave_requests lr
                 JOIN employees e ON e.id = lr.employee_id
                 JOIN leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.id = ?""",
            (req_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


def create_leave_request(data: dict, created_by: int,
                         conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        cur = c.execute(
            """INSERT INTO leave_requests
                 (employee_id, leave_type_id, start_date, end_date, days,
                  reason, has_medical_cert, status, created_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                data["employee_id"], data["leave_type_id"],
                data["start_date"], data["end_date"],
                float(data.get("days", 1)),
                data.get("reason"), int(data.get("has_medical_cert", 0)),
                data.get("status", "approved"), created_by,
            ),
        )
        c.commit()
        return cur.lastrowid
    finally:
        if owned:
            c.close()


def update_leave_request(req_id: int, data: dict,
                         conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        c.execute(
            """UPDATE leave_requests SET
                 employee_id=?, leave_type_id=?, start_date=?, end_date=?,
                 days=?, reason=?, has_medical_cert=?, status=?
               WHERE id=?""",
            (
                data["employee_id"], data["leave_type_id"],
                data["start_date"], data["end_date"],
                float(data.get("days", 1)), data.get("reason"),
                int(data.get("has_medical_cert", 0)),
                data.get("status", "approved"), req_id,
            ),
        )
        c.commit()
    finally:
        if owned:
            c.close()


def delete_leave_request(req_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        c.execute("DELETE FROM leave_requests WHERE id=?", (req_id,))
        c.commit()
    finally:
        if owned:
            c.close()


# ── Payroll runs ─────────────────────────────────────────────────────────────

def get_payroll_runs(conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT pr.*, co.name_th AS company_name,
                      COUNT(pi.id) AS item_count,
                      ROUND(SUM(pi.net_pay), 2) AS total_net
                 FROM payroll_runs pr
                 LEFT JOIN companies co ON co.id = pr.company_id
                 LEFT JOIN payroll_items pi ON pi.run_id = pr.id
                 GROUP BY pr.id
                 ORDER BY pr.year_month DESC, pr.company_id"""
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_payroll_run(run_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT pr.*, co.name_th AS company_name
                 FROM payroll_runs pr
                 LEFT JOIN companies co ON co.id = pr.company_id
                WHERE pr.id = ?""",
            (run_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


def get_payroll_items(run_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT pi.*, e.full_name, e.emp_code, e.bank_name,
                      e.bank_branch, e.bank_account_no, e.bank_account_name
                 FROM payroll_items pi
                 JOIN employees e ON e.id = pi.employee_id
                WHERE pi.run_id = ?
                ORDER BY e.emp_code""",
            (run_id,),
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_payroll_item(item_id: int, conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT pi.*, e.full_name, e.emp_code, e.bank_name,
                      e.bank_branch, e.bank_account_no, e.bank_account_name
                 FROM payroll_items pi
                 JOIN employees e ON e.id = pi.employee_id
                WHERE pi.id = ?""",
            (item_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


# ── Dashboard helpers ────────────────────────────────────────────────────────

def get_headcount(conn: Optional[sqlite3.Connection] = None) -> int:
    c, owned = _conn(conn)
    try:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM employees WHERE is_active=1"
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        if owned:
            c.close()


def get_on_leave_today(today_iso: str,
                       conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT e.full_name, e.emp_code, lt.name_th AS leave_name,
                      lr.end_date
                 FROM leave_requests lr
                 JOIN employees e ON e.id = lr.employee_id
                 JOIN leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.status = 'approved'
                  AND lr.start_date <= ? AND lr.end_date >= ?
                ORDER BY e.emp_code""",
            (today_iso, today_iso),
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_probation_ending(cutoff_iso: str,
                         today_iso: str,
                         conn: Optional[sqlite3.Connection] = None):
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT e.full_name, e.emp_code, e.probation_end_date,
                      co.name_th AS company_name
                 FROM employees e
                 LEFT JOIN companies co ON co.id = e.company_id
                WHERE e.is_active = 1
                  AND e.probation_end_date IS NOT NULL
                  AND e.probation_end_date BETWEEN ? AND ?
                ORDER BY e.probation_end_date""",
            (today_iso, cutoff_iso),
        ).fetchall()
    finally:
        if owned:
            c.close()
