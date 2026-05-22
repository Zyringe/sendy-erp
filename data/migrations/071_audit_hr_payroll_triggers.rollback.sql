-- Rollback for mig 071 — drop the audit triggers added for HR/payroll tables.
-- Leaves audit_log itself + mig 070 triggers untouched.
BEGIN;
DROP TRIGGER IF EXISTS audit_employees_insert;
DROP TRIGGER IF EXISTS audit_employees_update;
DROP TRIGGER IF EXISTS audit_employees_delete;
DROP TRIGGER IF EXISTS audit_employee_salary_history_insert;
DROP TRIGGER IF EXISTS audit_employee_salary_history_delete;
DROP TRIGGER IF EXISTS audit_payroll_runs_insert;
DROP TRIGGER IF EXISTS audit_payroll_runs_update;
DROP TRIGGER IF EXISTS audit_payroll_runs_delete;
DROP TRIGGER IF EXISTS audit_payroll_items_update;
DROP TRIGGER IF EXISTS audit_salary_advances_insert;
DROP TRIGGER IF EXISTS audit_salary_advances_update;
DROP TRIGGER IF EXISTS audit_salary_advances_delete;
DROP TRIGGER IF EXISTS audit_leave_requests_insert;
DROP TRIGGER IF EXISTS audit_leave_requests_update;
DROP TRIGGER IF EXISTS audit_leave_requests_delete;
DELETE FROM applied_migrations WHERE filename = '071_audit_hr_payroll_triggers.sql';
COMMIT;
