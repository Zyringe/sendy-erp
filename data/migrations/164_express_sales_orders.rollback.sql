-- Rollback for 164_express_sales_orders.sql — two new tables, so they just go.
BEGIN;
DROP INDEX IF EXISTS idx_sales_orders_date;
DROP INDEX IF EXISTS idx_sales_order_lines_remaining;
DROP TABLE IF EXISTS express_sales_order_lines;
DROP TABLE IF EXISTS express_sales_orders;
COMMIT;
