-- 110_finance_customer_perf_indexes.sql
-- Performance indexes for the customer / sales / finance pages, found by a
-- full EXPLAIN QUERY PLAN audit after the dashboard fix (mig 109).
--
-- WORST OFFENDER: the customer list (/m/customers + desktop) runs two
-- correlated subqueries over sales_transactions PER CUSTOMER (~1,477 customers,
-- ~20k sales rows). With no index on sales_transactions.customer each subquery
-- was a full scan -> measured 1,937ms warm on local SSD (far worse on Railway's
-- network volume with a cold cache). idx_st_customer drops it to ~1.9ms (~1000x).
--
-- The other four convert full SCANs into index seeks/range-scans on the hot
-- finance/product pages (cheap warm-local today, but they degrade on prod's cold
-- volume and as the tables grow):
--   - sales_transactions(customer_code) : call card, peer pricing, AR follow-up,
--                                          marketplace matching
--   - sales_transactions(date_iso)      : /sales list, trade dashboard, revenue,
--                                          AR aging (date-range filters + ORDER BY)
--   - transactions(product_id)          : product detail stock-history (the
--                                          transactions table had no product_id
--                                          index; only created_at from mig 109)
--   - purchase_transactions(date_iso)   : trade dashboard purchases aggregates
--
-- All additive read-only indexes: no schema/data change, no table rebuild, tiny
-- disk cost, negligible write overhead at this scale.
--
-- Apply:
--   sqlite3 /path/to/inventory.db < 110_finance_customer_perf_indexes.sql
--
-- Rollback: 110_finance_customer_perf_indexes.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE INDEX IF NOT EXISTS idx_st_customer
    ON sales_transactions(customer);

CREATE INDEX IF NOT EXISTS idx_st_customer_code
    ON sales_transactions(customer_code);

CREATE INDEX IF NOT EXISTS idx_st_date_iso
    ON sales_transactions(date_iso);

CREATE INDEX IF NOT EXISTS idx_txn_product_id
    ON transactions(product_id);

CREATE INDEX IF NOT EXISTS idx_pt_date_iso
    ON purchase_transactions(date_iso);

COMMIT;
