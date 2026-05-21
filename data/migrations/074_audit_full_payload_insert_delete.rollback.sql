-- Rollback for mig 074 — restore the previous INSERT/DELETE trigger payloads
-- from mig 071/072.

BEGIN;

-- ── 1. audit_payroll_items_insert — restore mig 072 version ─────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_insert;
CREATE TRIGGER audit_payroll_items_insert
AFTER INSERT ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', NEW.id, 'INSERT',
        json_object(
            'run_id',                   NEW.run_id,
            'employee_id',              NEW.employee_id,
            'salary_rate',              NEW.salary_rate,
            'base_amount',              NEW.base_amount,
            'diligence_allowance',      NEW.diligence_allowance,
            'diligence_forfeited',      NEW.diligence_forfeited,
            'sso_employee',             NEW.sso_employee,
            'salary_advance_deduction', NEW.salary_advance_deduction,
            'gross',                    NEW.gross,
            'net_pay',                  NEW.net_pay
        )
    );
END;

-- ── 2. audit_payroll_items_delete — restore mig 072 version ─────────────────
DROP TRIGGER IF EXISTS audit_payroll_items_delete;
CREATE TRIGGER audit_payroll_items_delete
BEFORE DELETE ON payroll_items
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'payroll_items', OLD.id, 'DELETE',
        json_object(
            'run_id',      OLD.run_id,
            'employee_id', OLD.employee_id,
            'gross',       OLD.gross,
            'net_pay',     OLD.net_pay
        )
    );
END;

-- ── 3. audit_salary_advances_insert — restore mig 071 version ───────────────
DROP TRIGGER IF EXISTS audit_salary_advances_insert;
CREATE TRIGGER audit_salary_advances_insert
AFTER INSERT ON salary_advances
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salary_advances', NEW.id, 'INSERT',
        json_object(
            'employee_id',  NEW.employee_id,
            'advance_date', NEW.advance_date,
            'amount',       NEW.amount,
            'note',         NEW.note
        )
    );
END;

DELETE FROM applied_migrations WHERE filename = '074_audit_full_payload_insert_delete.sql';
COMMIT;
