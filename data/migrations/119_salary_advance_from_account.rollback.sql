-- data/migrations/119_salary_advance_from_account.rollback.sql
-- SQLite >=3.35 DROP COLUMN; from_account_id is a plain nullable column.
BEGIN;
ALTER TABLE salary_advances DROP COLUMN from_account_id;
DELETE FROM applied_migrations WHERE filename = '119_salary_advance_from_account.sql';
COMMIT;
