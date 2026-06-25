-- Adds display-ordering + payroll-inclusion flags to employees.
-- sort_order: roster display order (owner/family first); decouples display
--   from emp_code so codes are never reordered (see id==emp_code decision).
-- on_payroll: 0 = on roster but never in a payroll run (e.g. ผู้ถือหุ้น).
-- NOTE: do NOT self-insert into applied_migrations (runner records it).
BEGIN;

ALTER TABLE employees ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100;
ALTER TABLE employees ADD COLUMN on_payroll INTEGER NOT NULL DEFAULT 1
                                  CHECK(on_payroll IN (0,1));

-- Seed sort_order to current roster order (emp_code already leads with owner/
-- family). Multiples of 10 leave gaps for future manual reordering.
UPDATE employees
   SET sort_order = CAST(SUBSTR(emp_code, 4) AS INTEGER) * 10
 WHERE emp_code GLOB 'EMP[0-9][0-9][0-9]';

COMMIT;
