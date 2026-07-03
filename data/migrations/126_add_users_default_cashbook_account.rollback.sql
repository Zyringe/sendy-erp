-- data/migrations/126_add_users_default_cashbook_account.rollback.sql
-- SQLite >=3.35 DROP COLUMN; default_cashbook_account_id is a plain nullable
-- column (mirrors mig 119's from_account_id rollback pattern).
-- Run manually; the migration runner does not auto-rollback.

BEGIN;
ALTER TABLE users DROP COLUMN default_cashbook_account_id;
DELETE FROM applied_migrations WHERE filename = '126_add_users_default_cashbook_account.sql';
COMMIT;
