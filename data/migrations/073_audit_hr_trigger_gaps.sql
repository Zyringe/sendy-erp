-- ============================================================================
-- Migration 073 — close audit_log gaps surfaced by codex review of mig 071/072
--
-- Findings (from adversarial review 2026-05-21):
--   1. salary_advances UPDATE: missing employee_id watch + raw_name/
--      import_batch_id in payload. Matching/correcting an unmatched advance
--      (employee_id NULL → resolved) was silent.
--   2. payroll_items UPDATE (mig 072): missing other_additions_note,
--      other_deductions_note, diligence_forfeit_reason. Editing the "why"
--      for an override was silent; editing amount + note logged amount only.
--   3. employee_salary_history: no UPDATE trigger at all. Direct correction
--      of monthly_salary/effective_date drives payroll math but was silent.
--   4. leave_requests UPDATE: missing reason + has_medical_cert. Leave
--      reasons affect unpaid-leave math + diligence forfeit reasoning.
--
-- Approach
--   Forward-only fix. Mig 071/072 already applied on Put's dev DB +
--   anyone who restored from backup since, so editing those files in
--   place would skew prod vs fresh-install state (runner is filename-
--   keyed). DROP + recreate the four UPDATE triggers; add the missing
--   salary-history UPDATE trigger.
--
-- Rollback restores the mig 071/072 versions verbatim.
-- ============================================================================

BEGIN;

-- ── 1. salary_advances UPDATE — add employee_id + raw_name + import_batch_id ─
DROP TRIGGER IF EXISTS audit_salary_advances_update;
CREATE TRIGGER audit_salary_advances_update
AFTER UPDATE ON salary_advances
WHEN (
       OLD.amount             IS NOT NEW.amount
    OR OLD.advance_date       IS NOT NEW.advance_date
    OR OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
    OR OLD.note               IS NOT NEW.note
    OR OLD.employee_id        IS NOT NEW.employee_id
    OR OLD.raw_name           IS NOT NEW.raw_name
    OR OLD.import_batch_id    IS NOT NEW.import_batch_id
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'salary_advances', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'amount'             AS field, OLD.amount             AS old_v, NEW.amount             AS new_v WHERE OLD.amount             IS NOT NEW.amount
        UNION ALL SELECT 'advance_date',       OLD.advance_date,       NEW.advance_date       WHERE OLD.advance_date       IS NOT NEW.advance_date
        UNION ALL SELECT 'deducted_in_run_id', OLD.deducted_in_run_id, NEW.deducted_in_run_id WHERE OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
        UNION ALL SELECT 'note',               OLD.note,               NEW.note               WHERE OLD.note               IS NOT NEW.note
        UNION ALL SELECT 'employee_id',        OLD.employee_id,        NEW.employee_id        WHERE OLD.employee_id        IS NOT NEW.employee_id
        UNION ALL SELECT 'raw_name',           OLD.raw_name,           NEW.raw_name           WHERE OLD.raw_name           IS NOT NEW.raw_name
        UNION ALL SELECT 'import_batch_id',    OLD.import_batch_id,    NEW.import_batch_id    WHERE OLD.import_batch_id    IS NOT NEW.import_batch_id
    );
END;

