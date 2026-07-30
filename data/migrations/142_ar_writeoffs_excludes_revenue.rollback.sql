-- ============================================================================
-- Rollback for 142 — drop ar_writeoffs.excludes_revenue.
--
-- Dropping the column restores the pre-142 behaviour exactly: every revenue
-- query falls back to counting the three giveaway invoices as sales again.
-- Nothing else read the column, and no data outside it is affected.
--
-- Rows written after the forward migration keep all their other columns —
-- DROP COLUMN rewrites the table in place, so this is not a
-- restore-from-snapshot (per the rename/cosmetic-migration safety rule, a
-- pure column drop has no snapshot to go stale).
--
-- Requires SQLite >= 3.35 for ALTER TABLE ... DROP COLUMN (local dev is
-- 3.51; Railway's image is newer still). CHECK constraints referencing the
-- column go with it.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE ar_writeoffs DROP COLUMN excludes_revenue;

DELETE FROM applied_migrations WHERE filename = '142_ar_writeoffs_excludes_revenue.sql';

COMMIT;
