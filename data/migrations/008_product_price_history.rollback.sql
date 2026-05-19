-- 008_product_price_history.rollback.sql
-- Rolls back 008_product_price_history.sql.
-- Drops the trigger, index and table. Any captured history rows are
-- discarded — back up product_price_history first if you need them.
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/008_product_price_history.rollback.sql
--
-- Verify (each should be 0):
--   sqlite3 .../inventory.db "SELECT COUNT(*) FROM sqlite_master WHERE name='product_price_history';"
--   sqlite3 .../inventory.db "SELECT COUNT(*) FROM sqlite_master WHERE name='idx_pph_product_time';"
--   sqlite3 .../inventory.db "SELECT COUNT(*) FROM sqlite_master WHERE name='product_price_history_update';"

BEGIN;

DROP TRIGGER IF EXISTS product_price_history_update;
DROP INDEX   IF EXISTS idx_pph_product_time;
DROP TABLE   IF EXISTS product_price_history;

COMMIT;
