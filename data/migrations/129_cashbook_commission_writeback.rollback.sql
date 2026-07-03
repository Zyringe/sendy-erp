-- data/migrations/129_cashbook_commission_writeback.rollback.sql
-- SQLite >=3.35 DROP COLUMN. commission_payout_id is a plain nullable FK
-- column (mirrors mig 128's rollback). real_name is dropped too (the
-- aliases are re-derivable by re-applying this migration). The seeded
-- category is left in place — dropping it could orphan any commission rows
-- entered before rollback; harmless to keep (mirrors mig 128's rollback note).
-- Run manually; the migration runner does not auto-rollback.

BEGIN;
DROP INDEX IF EXISTS idx_cashbook_txn_commission_payout;
ALTER TABLE cashbook_transactions DROP COLUMN commission_payout_id;
ALTER TABLE salespersons DROP COLUMN real_name;
DELETE FROM applied_migrations WHERE filename = '129_cashbook_commission_writeback.sql';
COMMIT;
