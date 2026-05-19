-- 055_cashbook.rollback.sql
-- Rolls back 055_cashbook.sql.
--
-- Drops every cashbook table (cashbook_accounts, cashbook_categories,
-- cashbook_transactions, salary_advances) and their indexes, then removes
-- the applied_migrations bookkeeping row so the runner will re-apply it.
-- No employees-table change was made by 055, so nothing to undo there.
--
-- Pre-flight:
--   1. Stop the Sendy Flask app.
--   2. Backup the DB (any rollback is destructive):
--        DEST=data/backups/inventory-pre-rollback055-$(date +%Y-%m-%d-%H%M%S).db
--        sqlite3 inventory.db ".backup '$DEST'"
--   3. Confirm no other tables FK to cashbook tables yet (none in v1).

BEGIN;

DROP INDEX IF EXISTS idx_salary_advances_run;
DROP INDEX IF EXISTS idx_salary_advances_emp;
DROP INDEX IF EXISTS idx_cashbook_txn_category;
DROP INDEX IF EXISTS idx_cashbook_txn_date;
DROP INDEX IF EXISTS idx_cashbook_txn_vat_account;
DROP INDEX IF EXISTS idx_cashbook_txn_account_date;
DROP INDEX IF EXISTS idx_cashbook_accounts_code;

DROP TABLE IF EXISTS salary_advances;
DROP TABLE IF EXISTS cashbook_transactions;
DROP TABLE IF EXISTS cashbook_categories;
DROP TABLE IF EXISTS cashbook_accounts;

DELETE FROM applied_migrations WHERE filename = '055_cashbook.sql';

COMMIT;
