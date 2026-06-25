-- SQLite >=3.35 supports DROP COLUMN; Sendy's runtime qualifies.
BEGIN;
ALTER TABLE employees DROP COLUMN on_payroll;
ALTER TABLE employees DROP COLUMN sort_order;
DELETE FROM applied_migrations WHERE filename = '114_hr_sort_order_on_payroll.sql';
COMMIT;
