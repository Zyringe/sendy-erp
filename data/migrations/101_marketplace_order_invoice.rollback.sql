-- Rollback 101_marketplace_order_invoice.sql
-- Additive table (no data transform), so a plain DROP is the correct reverse.
-- After running this, also: DELETE FROM applied_migrations WHERE filename='101_marketplace_order_invoice.sql';

BEGIN;

DROP INDEX IF EXISTS idx_marketplace_order_invoice_doc_base;
DROP TABLE IF EXISTS marketplace_order_invoice;

COMMIT;
