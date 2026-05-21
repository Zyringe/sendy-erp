"""Migrations 071 + 072 — rollback round-trip smoke tests.

Mig 071 added 15 audit_log triggers for HR/payroll tables. Mig 072 added
3 more for payroll_items (insert/delete + extended update with `note`).

Rollback files exist (`071_*.rollback.sql`, `072_*.rollback.sql`). These
tests prove the rollback actually undoes the up migration cleanly:

  applied (current state)
    → run rollback → triggers gone (or 071's UPDATE restored when rolling 072)
    → re-apply up   → triggers back to applied-state

Style mirrors test_audit_log_triggers.py (mig 070 rollback would have
caught this pattern earlier — rollback never tested means rollback never
trusted).
"""
import os
import sqlite3

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MIG_071 = os.path.join(REPO, "data", "migrations",
                       "071_audit_hr_payroll_triggers.sql")
ROLLBACK_071 = os.path.join(REPO, "data", "migrations",
                            "071_audit_hr_payroll_triggers.rollback.sql")
MIG_072 = os.path.join(REPO, "data", "migrations",
                       "072_audit_payroll_items_insert_delete.sql")
ROLLBACK_072 = os.path.join(REPO, "data", "migrations",
                            "072_audit_payroll_items_insert_delete.rollback.sql")

# All triggers added by mig 071 (15 of them).
MIG_071_TRIGGERS = {
    'audit_employees_insert', 'audit_employees_update', 'audit_employees_delete',
    'audit_employee_salary_history_insert', 'audit_employee_salary_history_delete',
    'audit_payroll_runs_insert', 'audit_payroll_runs_update', 'audit_payroll_runs_delete',
    'audit_payroll_items_update',  # mig 071 version; mig 072 replaces it
    'audit_salary_advances_insert', 'audit_salary_advances_update', 'audit_salary_advances_delete',
    'audit_leave_requests_insert', 'audit_leave_requests_update', 'audit_leave_requests_delete',
}

# Mig 072 adds these + replaces audit_payroll_items_update.
MIG_072_TRIGGERS = {
    'audit_payroll_items_insert',
    'audit_payroll_items_delete',
    'audit_payroll_items_update',  # extended with `note`
}


def _apply(conn, path):
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())


def _trigger_names(conn, prefix='audit_'):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
        (prefix + '%',)
    ).fetchall()
    return {r[0] for r in rows}


def test_mig_072_rollback_round_trip(tmp_db):
    """Rollback 072 drops INSERT/DELETE triggers + restores mig 071's UPDATE.
    Re-apply 072 brings INSERT/DELETE back + UPDATE-with-note again."""
    conn = sqlite3.connect(tmp_db)

    # Starting state: 072 applied (tmp_db is copy of live DB which has 072).
    triggers = _trigger_names(conn)
    assert MIG_072_TRIGGERS.issubset(triggers), \
        f"start: missing {MIG_072_TRIGGERS - triggers}"

    # Rollback 072 → INSERT/DELETE gone, UPDATE restored (still exists).
    _apply(conn, ROLLBACK_072)
    triggers = _trigger_names(conn)
    assert 'audit_payroll_items_insert' not in triggers
    assert 'audit_payroll_items_delete' not in triggers
    assert 'audit_payroll_items_update' in triggers, \
        "rollback should restore mig 071's UPDATE trigger"

    # Re-apply 072 → all 3 back.
    _apply(conn, MIG_072)
    triggers = _trigger_names(conn)
    assert MIG_072_TRIGGERS.issubset(triggers)

    conn.close()


def test_mig_071_rollback_round_trip(tmp_db):
    """Rollback 071 drops all 15 HR/payroll triggers (after rolling 072 first
    so we can land on the 071-applied state cleanly). Re-apply 071 brings
    them back."""
    conn = sqlite3.connect(tmp_db)

    # Have to roll 072 first since it sits on top of 071.
    _apply(conn, ROLLBACK_072)
    triggers_after_072_rb = _trigger_names(conn)
    # 071 triggers (including its UPDATE) should still exist
    assert MIG_071_TRIGGERS.issubset(triggers_after_072_rb)

    # Rollback 071 → all 15 triggers gone.
    _apply(conn, ROLLBACK_071)
    triggers = _trigger_names(conn)
    leftover = MIG_071_TRIGGERS & triggers
    assert not leftover, f"after rollback 071, leftover: {leftover}"

    # Re-apply 071 → 15 back.
    _apply(conn, MIG_071)
    triggers = _trigger_names(conn)
    assert MIG_071_TRIGGERS.issubset(triggers)

    # Re-apply 072 to leave DB in the original state (idempotency vs.
    # next test in the file — but pytest gives a fresh tmp_db per test, so
    # this is just hygiene).
    _apply(conn, MIG_072)
    conn.close()


def test_applied_migrations_row_cleaned_on_rollback(tmp_db):
    """Rollback scripts delete their row from applied_migrations so the
    runner re-applies on next boot."""
    conn = sqlite3.connect(tmp_db)
    # Start: both rows present
    rows = conn.execute(
        "SELECT filename FROM applied_migrations "
        "WHERE filename IN ('071_audit_hr_payroll_triggers.sql', "
        "                   '072_audit_payroll_items_insert_delete.sql')"
    ).fetchall()
    assert len(rows) == 2

    _apply(conn, ROLLBACK_072)
    rows = conn.execute(
        "SELECT filename FROM applied_migrations "
        "WHERE filename='072_audit_payroll_items_insert_delete.sql'"
    ).fetchall()
    assert len(rows) == 0

    _apply(conn, ROLLBACK_071)
    rows = conn.execute(
        "SELECT filename FROM applied_migrations "
        "WHERE filename='071_audit_hr_payroll_triggers.sql'"
    ).fetchall()
    assert len(rows) == 0

    conn.close()
