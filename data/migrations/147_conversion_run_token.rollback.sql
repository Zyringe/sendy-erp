-- ============================================================================
-- Rollback for 147 — drop conversion_cost_log.run_token + its unique index.
--
-- SQLite 3.35+ supports DROP COLUMN directly, and this column has no FK, no
-- generated-column dependency and no view referencing it, so no table rebuild
-- is needed. Drop the index first: dropping a column that a partial index
-- references errors otherwise.
--
-- Genuinely lossy, and that is correct here: run_token is bookkeeping for the
-- replay guard, not business data. Rolling back means giving up replay
-- protection for runs recorded after 147, which is exactly the pre-147 state.
-- No stock, cost or document number is touched.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_conversion_cost_log_run_token;

ALTER TABLE conversion_cost_log DROP COLUMN run_token;

COMMIT;
