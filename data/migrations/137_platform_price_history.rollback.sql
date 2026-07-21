-- 137_platform_price_history.rollback.sql
-- Reverse of 137. Additive migration (new table + indexes + trigger); safe to drop.

BEGIN;

DROP TRIGGER IF EXISTS platform_skus_price_history_update;
DROP INDEX   IF EXISTS idx_plat_price_hist_variation;
DROP INDEX   IF EXISTS idx_plat_price_hist_product;
DROP TABLE   IF EXISTS platform_price_history;

COMMIT;
