-- 109_dashboard_perf_indexes.sql
-- Performance indexes for the dashboard ("/") landing page.
--
-- PROBLEM: the dashboard route calls models.count_restock_needed(), whose query
-- runs a correlated EXISTS subquery against sales_transactions (~20k rows) for
-- every active product (~2k). sales_transactions had NO index on product_id, so
-- each EXISTS was a full table scan → ~40M row scans per dashboard load.
-- Measured on a prod snapshot: 0.84-1.05s on local SSD with a warm cache; far
-- worse on Railway's network volume /data with a cold page cache (and "/" is the
-- first page opened, so it bears the cold-start cost). All other dashboard /
-- context-processor queries are <=0.013s.
--
-- FIX: index sales_transactions(product_id). Verified on a snapshot: the plan
-- changes from "SCAN st" to "SEARCH st USING INDEX idx_st_product_id" and the
-- query drops from ~0.84s to ~0.004s (~200x).
--
-- Also index transactions(created_at): transactions (~26k rows) had NO indexes
-- at all, and models.get_recent_transactions() does ORDER BY created_at DESC
-- LIMIT 10 (a full scan + sort). The index turns that into an index scan.
--
-- Both are read-only additive indexes: no schema/data change, tiny disk cost
-- (~a few hundred KB), no table rebuild, negligible write overhead at this scale.
--
-- Apply:
--   sqlite3 /path/to/inventory.db < 109_dashboard_perf_indexes.sql
--
-- Rollback: 109_dashboard_perf_indexes.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE INDEX IF NOT EXISTS idx_st_product_id
    ON sales_transactions(product_id);

CREATE INDEX IF NOT EXISTS idx_txn_created_at
    ON transactions(created_at);

COMMIT;
