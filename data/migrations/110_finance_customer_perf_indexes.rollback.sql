-- 110_finance_customer_perf_indexes.rollback.sql
-- Drops the finance/customer performance indexes added in
-- 110_finance_customer_perf_indexes.sql.
-- After running this, also: DELETE FROM applied_migrations WHERE filename='110_finance_customer_perf_indexes.sql';

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_st_customer;
DROP INDEX IF EXISTS idx_st_customer_code;
DROP INDEX IF EXISTS idx_st_date_iso;
DROP INDEX IF EXISTS idx_txn_product_id;
DROP INDEX IF EXISTS idx_pt_date_iso;

COMMIT;
