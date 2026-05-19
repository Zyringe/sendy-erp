-- 057_payroll_salary_advance.rollback.sql
-- SQLite does not support DROP COLUMN in all versions (requires 3.35+).
-- Safe rollback: recreate payroll_items without salary_advance_deduction,
-- copying data. Run this manually when needed; the migration runner does not
-- auto-rollback.

BEGIN;

-- Step 1: recreate table without salary_advance_deduction
CREATE TABLE payroll_items_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                  INTEGER NOT NULL REFERENCES payroll_runs(id),
    employee_id             INTEGER NOT NULL REFERENCES employees(id),
    salary_rate             REAL    NOT NULL DEFAULT 0,
    base_amount             REAL    NOT NULL DEFAULT 0,
    unpaid_leave_days       REAL    NOT NULL DEFAULT 0,
    unpaid_leave_deduction  REAL    NOT NULL DEFAULT 0,
    diligence_allowance     REAL    NOT NULL DEFAULT 0,
    diligence_forfeited     INTEGER NOT NULL DEFAULT 0 CHECK(diligence_forfeited IN (0,1)),
    diligence_forfeit_reason TEXT   CHECK(diligence_forfeit_reason IN ('leave','late')
                                          OR diligence_forfeit_reason IS NULL),
    bonus                   REAL    NOT NULL DEFAULT 0,
    other_additions         REAL    NOT NULL DEFAULT 0,
    other_additions_note    TEXT,
    other_deductions        REAL    NOT NULL DEFAULT 0,
    other_deductions_note   TEXT,
    sso_employee            REAL    NOT NULL DEFAULT 0,
    sso_employer            REAL    NOT NULL DEFAULT 0,
    commission_amount       REAL    NOT NULL DEFAULT 0,
    gross                   REAL    NOT NULL DEFAULT 0,
    net_pay                 REAL    NOT NULL DEFAULT 0,
    note                    TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, employee_id)
);

-- Step 2: copy data (drop salary_advance_deduction column)
INSERT INTO payroll_items_new
    (id, run_id, employee_id, salary_rate, base_amount,
     unpaid_leave_days, unpaid_leave_deduction, diligence_allowance,
     diligence_forfeited, diligence_forfeit_reason, bonus, other_additions,
     other_additions_note, other_deductions, other_deductions_note,
     sso_employee, sso_employer, commission_amount, gross, net_pay,
     note, created_at)
SELECT id, run_id, employee_id, salary_rate, base_amount,
       unpaid_leave_days, unpaid_leave_deduction, diligence_allowance,
       diligence_forfeited, diligence_forfeit_reason, bonus, other_additions,
       other_additions_note, other_deductions, other_deductions_note,
       sso_employee, sso_employer, commission_amount, gross, net_pay,
       note, created_at
FROM payroll_items;

-- Step 3: swap
DROP TABLE payroll_items;
ALTER TABLE payroll_items_new RENAME TO payroll_items;

-- Step 4: recreate indexes
CREATE INDEX IF NOT EXISTS idx_payroll_items_run ON payroll_items(run_id);
CREATE INDEX IF NOT EXISTS idx_payroll_items_emp ON payroll_items(employee_id);

-- Step 5: record rollback
DELETE FROM applied_migrations WHERE filename = '057_payroll_salary_advance.sql';

COMMIT;
