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

def next_emp_code(conn: Optional[sqlite3.Connection] = None) -> str:
    """Return the next available EMP code (EMP%03d % (max_numeric + 1))."""
    c, owned = _conn(conn)
    try:
        row = c.execute(
            "SELECT MAX(CAST(SUBSTR(emp_code, 4) AS INTEGER))"
            "  FROM employees WHERE emp_code GLOB 'EMP[0-9]*'"
        ).fetchone()
        return "EMP%03d" % ((row[0] or 0) + 1)
    finally:
        if owned:
            c.close()


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


def get_employee_by_user_id(user_id: int,
                            conn: Optional[sqlite3.Connection] = None):
    """Return the active employee row linked to the given user_id, or None."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT * FROM employees WHERE user_id=? AND is_active=1", (user_id,)
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

        on_payroll = int(data.get("on_payroll") or 0) if "on_payroll" in data else 1
        # The employee↔login link (user_id) is NOT set here — new employees start
        # unlinked; the link is managed solely on /users. Column defaults to NULL.

        if explicit_id is not None:
            cur = c.execute(
                """INSERT INTO employees
                     (id, emp_code, full_name, nickname, national_id, gender, phone,
                      address, position, company_id, employment_type, start_date,
                      probation_days, probation_end_date, end_date, sso_enrolled,
                      diligence_allowance, bank_name, bank_branch, bank_account_no,
                      bank_account_name, salesperson_code, is_active,
                      on_payroll, note)
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
                    data.get("salesperson_code"),
                    int(data.get("is_active", 1)), on_payroll, data.get("note"),
                ),
            )
        else:
            cur = c.execute(
                """INSERT INTO employees
                     (emp_code, full_name, nickname, national_id, gender, phone,
                      address, position, company_id, employment_type, start_date,
                      probation_days, probation_end_date, end_date, sso_enrolled,
                      diligence_allowance, bank_name, bank_branch, bank_account_no,
                      bank_account_name, salesperson_code, is_active,
                      on_payroll, note)
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
                    data.get("salesperson_code"),
                    int(data.get("is_active", 1)), on_payroll, data.get("note"),
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
        # on_payroll: checkbox sends "1" when checked, nothing when unchecked
        on_payroll = int(data.get("on_payroll") or 0) if "on_payroll" in data else 1
        # user_id is deliberately NOT in this UPDATE: the employee↔login link is
        # owned by /users, so an HR edit must leave the existing link untouched.
        c.execute(
            """UPDATE employees SET
                 emp_code=?, full_name=?, nickname=?, national_id=?, gender=?,
                 phone=?, address=?, position=?, company_id=?, employment_type=?,
                 start_date=?, probation_days=?, probation_end_date=?, end_date=?,
                 sso_enrolled=?, diligence_allowance=?, bank_name=?, bank_branch=?,
                 bank_account_no=?, bank_account_name=?, salesperson_code=?,
                 is_active=?, on_payroll=?, note=?,
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
                data.get("salesperson_code"),
                int(data.get("is_active", 1)), on_payroll, data.get("note"),
                emp_id,
            ),
        )
        c.commit()
    finally:
        if owned:
            c.close()


def get_linked_account(user_id: Optional[int],
                       conn: Optional[sqlite3.Connection] = None):
    """The login account linked to an employee (read-only, for HR display).
    The link is edited only on /users; HR just shows it. None when unlinked."""
    if not user_id:
        return None
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT id, username, display_name, role FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


