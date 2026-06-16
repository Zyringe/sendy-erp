-- 107_marketplace_fees_wallet.rollback.sql
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_wallet_txns_platform_time;
DROP INDEX IF EXISTS idx_marketplace_orders_payout_id;
DROP TABLE IF EXISTS marketplace_payouts;
DROP TABLE IF EXISTS marketplace_wallet_txns;
DROP TABLE IF EXISTS marketplace_order_fees;
-- marketplace_orders.payout_id is an additive nullable column; left in place
-- (dropping a column needs a table rebuild and isn't worth the risk).
COMMIT;
