-- 054_hr_module.rollback.sql
-- Rolls back 054_hr_module.sql.
--
-- Drops every HR table (incl. seeded leave types, hr_config, the two
-- contract employees + their salary history) and removes the
-- applied_migrations bookkeeping row so the runner will re-apply it.
--
-- Pre-flight:
--   1. Stop the Sendy Flask app.
--   2. Backup the DB (any rollback is destructive):
--        DEST=data/backups/inventory-pre-rollback054-$(date +%Y-%m-%d-%H%M%S).db
--        sqlite3 inventory.db ".backup '$DEST'"
--   3. Confirm no other tables FK to HR tables yet (none in v1).

BEGIN;

DROP INDEX IF EXISTS idx_company_holidays_company;
DROP INDEX IF EXISTS idx_payroll_items_emp;
DROP INDEX IF EXISTS idx_payroll_items_run;
DROP INDEX IF EXISTS idx_payroll_runs_company;
DROP INDEX IF EXISTS idx_leave_req_dates;
DROP INDEX IF EXISTS idx_leave_req_type;
DROP INDEX IF EXISTS idx_leave_req_emp;
DROP INDEX IF EXISTS idx_leave_entl_emp;
DROP INDEX IF EXISTS idx_salary_hist_emp;
DROP INDEX IF EXISTS idx_employees_user;
DROP INDEX IF EXISTS idx_employees_active;
DROP INDEX IF EXISTS idx_employees_company;

DROP TABLE IF EXISTS company_holidays;
DROP TABLE IF EXISTS hr_config;
DROP TABLE IF EXISTS payroll_items;
DROP TABLE IF EXISTS payroll_runs;
DROP TABLE IF EXISTS leave_requests;
DROP TABLE IF EXISTS employee_leave_entitlements;
DROP TABLE IF EXISTS leave_types;
DROP TABLE IF EXISTS employee_salary_history;
DROP TABLE IF EXISTS employees;

DELETE FROM applied_migrations WHERE filename = '054_hr_module.sql';

COMMIT;