def get_linkable_employees(user_id: Optional[int] = None,
                           conn: Optional[sqlite3.Connection] = None):
    """Return active employees available to link to a login account.

    The employee↔account link (`employees.user_id`) is 1:1 and edited only on
    `/users`. This is the single source of that selection rule: offer employees
    with no login yet, plus — when `user_id` is given — the employee currently
    linked to that account (so the edit picker shows the existing selection).
    """
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT id, emp_code, full_name, nickname FROM employees
                WHERE is_active=1
                  AND (user_id IS NULL OR user_id = ?)
                ORDER BY sort_order, full_name""",
            (user_id,),
        ).fetchall()
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
                       status: Optional[str] = None,
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
        if status:
            clauses.append("lr.status = ?")
            params.append(status)
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
        if "employee_id" in data:
            # Full-edit path (admin leave_edit modal): all standard fields present.
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
        else:
            # Partial-update path (approval workflow): only stamp the fields provided.
            _allowed = {"status", "approved_by", "approved_at"}
            sets = [(k, data[k]) for k in _allowed if k in data]
            if sets:
                cols = ", ".join(f"{k}=?" for k, _ in sets)
                c.execute(
                    f"UPDATE leave_requests SET {cols} WHERE id=?",
                    [v for _, v in sets] + [req_id],
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


def get_employee_payslips(employee_id: int,
                          conn: Optional[sqlite3.Connection] = None):
    """One row per FINALIZED payslip for this employee, newest first.

    Drafts are intentionally excluded — an employee may only see numbers
    that have been finalized (a draft can still change before finalize)."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT pi.id AS item_id, pr.id AS run_id, pr.year_month,
                      pr.status, pi.gross, pi.net_pay,
                      co.name_th AS company_name
                 FROM payroll_items pi
                 JOIN payroll_runs pr ON pr.id = pi.run_id
                 LEFT JOIN companies co ON co.id = pr.company_id
                WHERE pi.employee_id = ? AND pr.status = 'finalized'
                ORDER BY pr.year_month DESC, pr.id DESC""",
            (employee_id,),
        ).fetchall()
    finally:
        if owned:
            c.close()


# ── Salary advances ──────────────────────────────────────────────────────────

def get_active_cashbook_accounts(conn: Optional[sqlite3.Connection] = None):
    """Return active cashbook accounts for the advance form dropdown."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT id, code, display_name
                 FROM cashbook_accounts
                WHERE is_active = 1
                ORDER BY sort_order, id"""
        ).fetchall()
    finally:
        if owned:
            c.close()


def _coerce_advance_data(data: dict) -> dict:
    """Coerce string values from request.form to the correct Python types.

    Blank/missing from_account_id → None (avoids FK IntegrityError on the
    INTEGER column when the leading '— ไม่ระบุ —' option is selected).
    """
    faid_raw = data.get("from_account_id")
    faid = None if (not faid_raw or str(faid_raw).strip() == "") else int(faid_raw)
    return {
        "employee_id": int(data["employee_id"]),
        "advance_date": data.get("advance_date", ""),
        "amount": float(data.get("amount", 0)),
        "from_account_id": faid,
        "note": data.get("note") or None,
    }


def get_salary_advances(conn: Optional[sqlite3.Connection] = None):
    """Return all advances, newest first, with employee + account info."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT sa.*,
                      e.full_name, e.emp_code,
                      ca.display_name AS account_name,
                      pr.year_month   AS deducted_ym
                 FROM salary_advances sa
                 JOIN employees e ON e.id = sa.employee_id
                 LEFT JOIN cashbook_accounts ca ON ca.id = sa.from_account_id
                 LEFT JOIN payroll_runs pr ON pr.id = sa.deducted_in_run_id
                 ORDER BY sa.advance_date DESC, sa.id DESC"""
        ).fetchall()
    finally:
        if owned:
            c.close()


def get_salary_advance(adv_id: int, conn: Optional[sqlite3.Connection] = None):
    """Return a single advance row with employee + account info, or None."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            """SELECT sa.*,
                      e.full_name, e.emp_code,
                      ca.display_name AS account_name,
                      pr.year_month   AS deducted_ym
                 FROM salary_advances sa
                 JOIN employees e ON e.id = sa.employee_id
                 LEFT JOIN cashbook_accounts ca ON ca.id = sa.from_account_id
                 LEFT JOIN payroll_runs pr ON pr.id = sa.deducted_in_run_id
                WHERE sa.id = ?""",
            (adv_id,),
        ).fetchone()
    finally:
        if owned:
            c.close()


def create_salary_advance(data: dict, conn: Optional[sqlite3.Connection] = None):
    """Insert a new salary_advance row. Returns the new id."""
    d = _coerce_advance_data(data)
    c, owned = _conn(conn)
    try:
        cur = c.execute(
            """INSERT INTO salary_advances
                 (employee_id, advance_date, amount, from_account_id, note)
               VALUES (?,?,?,?,?)""",
            (d["employee_id"], d["advance_date"], d["amount"],
             d["from_account_id"], d["note"]),
        )
        c.commit()
        return cur.lastrowid
    finally:
        if owned:
            c.close()


def update_salary_advance(adv_id: int, data: dict,
                          conn: Optional[sqlite3.Connection] = None):
    """Update editable fields. Does NOT touch deducted_in_run_id."""
    d = _coerce_advance_data(data)
    c, owned = _conn(conn)
    try:
        c.execute(
            """UPDATE salary_advances
                  SET employee_id=?, advance_date=?, amount=?,
                      from_account_id=?, note=?
                WHERE id=?""",
            (d["employee_id"], d["advance_date"], d["amount"],
             d["from_account_id"], d["note"], adv_id),
        )
        c.commit()
    finally:
        if owned:
            c.close()


def delete_salary_advance(adv_id: int, conn: Optional[sqlite3.Connection] = None):
    """Hard-delete an advance (audit trigger logs the DELETE automatically)."""
    c, owned = _conn(conn)
    try:
        c.execute("DELETE FROM salary_advances WHERE id=?", (adv_id,))
        c.commit()
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


def get_leave_count_by_status(status: str,
                             conn: Optional[sqlite3.Connection] = None) -> int:
    """Return the count of leave_requests rows with the given status."""
    c, owned = _conn(conn)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM leave_requests WHERE status=?", (status,)
        ).fetchone()[0]
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
