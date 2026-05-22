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


def _seed_payroll_item(conn):
    """Seed: 1 employee + 1 payroll_run + 1 payroll_item. Returns item id."""
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES ('T_AUD','audit-target','M',1,'2026-01-01',
                   90, 0, 0, 1)"""
    )
    eid = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO payroll_runs
             (year_month, company_id, status, run_date, created_by)
           VALUES ('2026-09', 1, 'draft', '2026-09-30', 1)"""
    )
    rid = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO payroll_items
             (run_id, employee_id, salary_rate, base_amount, gross, net_pay)
           VALUES (?, ?, 13000, 13000, 13000, 12350)""",
        (rid, eid),
    )
    conn.commit()
    return cur.lastrowid


def test_mig_073_payroll_items_update_logs_note_fields(tmp_db):
    """Editing other_additions_note (the "why" for a bonus) must be audited."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    pid = _seed_payroll_item(conn)
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
    # Seed our own row instead of depending on live DB clone state.
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES ('T_SH','sh-target','M',1,'2026-01-01', 90, 0, 0, 1)"""
    )
    eid = cur.lastrowid
    cur = conn.execute(
        """INSERT INTO employee_salary_history
             (employee_id, effective_date, monthly_salary, reason)
           VALUES (?, '2026-01-01', 15000.0, 'initial')""", (eid,)
    )
    sid = cur.lastrowid
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='employee_salary_history' AND row_id=? AND action='UPDATE'", (sid,)).fetchone()[0]
    conn.execute("UPDATE employee_salary_history SET monthly_salary=15500 WHERE id=?", (sid,))
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name='employee_salary_history' AND row_id=? AND action='UPDATE'", (sid,)).fetchone()[0]
    assert after > before, "salary change must be audited"
    conn.close()


def test_mig_073_leave_requests_update_logs_reason(tmp_db):
    """Editing a leave reason must be audited — affects unpaid-leave and
    diligence-forfeit math."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    # Seed our own employee + leave row so we don't depend on live DB state.
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active)
           VALUES ('T_LV','lv-target','M',1,'2026-01-01', 90, 0, 0, 1)"""
    )
    eid = cur.lastrowid
    # leave_types.id=1 is seeded by mig 054
    cur = conn.execute(
        """INSERT INTO leave_requests
             (employee_id, leave_type_id, start_date, end_date, days,
              reason, status)
           VALUES (?, 1, '2026-09-01', '2026-09-02', 2, 'initial reason', 'approved')""",
        (eid,)
    )
    lid = cur.lastrowid
    conn.commit()
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


# ── Mig 074 — full-payload INSERT/DELETE triggers (codex pass 2) ─────────────

MIG_074 = os.path.join(REPO, "data", "migrations",
                       "074_audit_full_payload_insert_delete.sql")
ROLLBACK_074 = os.path.join(REPO, "data", "migrations",
                            "074_audit_full_payload_insert_delete.rollback.sql")


def test_mig_074_payroll_items_insert_logs_full_payload(tmp_db):
    """INSERT trigger must capture all 17 material columns (not just net-pay
    summary). Verifies all the fields codex flagged as missing from mig 072
    are now in the JSON payload."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    pid = _seed_payroll_item(conn)
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='payroll_items' AND row_id=? AND action='INSERT'",
        (pid,),
    ).fetchone()[0]
    # Fields that mig 072 missed and mig 074 adds:
    for required in ('unpaid_leave_days', 'unpaid_leave_deduction',
                     'diligence_forfeit_reason', 'bonus',
                     'other_additions', 'other_additions_note',
                     'other_deductions', 'other_deductions_note',
                     'sso_employer', 'commission_amount', 'note'):
        assert required in payload, f"INSERT payload missing {required}"
    conn.close()


