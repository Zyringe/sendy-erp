-- ============================================================================
-- 178 — Payroll carry-forward (P1a: migration + engine).
--
-- See projects/payroll-carry-forward/plan.md for the full design. Trigger:
-- prod payroll_runs id 8 (2026-08, draft), employee EMP004 หลุย — net_pay
-- computed to -฿950 (SSO ฿750 + a ฿15,200 advance against a ฿15,000
-- salary). Finalizing as-is would stamp the FULL ฿15,200 of advances
-- "collected", pay ฿0, and silently write off the ฿950 the moment
-- September started clean.
--
-- Adds two columns to payroll_items:
--   carried_in  REAL NOT NULL DEFAULT 0 CHECK(>= 0) — ยอดยกมาจากรอบก่อน,
--     subtracted like any other deduction (hr.py::_recompute_totals).
--   carried_out REAL NOT NULL DEFAULT 0 CHECK(>= 0) — ส่วนที่รอบนี้เก็บไม่ได้,
--     what next month's carried_in will read.
--
-- Both are DERIVED, never stamped by hand: generate_run's DELETE+re-INSERT
-- means anything keyed by hand into a draft dies on the next regenerate
-- (hr.py:1245 — "existing items for the run are replaced, preserving
-- nothing"). carried_in for a run is read from the carried_out of that
-- employee's most recent FINALIZED prior run — a finalized run cannot
-- change without an explicit reopen_run, so the source is stable and a
-- regenerate recomputes the same value every time.
--
-- Rejected: a salary_advances row for the carried amount — _resolve_advance_
-- rows forces direction='expense', a real cash outflow; a carried balance is
-- not new cash leaving, so that would double-count it (plan.md fact #7).
--
-- payroll_items' three audit triggers (insert/update/delete) are recreated
-- to also cover carried_in/carried_out — money on the same row, same
-- precedent as 157_wht_single_source.sql did for wht_amount (copied
-- verbatim below, only the column list differs).
--
-- ⚠ NOT re-runnable on its own: `ALTER TABLE ... ADD COLUMN` has no IF NOT
-- EXISTS form, so a second apply fails with `duplicate column name:
-- carried_in`. To re-apply, run 178_payroll_carry_forward.rollback.sql
-- FIRST (see the `pre178_conn` fixture in
-- tests/test_payroll_carry_forward.py, same shape as test_mig157's
-- pre157_conn).
--
-- Rehearsed forward + rollback on a `sqlite3 .backup` snapshot (never `cp`
-- — sendy holds a WAL) before commit, sqlite_master diffed byte-identical
-- after rollback including all three trigger bodies.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS audit_payroll_items_delete;
DROP TRIGGER IF EXISTS audit_payroll_items_insert;
DROP TRIGGER IF EXISTS audit_payroll_items_update;

-- SQLite accepts a CHECK on ADD COLUMN (verified 3.51.0 local; same pattern
-- already proven for wht_amount in migration 157) — existing rows take the
-- default 0 and pass, a later INSERT/UPDATE of a negative value is refused
-- at the schema level, not only by hr.py's Python guard.
ALTER TABLE payroll_items ADD COLUMN carried_in REAL NOT NULL DEFAULT 0
    CHECK (carried_in >= 0);
ALTER TABLE payroll_items ADD COLUMN carried_out REAL NOT NULL DEFAULT 0
    CHECK (carried_out >= 0);

CREATE TRIGGER audit_payroll_items_delete
BEFORE DELETE ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', OLD.id, 'DELETE',
        json_object(
            'run_id',                    OLD.run_id,
            'employee_id',               OLD.employee_id,
            'salary_rate',               OLD.salary_rate,
            'base_amount',               OLD.base_amount,
            'unpaid_leave_days',         OLD.unpaid_leave_days,
            'unpaid_leave_deduction',    OLD.unpaid_leave_deduction,
            'diligence_allowance',       OLD.diligence_allowance,
            'diligence_forfeited',       OLD.diligence_forfeited,
            'diligence_forfeit_reason',  OLD.diligence_forfeit_reason,
            'bonus',                     OLD.bonus,
            'other_additions',           OLD.other_additions,
            'other_additions_note',      OLD.other_additions_note,
            'other_deductions',          OLD.other_deductions,
            'other_deductions_note',     OLD.other_deductions_note,
            'wht_amount',                OLD.wht_amount,
            'sso_employee',              OLD.sso_employee,
            'sso_employer',              OLD.sso_employer,
            'commission_amount',         OLD.commission_amount,
            'salary_advance_deduction',  OLD.salary_advance_deduction,
            'carried_in',                OLD.carried_in,
            'carried_out',               OLD.carried_out,
            'gross',                     OLD.gross,
            'net_pay',                   OLD.net_pay,
            'note',                      OLD.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_insert
AFTER INSERT ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', NEW.id, 'INSERT',
        json_object(
            'run_id',                    NEW.run_id,
            'employee_id',               NEW.employee_id,
            'salary_rate',               NEW.salary_rate,
            'base_amount',               NEW.base_amount,
            'unpaid_leave_days',         NEW.unpaid_leave_days,
            'unpaid_leave_deduction',    NEW.unpaid_leave_deduction,
            'diligence_allowance',       NEW.diligence_allowance,
            'diligence_forfeited',       NEW.diligence_forfeited,
            'diligence_forfeit_reason',  NEW.diligence_forfeit_reason,
            'bonus',                     NEW.bonus,
            'other_additions',           NEW.other_additions,
            'other_additions_note',      NEW.other_additions_note,
            'other_deductions',          NEW.other_deductions,
            'other_deductions_note',     NEW.other_deductions_note,
            'wht_amount',                NEW.wht_amount,
            'sso_employee',              NEW.sso_employee,
            'sso_employer',              NEW.sso_employer,
            'commission_amount',         NEW.commission_amount,
            'salary_advance_deduction',  NEW.salary_advance_deduction,
            'carried_in',                NEW.carried_in,
            'carried_out',               NEW.carried_out,
            'gross',                     NEW.gross,
            'net_pay',                   NEW.net_pay,
            'note',                      NEW.note
        )
    );
END;

CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                       IS NOT NEW.bonus
    OR OLD.other_additions             IS NOT NEW.other_additions
    OR OLD.other_deductions            IS NOT NEW.other_deductions
    OR OLD.wht_amount                  IS NOT NEW.wht_amount
    OR OLD.diligence_allowance         IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee                IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
    OR OLD.carried_in                  IS NOT NEW.carried_in
    OR OLD.carried_out                 IS NOT NEW.carried_out
    OR OLD.gross                       IS NOT NEW.gross
    OR OLD.net_pay                     IS NOT NEW.net_pay
    OR OLD.note                        IS NOT NEW.note
    OR OLD.other_additions_note        IS NOT NEW.other_additions_note
    OR OLD.other_deductions_note       IS NOT NEW.other_deductions_note
    OR OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus'                       AS field, OLD.bonus                       AS old_v, NEW.bonus                       AS new_v WHERE OLD.bonus                       IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',             OLD.other_additions,             NEW.other_additions             WHERE OLD.other_additions             IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',            OLD.other_deductions,            NEW.other_deductions            WHERE OLD.other_deductions            IS NOT NEW.other_deductions
        UNION ALL SELECT 'wht_amount',                  OLD.wht_amount,                  NEW.wht_amount                  WHERE OLD.wht_amount                  IS NOT NEW.wht_amount
        UNION ALL SELECT 'diligence_allowance',         OLD.diligence_allowance,         NEW.diligence_allowance         WHERE OLD.diligence_allowance         IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',         OLD.diligence_forfeited,         NEW.diligence_forfeited         WHERE OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',                OLD.sso_employee,                NEW.sso_employee                WHERE OLD.sso_employee                IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction',    OLD.salary_advance_deduction,    NEW.salary_advance_deduction    WHERE OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'carried_in',                  OLD.carried_in,                  NEW.carried_in                  WHERE OLD.carried_in                  IS NOT NEW.carried_in
        UNION ALL SELECT 'carried_out',                 OLD.carried_out,                 NEW.carried_out                 WHERE OLD.carried_out                 IS NOT NEW.carried_out
        UNION ALL SELECT 'gross',                       OLD.gross,                       NEW.gross                       WHERE OLD.gross                       IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',                     OLD.net_pay,                     NEW.net_pay                     WHERE OLD.net_pay                     IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                        OLD.note,                        NEW.note                        WHERE OLD.note                        IS NOT NEW.note
        UNION ALL SELECT 'other_additions_note',        OLD.other_additions_note,        NEW.other_additions_note        WHERE OLD.other_additions_note        IS NOT NEW.other_additions_note
        UNION ALL SELECT 'other_deductions_note',       OLD.other_deductions_note,       NEW.other_deductions_note       WHERE OLD.other_deductions_note       IS NOT NEW.other_deductions_note
        UNION ALL SELECT 'diligence_forfeit_reason',    OLD.diligence_forfeit_reason,    NEW.diligence_forfeit_reason    WHERE OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
    );
END;

-- Self-record INSIDE this transaction (see 157's header for why: the runner
-- records the filename in a SEPARATE statement after executescript has
-- already committed, so a crash in that window would leave the schema
-- applied but unrecorded — and because this file is deliberately not
-- re-runnable, the next boot would retry it and die on `duplicate column
-- name: carried_in`). INSERT OR IGNORE lets this self-record safely.
INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('178_payroll_carry_forward.sql', 'auto');

COMMIT;
