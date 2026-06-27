"""Phase 6 — Self-service payslip.

Task 6.1: get_employee_payslips — finalized-only helper.
Task 6.2: /me/payslip list + /me/payslip/<item_id> detail routes (security core).
Task 6.3: parameterize payslip back-button (manager flow unchanged).
Task 6.4: general allowlist + สลิป nav slot.
Task 6.5: per-actor security matrix.

Python 3.9 — Optional[...] not `X | None`.
"""
import os
import sqlite3

import pytest


# ── Shared seed helpers ───────────────────────────────────────────────────────

def _seed_payslips(db):
    """EMP004 = our employee. Returns (employee_id_A, employee_id_B).

    Creates:
      finalized run 901 (item 9001 for EMP004, item 9002 for EMP002),
      draft run 902 (item 9003 for EMP004).
    """
    c = sqlite3.connect(db)
    a = c.execute("SELECT id FROM employees WHERE emp_code='EMP004'").fetchone()[0]
    b = c.execute("SELECT id FROM employees WHERE emp_code='EMP002'").fetchone()[0]
    # Use 2099-xx year_months to avoid the UNIQUE(year_month,company_id)
    # constraint collision with real finalized runs (3='2026-05', 4='2026-04').
    c.executescript(f"""
        INSERT OR IGNORE INTO payroll_runs(id,year_month,company_id,status) VALUES
            (901,'2099-01',1,'finalized'), (902,'2099-02',1,'draft');
        INSERT OR IGNORE INTO payroll_items(id,run_id,employee_id,gross,net_pay) VALUES
            (9001,901,{a},20000,19000),
            (9002,901,{b},30000,28000),
            (9003,902,{a},20000,19000);
    """)
    c.commit(); c.close()
    return a, b


def _link(db, emp_code, uid):
    """Link an employee to a user_id."""
    c = sqlite3.connect(db)
    c.execute("UPDATE employees SET user_id=? WHERE emp_code=?", (uid, emp_code))
    c.commit(); c.close()


def _client(role, user_id):
    """Test client pre-logged-in as given role and user_id.
    Relies on tmp_db monkeypatching config/database.DATABASE_PATH."""
    os.environ.setdefault('SKIP_DB_INIT', '1')
    from app import app as a
    a.config['TESTING'] = True
    c = a.test_client()
    with c.session_transaction() as s:
        s['user_id'] = user_id
        s['username'] = f'test-{role}'
        s['role'] = role
    return c


# ── Task 6.1 — get_employee_payslips (finalized only) ────────────────────────

def test_get_employee_payslips_finalized_only(tmp_db):
    """Own finalized run visible; own DRAFT and another employee's item hidden."""
    import hr_queries as hrq
    a, _ = _seed_payslips(tmp_db)
    # No explicit conn — uses get_connection() → monkeypatched DATABASE_PATH
    rows = hrq.get_employee_payslips(a)
    ids = [r["item_id"] for r in rows]
    assert 9001 in ids          # own finalized → visible
    assert 9003 not in ids      # own DRAFT → hidden
    assert 9002 not in ids      # another employee's item → never
