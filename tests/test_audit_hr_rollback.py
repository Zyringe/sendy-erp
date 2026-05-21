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


# ── Mig 073 — audit-payload gap fixes (codex adversarial review) ─────────────

MIG_073 = os.path.join(REPO, "data", "migrations",
                       "073_audit_hr_trigger_gaps.sql")
ROLLBACK_073 = os.path.join(REPO, "data", "migrations",
                            "073_audit_hr_trigger_gaps.rollback.sql")


def test_mig_073_salary_advances_update_logs_employee_id_change(tmp_db):
    """Matching an unmatched advance (employee_id NULL → 1) must be audited —
    the case mig 071 silently dropped."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        "INSERT INTO salary_advances (employee_id, advance_date, amount, raw_name) "
        "VALUES (NULL, '2026-09-05', 500.0, 'unknown person')"
    )
    advance_id = cur.lastrowid
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='salary_advances' AND row_id=? AND action='UPDATE'", (advance_id,)).fetchone()[0]
    conn.execute("UPDATE salary_advances SET employee_id=1 WHERE id=?", (advance_id,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='salary_advances' AND row_id=? AND action='UPDATE'", (advance_id,)).fetchone()[0]
    assert after > before, "employee_id change should be audited"
    cf = conn.execute("SELECT changed_fields FROM audit_log WHERE table_name='salary_advances' AND row_id=? AND action='UPDATE' ORDER BY id DESC LIMIT 1", (advance_id,)).fetchone()[0]
    assert 'employee_id' in cf
    conn.close()


def test_mig_073_payroll_items_update_logs_note_fields(tmp_db):
    """Editing other_additions_note (the "why" for a bonus) must be audited."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    # Need an existing payroll_items row from the live DB
    row = conn.execute("SELECT id FROM payroll_items LIMIT 1").fetchone()
    if row is None:
        conn.close()
        import pytest
        pytest.skip("No payroll_items in live DB clone")
    pid = row[0]
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='payroll_items' AND row_id=? AND action='UPDATE'", (pid,)).fetchone()[0]
    conn.execute("UPDATE payroll_items SET other_additions_note='new reason' WHERE id=?", (pid,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='payroll_items' AND row_id=? AND action='UPDATE'", (pid,)).fetchone()[0]
    assert after > before, "other_additions_note change should be audited"
    conn.close()


def test_mig_073_salary_history_update_trigger_exists(tmp_db):
    """Mig 071 left employee_salary_history without an UPDATE trigger — 073
    adds it. A monthly_salary correction must log."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    triggers = _trigger_names(conn)
    assert 'audit_employee_salary_history_update' in triggers
    row = conn.execute("SELECT id, monthly_salary FROM employee_salary_history LIMIT 1").fetchone()
    if row is None:
        conn.close()
        import pytest
        pytest.skip("No salary_history rows in live DB clone")
    sid, ms = row
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='employee_salary_history' AND row_id=? AND action='UPDATE'", (sid,)).fetchone()[0]
    conn.execute("UPDATE employee_salary_history SET monthly_salary=? WHERE id=?", (ms + 1, sid))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='employee_salary_history' AND row_id=? AND action='UPDATE'", (sid,)).fetchone()[0]
    assert after > before, "salary change must be audited"
    conn.close()


def test_mig_073_leave_requests_update_logs_reason(tmp_db):
    """Editing a leave reason must be audited — affects unpaid-leave and
    diligence-forfeit math."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute("SELECT id FROM leave_requests LIMIT 1").fetchone()
    if row is None:
        conn.close()
        import pytest
        pytest.skip("No leave_requests in live DB clone")
    lid = row[0]
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='leave_requests' AND row_id=? AND action='UPDATE'", (lid,)).fetchone()[0]
    conn.execute("UPDATE leave_requests SET reason='updated reason' WHERE id=?", (lid,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='leave_requests' AND row_id=? AND action='UPDATE'", (lid,)).fetchone()[0]
    assert after > before, "reason edit must be audited"
    conn.close()


def test_mig_073_rollback_round_trip(tmp_db):
    """Rollback 073 restores mig 071/072 versions (which lose the audit
    payload additions). Re-apply 073 brings them back."""
    conn = sqlite3.connect(tmp_db)
    triggers = _trigger_names(conn)
    assert 'audit_employee_salary_history_update' in triggers, "073 baseline"

    _apply(conn, ROLLBACK_073)
    triggers = _trigger_names(conn)
    assert 'audit_employee_salary_history_update' not in triggers, \
        "rollback drops the new salary_history UPDATE trigger"
    # The 4 restored triggers should still exist (rollback recreates from 071/072)
    for t in ('audit_salary_advances_update', 'audit_payroll_items_update',
              'audit_leave_requests_update'):
        assert t in triggers, f"rollback should restore {t} (mig 071/072 version)"

    _apply(conn, MIG_073)
    triggers = _trigger_names(conn)
    assert 'audit_employee_salary_history_update' in triggers
    conn.close()