def test_mig_074_payroll_items_delete_logs_full_payload(tmp_db):
    """DELETE trigger must capture the full row, not just gross/net_pay."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    pid = _seed_payroll_item(conn)
    conn.execute("DELETE FROM payroll_items WHERE id=?", (pid,))
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='payroll_items' AND row_id=? AND action='DELETE'",
        (pid,),
    ).fetchone()[0]
    for required in ('unpaid_leave_deduction', 'bonus', 'other_additions',
                     'other_deductions', 'sso_employer', 'commission_amount'):
        assert required in payload, f"DELETE payload missing {required}"
    conn.close()


def test_mig_074_salary_advances_insert_logs_raw_name_and_source(tmp_db):
    """INSERT trigger must capture raw_name + source_file + import_batch_id
    so an unmatched-advance audit row identifies the import source."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        """INSERT INTO salary_advances
             (employee_id, advance_date, amount, raw_name, source_file,
              import_batch_id)
           VALUES (NULL, '2026-09-05', 500.0, 'unknown',
                   'Salary_Sheet.xlsx', 'batch-2026-09')"""
    )
    aid = cur.lastrowid
    conn.commit()
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log "
        "WHERE table_name='salary_advances' AND row_id=? AND action='INSERT'",
        (aid,),
    ).fetchone()[0]
    assert 'raw_name' in payload, "INSERT payload missing raw_name"
    assert 'source_file' in payload, "INSERT payload missing source_file"
    assert 'import_batch_id' in payload, "INSERT payload missing import_batch_id"
    conn.close()


MIG_075 = os.path.join(REPO, "data", "migrations",
                       "075_audit_employees_full_payload.sql")
ROLLBACK_075 = os.path.join(REPO, "data", "migrations",
                            "075_audit_employees_full_payload.rollback.sql")


def test_mig_075_employees_update_logs_bank_account_change(tmp_db):
    """Money path: silently changing bank_account_no must NOT be possible —
    mig 071 missed this. Mig 075 closes the gap."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active,
              bank_name, bank_account_no)
           VALUES ('T_BANK','bank-target','M',1,'2026-01-01', 90, 0, 0, 1,
                   'KBank','1234567890')"""
    )
    eid = cur.lastrowid
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='employees' "
        "AND row_id=? AND action='UPDATE'", (eid,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE employees SET bank_account_no='9999999999' WHERE id=?", (eid,)
    )
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='employees' "
        "AND row_id=? AND action='UPDATE'", (eid,)
    ).fetchone()[0]
    assert after > before, "bank_account_no change must be audited (money path)"
    payload = conn.execute(
        "SELECT changed_fields FROM audit_log WHERE table_name='employees' "
        "AND row_id=? AND action='UPDATE' ORDER BY id DESC LIMIT 1", (eid,)
    ).fetchone()[0]
    assert 'bank_account_no' in payload
    assert '1234567890' in payload and '9999999999' in payload
    conn.close()


def test_mig_075_employees_update_logs_national_id_change(tmp_db):
    """PII material change must be audited."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.execute(
        """INSERT INTO employees
             (emp_code, full_name, gender, company_id, start_date,
              probation_days, sso_enrolled, diligence_allowance, is_active,
              national_id)
           VALUES ('T_NID','nid-target','M',1,'2026-01-01', 90, 0, 0, 1,
                   '1234567890123')"""
    )
    eid = cur.lastrowid
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='employees' "
        "AND row_id=? AND action='UPDATE'", (eid,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE employees SET national_id='9999999999999' WHERE id=?", (eid,)
    )
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE table_name='employees' "
        "AND row_id=? AND action='UPDATE'", (eid,)
    ).fetchone()[0]
    assert after > before
    conn.close()


def test_mig_075_rollback_round_trip(tmp_db):
    """Rollback 075 restores mig 071's narrower trigger (no bank fields).
    Re-apply restores the expanded version."""
    conn = sqlite3.connect(tmp_db)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_employees_update'"
    ).fetchone()[0]
    assert 'bank_account_no' in sql, "075 baseline"
    _apply(conn, ROLLBACK_075)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_employees_update'"
    ).fetchone()[0]
    assert 'bank_account_no' not in sql, "rollback should restore mig 071 version"
    _apply(conn, MIG_075)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_employees_update'"
    ).fetchone()[0]
    assert 'bank_account_no' in sql
    conn.close()


def test_mig_074_rollback_round_trip(tmp_db):
    """Rollback 074 restores mig 071/072 payloads. Re-apply 074 brings the
    full payloads back."""
    conn = sqlite3.connect(tmp_db)
    # Sanity: 074 applied state → INSERT trigger SQL references full payload
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_payroll_items_insert'"
    ).fetchone()[0]
    assert 'unpaid_leave_days' in sql, "074 baseline missing"

    _apply(conn, ROLLBACK_074)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_payroll_items_insert'"
    ).fetchone()[0]
    assert 'unpaid_leave_days' not in sql, \
        "rollback should restore mig 072 (minimal) payload"

    _apply(conn, MIG_074)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='audit_payroll_items_insert'"
    ).fetchone()[0]
    assert 'unpaid_leave_days' in sql
    conn.close()
