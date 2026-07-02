-- 123_cashbook_manual_entry.rollback.sql
-- SQLite >=3.35 DROP COLUMN; all 4 added columns are plain nullable columns
-- (no CHECK constraints reference them, so no table rebuild needed).
-- Run manually; the migration runner does not auto-rollback.
BEGIN;

DROP INDEX IF EXISTS idx_cashbook_txn_payroll_item;

ALTER TABLE cashbook_transactions DROP COLUMN payroll_item_id;
ALTER TABLE cashbook_transactions DROP COLUMN payroll_run_id;
ALTER TABLE cashbook_transactions DROP COLUMN created_by;

ALTER TABLE employees DROP COLUMN default_cashbook_account_id;

DELETE FROM applied_migrations WHERE filename = '123_cashbook_manual_entry.sql';

COMMIT;
