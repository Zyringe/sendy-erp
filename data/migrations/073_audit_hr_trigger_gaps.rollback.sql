-- Rollback for mig 073 — restore mig 071/072 versions of the UPDATE triggers
-- and drop the new salary_history UPDATE trigger.

BEGIN;

-- ── 1. salary_advances UPDATE — restore mig 071 version ─────────────────────
DROP TRIGGER IF EXISTS audit_salary_advances_update;
CREATE TRIGGER audit_salary_advances_update
AFTER UPDATE ON salary_advances
WHEN (
       OLD.amount             IS NOT NEW.amount
    OR OLD.advance_date       IS NOT NEW.advance_date
    OR OLD.deducted_in_run_id IS NOT NEW.deducted_in_run_id
    OR OLD.note               IS NOT NEW.note
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
    );
END;

-- ── 2. payroll_items UPDATE — restore mig 072 version ───────────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_update;
CREATE TRIGGER audit_payroll_items_update
AFTER UPDATE ON payroll_items
WHEN (
       OLD.bonus                IS NOT NEW.bonus
    OR OLD.other_additions      IS NOT NEW.other_additions
    OR OLD.other_deductions     IS NOT NEW.other_deductions
    OR OLD.diligence_allowance  IS NOT NEW.diligence_allowance
    OR OLD.diligence_forfeited  IS NOT NEW.diligence_forfeited
    OR OLD.sso_employee         IS NOT NEW.sso_employee
    OR OLD.salary_advance_deduction IS NOT NEW.salary_advance_deduction
    OR OLD.gross                IS NOT NEW.gross
    OR OLD.net_pay              IS NOT NEW.net_pay
    OR OLD.note                 IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus'                AS field, OLD.bonus                AS old_v, NEW.bonus                AS new_v WHERE OLD.bonus                IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',      OLD.other_additions,      NEW.other_additions      WHERE OLD.other_additions      IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',     OLD.other_deductions,     NEW.other_deductions     WHERE OLD.other_deductions     IS NOT NEW.other_deductions
        UNION ALL SELECT 'diligence_allowance',  OLD.diligence_allowance,  NEW.diligence_allowance  WHERE OLD.diligence_allowance  IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',  OLD.diligence_forfeited,  NEW.diligence_forfeited  WHERE OLD.diligence_forfeited  IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',         OLD.sso_employee,         NEW.sso_employee         WHERE OLD.sso_employee         IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction', OLD.salary_advance_deduction, NEW.salary_advance_deduction WHERE OLD.salary_advance_deduction IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                OLD.gross,                NEW.gross                WHERE OLD.gross                IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',              OLD.net_pay,              NEW.net_pay              WHERE OLD.net_pay              IS NOT NEW.net_pay
        UNION ALL SELECT 'note',                 OLD.note,                 NEW.note                 WHERE OLD.note                 IS NOT NEW.note
    );
END;

-- ── 3. employee_salary_history UPDATE — drop (didn't exist before 073) ──────
DROP TRIGGER IF EXISTS audit_employee_salary_history_update;

-- ── 4. leave_requests UPDATE — restore mig 071 version ──────────────────────
DROP TRIGGER IF EXISTS audit_leave_requests_update;
CREATE TRIGGER audit_leave_requests_update
AFTER UPDATE ON leave_requests
WHEN (
       OLD.status        IS NOT NEW.status
    OR OLD.start_date    IS NOT NEW.start_date
    OR OLD.end_date      IS NOT NEW.end_date
    OR OLD.days          IS NOT NEW.days
    OR OLD.leave_type_id IS NOT NEW.leave_type_id
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'leave_requests', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'status'        AS field, OLD.status        AS old_v, NEW.status        AS new_v WHERE OLD.status        IS NOT NEW.status
        UNION ALL SELECT 'start_date',    OLD.start_date,    NEW.start_date    WHERE OLD.start_date    IS NOT NEW.start_date
        UNION ALL SELECT 'end_date',      OLD.end_date,      NEW.end_date      WHERE OLD.end_date      IS NOT NEW.end_date
        UNION ALL SELECT 'days',          OLD.days,          NEW.days          WHERE OLD.days          IS NOT NEW.days
        UNION ALL SELECT 'leave_type_id', OLD.leave_type_id, NEW.leave_type_id WHERE OLD.leave_type_id IS NOT NEW.leave_type_id
    );
END;

DELETE FROM applied_migrations WHERE filename = '073_audit_hr_trigger_gaps.sql';
COMMIT;
