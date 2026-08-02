-- ============================================================================
-- Rollback for 149 — drop system_alerts and its two indexes.
--
-- Lossy by definition: any unresolved operational alert is discarded. That is
-- acceptable because the table is a NOTIFICATION surface, not a system of
-- record — the underlying condition (a WACC cost-identity failure) re-raises
-- and re-alerts the next time the affected product is recalculated. No stock,
-- cost, document or business value lives here.
--
-- Dropping the table drops its indexes automatically; they are named here only
-- for symmetry with the forward migration.
--
-- ⚠ Coordinate with the application: PR3's callers write to this table and
-- /alerts reads it. Roll the code back BEFORE (or with) this migration.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_system_alerts_open;
DROP INDEX IF EXISTS idx_system_alerts_open_dedupe;
DROP TABLE IF EXISTS system_alerts;

DELETE FROM applied_migrations WHERE filename = '149_system_alerts.sql';

COMMIT;