-- ── 2. payroll_items UPDATE — add note + diligence_forfeit_reason fields ────
DROP TRIGGER IF EXISTS audit_payroll_items_update;
CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                       IS NOT NEW.bonus
    OR OLD.other_additions             IS NOT NEW.other_additions
    OR OLD.other_deductions            IS NOT NEW.other_deductions
    OR OLD.diligence_allowance         IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee                IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
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
        UNION ALL SELECT 'diligence_allowance',         OLD.diligence_allowance,         NEW.diligence_allowance         WHERE OLD.diligence_allowance         IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',         OLD.diligence_forfeited,         NEW.diligence_forfeited         WHERE OLD.diligence_forfeited         IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',                OLD.sso_employee,                NEW.sso_employee                WHERE OLD.sso_employee                IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction',    OLD.salary_advance_deduction,    NEW.salary_advance_deduction    WHERE OLD.salary_advance_deduction    IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                       OLD.gross,                       NEW.gross                       WHERE OLD.gross                       IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',                     OLD.net_pay,                     NEW.net_pay                     WHERE OLD.net_pay                     IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                        OLD.note,                        NEW.note                        WHERE OLD.note                        IS NOT NEW.note
        UNION ALL SELECT 'other_additions_note',        OLD.other_additions_note,        NEW.other_additions_note        WHERE OLD.other_additions_note        IS NOT NEW.other_additions_note
        UNION ALL SELECT 'other_deductions_note',       OLD.other_deductions_note,       NEW.other_deductions_note       WHERE OLD.other_deductions_note       IS NOT NEW.other_deductions_note
        UNION ALL SELECT 'diligence_forfeit_reason',    OLD.diligence_forfeit_reason,    NEW.diligence_forfeit_reason    WHERE OLD.diligence_forfeit_reason    IS NOT NEW.diligence_forfeit_reason
    );
END;

-- ── 3. employee_salary_history UPDATE — was missing entirely ────────────────
DROP TRIGGER IF EXISTS audit_employee_salary_history_update;
CREATE TRIGGER audit_employee_salary_history_update
AFTER UPDATE ON employee_salary_history
WHEN (
       OLD.monthly_salary  IS NOT NEW.monthly_salary
    OR OLD.effective_date  IS NOT NEW.effective_date
    OR OLD.reason          IS NOT NEW.reason
    OR OLD.note            IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'employee_salary_history', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'monthly_salary'  AS field, OLD.monthly_salary  AS old_v, NEW.monthly_salary  AS new_v WHERE OLD.monthly_salary  IS NOT NEW.monthly_salary
        UNION ALL SELECT 'effective_date',  OLD.effective_date,  NEW.effective_date  WHERE OLD.effective_date  IS NOT NEW.effective_date
        UNION ALL SELECT 'reason',          OLD.reason,          NEW.reason          WHERE OLD.reason          IS NOT NEW.reason
        UNION ALL SELECT 'note',            OLD.note,            NEW.note            WHERE OLD.note            IS NOT NEW.note
    );
END;

-- ── 4. leave_requests UPDATE — add reason + has_medical_cert ────────────────
DROP TRIGGER IF EXISTS audit_leave_requests_update;
CREATE TRIGGER audit_leave_requests_update
AFTER UPDATE ON leave_requests
WHEN (
       OLD.status            IS NOT NEW.status
    OR OLD.start_date        IS NOT NEW.start_date
    OR OLD.end_date          IS NOT NEW.end_date
    OR OLD.days              IS NOT NEW.days
    OR OLD.leave_type_id     IS NOT NEW.leave_type_id
    OR OLD.reason            IS NOT NEW.reason
    OR OLD.has_medical_cert  IS NOT NEW.has_medical_cert
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'leave_requests', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'            AS field, OLD.status            AS old_v, NEW.status            AS new_v WHERE OLD.status            IS NOT NEW.status
        UNION ALL SELECT 'start_date',        OLD.start_date,        NEW.start_date        WHERE OLD.start_date        IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',          OLD.end_date,          NEW.end_date          WHERE OLD.end_date          IS NOT NEW.end_date
        UNION ALL SELECT 'days',              OLD.days,              NEW.days              WHERE OLD.days              IS NOT NEW.days
        UNION ALL SELECT 'leave_type_id',     OLD.leave_type_id,     NEW.leave_type_id     WHERE OLD.leave_type_id     IS NOT NEW.leave_type_id
        UNION ALL SELECT 'reason',            OLD.reason,            NEW.reason            WHERE OLD.reason            IS NOT NEW.reason
        UNION ALL SELECT 'has_medical_cert',  OLD.has_medical_cert,  NEW.has_medical_cert  WHERE OLD.has_medical_cert  IS NOT NEW.has_medical_cert
    );
END;

COMMIT;
