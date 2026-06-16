-- 109_dashboard_perf_indexes.rollback.sql
-- Drops the dashboard performance indexes added in 109_dashboard_perf_indexes.sql.
-- After running this, also: DELETE FROM applied_migrations WHERE filename='109_dashboard_perf_indexes.sql';

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_st_product_id;
DROP INDEX IF EXISTS idx_txn_created_at;

COMMIT;
