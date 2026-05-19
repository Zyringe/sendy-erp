-- 057_payroll_salary_advance.sql
-- Adds salary_advance_deduction to payroll_items.
--
-- Rationale: เบิกเงินล่วงหน้า (salary_advances ledger, mig 055) must reduce
-- net pay. payroll_items now carries the per-line deducted amount so payslips
-- / exports can show it and so net_pay is reproducible from the row alone.
--
-- Engine semantics (hr.py::_build_item / finalize_run): a draft run sums all
-- of the employee's advances dated on/before period_end whose deducted_in_run_id
-- is NULL or already points at THIS run (re-runnable without doubling).
-- finalize_run() stamps deducted_in_run_id so a later month does not re-deduct.
--
-- Apply:    via database.py::run_pending_migrations (automatic on boot)
-- Rollback: 057_payroll_salary_advance.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
-- every migration it executes; a self-insert would duplicate-key crash on boot.

BEGIN;

ALTER TABLE payroll_items
    ADD COLUMN salary_advance_deduction REAL NOT NULL DEFAULT 0;

COMMIT;
