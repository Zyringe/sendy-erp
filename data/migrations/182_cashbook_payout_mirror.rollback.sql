-- data/migrations/182_cashbook_payout_mirror.rollback.sql
-- SQLite >=3.35 DROP COLUMN. Run manually; the migration runner does not
-- auto-rollback. Dropping these columns un-locks every payout-sourced row
-- (they become indistinguishable from manual rows) — only safe if nothing
-- downstream still expects the lock.

BEGIN;
DROP INDEX IF EXISTS idx_cashbook_txn_payout_natural_key;
ALTER TABLE cashbook_transactions DROP COLUMN payout_platform;
ALTER TABLE cashbook_transactions DROP COLUMN payout_deposit_date;
ALTER TABLE cashbook_transactions DROP COLUMN payout_amount;
ALTER TABLE cashbook_transactions DROP COLUMN payout_occurrence;
DELETE FROM applied_migrations WHERE filename = '182_cashbook_payout_mirror.sql';
COMMIT;
