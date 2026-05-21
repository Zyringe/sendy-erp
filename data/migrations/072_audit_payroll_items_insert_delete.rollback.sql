-- Rollback for mig 072 — drop INSERT/DELETE triggers, restore mig 071's
-- UPDATE trigger (without `note` in WHEN clause).
BEGIN;
DROP TRIGGER IF EXISTS audit_payroll_items_insert;
DROP TRIGGER IF EXISTS audit_payroll_items_delete;
DROP TRIGGER IF EXISTS audit_payroll_items_update;
-- Restore mig 071 version (no note in WHEN)
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
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'payroll_items', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'bonus' AS field, OLD.bonus AS old_v, NEW.bonus AS new_v WHERE OLD.bonus IS NOT NEW.bonus
        UNION ALL SELECT 'other_additions',      OLD.other_additions,      NEW.other_additions      WHERE OLD.other_additions      IS NOT NEW.other_additions
        UNION ALL SELECT 'other_deductions',     OLD.other_deductions,     NEW.other_deductions     WHERE OLD.other_deductions     IS NOT NEW.other_deductions
        UNION ALL SELECT 'diligence_allowance',  OLD.diligence_allowance,  NEW.diligence_allowance  WHERE OLD.diligence_allowance  IS NOT NEW.diligence_allowance
        UNION ALL SELECT 'diligence_forfeited',  OLD.diligence_forfeited,  NEW.diligence_forfeited  WHERE OLD.diligence_forfeited  IS NOT NEW.diligence_forfeited
        UNION ALL SELECT 'sso_employee',         OLD.sso_employee,         NEW.sso_employee         WHERE OLD.sso_employee         IS NOT NEW.sso_employee
        UNION ALL SELECT 'salary_advance_deduction', OLD.salary_advance_deduction, NEW.salary_advance_deduction WHERE OLD.salary_advance_deduction IS NOT NEW.salary_advance_deduction
        UNION ALL SELECT 'gross',                OLD.gross,                NEW.gross                WHERE OLD.gross                IS NOT NEW.gross
        UNION ALL SELECT 'net_pay',              OLD.net_pay,              NEW.net_pay              WHERE OLD.net_pay              IS NOT NEW.net_pay
    );
END;
DELETE FROM applied_migrations WHERE filename = '072_audit_payroll_items_insert_delete.sql';
COMMIT;
