-- Rollback 105 — drop payout_batches table.
--
-- The payout_batch_id column added to marketplace_orders is additive + nullable.
-- SQLite (pre-3.35) cannot DROP COLUMN, so following project convention for
-- additive-only migrations (see mig 100 comments) we leave the column in place
-- on rollback — it is NULL for all rows and harmless. Only the new table and
-- its index are removed.
--
-- If a full column drop is needed in future, use the table-rebuild pattern from
-- 100_marketplace_settlement.rollback.sql.

DROP INDEX IF EXISTS idx_marketplace_orders_payout_batch;
DROP TABLE IF EXISTS payout_batches;

DELETE FROM applied_migrations WHERE filename = '105_payout_batches.sql';
