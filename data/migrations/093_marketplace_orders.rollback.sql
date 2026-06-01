-- Rollback 093 — drop the marketplace order capture tables.
-- Purely additive forward migration, so a clean drop fully reverts it.

BEGIN;

DROP INDEX IF EXISTS idx_marketplace_items_unmapped;
DROP INDEX IF EXISTS idx_marketplace_items_order;
DROP TABLE IF EXISTS marketplace_order_items;

DROP INDEX IF EXISTS idx_marketplace_orders_status;
DROP INDEX IF EXISTS idx_marketplace_orders_date;
DROP TABLE IF EXISTS marketplace_orders;

DELETE FROM applied_migrations WHERE filename = '093_marketplace_orders.sql';

COMMIT;
