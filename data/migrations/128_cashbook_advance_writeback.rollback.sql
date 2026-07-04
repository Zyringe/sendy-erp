-- data/migrations/128_cashbook_advance_writeback.rollback.sql
-- SQLite >=3.35 DROP COLUMN. salary_advance_id is a plain nullable FK column
-- (mirrors mig 126's rollback). The seeded category is left in place — dropping
-- it could orphan any advance rows entered before rollback; harmless to keep.
-- Run manually; the migration runner does not auto-rollback.

BEGIN;
DROP INDEX IF EXISTS idx_cashbook_txn_salary_advance;
ALTER TABLE cashbook_transactions DROP COLUMN salary_advance_id;
DELETE FROM applied_migrations WHERE filename = '128_cashbook_advance_writeback.sql';
COMMIT;
