-- ============================================================================
-- Migration 074 — full-payload INSERT/DELETE triggers (codex review pass 2)
--
-- Findings
--   1. audit_payroll_items_insert (mig 072) captured only 8 of 17 material
--      columns. unpaid_leave_*, bonus, other_additions/deductions,
--      sso_employer, commission_amount, diligence_forfeit_reason, note all
--      missing — can't reconstruct what generate_run produced.
--   2. audit_payroll_items_delete (mig 072) captured only 4 columns —
--      loses the full money snapshot on regenerate-wipe.
--   3. audit_salary_advances_insert (mig 071) omitted raw_name +
--      source_file + import_batch_id — unmatched-advance audit can't tell
--      which import batch / source sheet produced the row.
--
-- Approach
--   Forward-only fix (mig 071/072 + 073 already applied on dev DB).
--   DROP + recreate 3 triggers. Rollback restores the previous payloads.
-- ============================================================================

BEGIN;

-- ── 1. audit_payroll_items_insert — full payload ────────────────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_insert;
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
            'sso_employee',              NEW.sso_employee,
            'sso_employer',              NEW.sso_employer,
            'commission_amount',         NEW.commission_amount,
            'salary_advance_deduction',  NEW.salary_advance_deduction,
            'gross',                     NEW.gross,
            'net_pay',                   NEW.net_pay,
            'note',                      NEW.note
        )
    );
END;

-- ── 2. audit_payroll_items_delete — full payload ────────────────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_delete;
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
            'sso_employee',              OLD.sso_employee,
            'sso_employer',              OLD.sso_employer,
            'commission_amount',         OLD.commission_amount,
            'salary_advance_deduction',  OLD.salary_advance_deduction,
            'gross',                     OLD.gross,
            'net_pay',                   OLD.net_pay,
            'note',                      OLD.note
        )
    );
END;

-- ── 3. audit_salary_advances_insert — add raw_name/source_file/batch ────────
DROP TRIGGER IF EXISTS audit_salary_advances_insert;
CREATE TRIGGER audit_salary_advances_insert
AFTER INSERT ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', NEW.id, 'INSERT',
        json_object(
            'employee_id',     NEW.employee_id,
            'advance_date',    NEW.advance_date,
            'amount',          NEW.amount,
            'raw_name',        NEW.raw_name,
            'note',            NEW.note,
            'source_file',     NEW.source_file,
            'import_batch_id', NEW.import_batch_id
        )
    );
END;

COMMIT;
