-- 131: employees date columns — '' (posted by the HTML form for a blank date
-- input, stored verbatim before the hr_queries normalize fix) must be NULL.
-- The payroll generate filter (end_date IS NULL OR end_date >= ?) reads ''
-- as an always-past end date and silently drops the employee from every run
-- (hit EMP001 + EMP008 on prod, found 2026-07-06).
UPDATE employees SET start_date = NULL WHERE start_date = '';
UPDATE employees SET end_date = NULL WHERE end_date = '';
UPDATE employees SET probation_end_date = NULL WHERE probation_end_date = '';
