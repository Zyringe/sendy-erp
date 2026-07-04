-- 130_price_history_source.rollback.sql
-- Rolls back 130_price_history_source.sql.
-- Restores the pre-130 trigger (008 version, no source), drops the context
-- table and the source column, and de-registers the migration. Any captured
-- `source` values are discarded; the old/new price pairs are untouched.
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/130_price_history_source.rollback.sql
--
-- Verify (each should be 0):
--   SELECT COUNT(*) FROM pragma_table_info('product_price_history') WHERE name='source';
--   SELECT COUNT(*) FROM sqlite_master WHERE name='price_change_source';

BEGIN;

-- restore the 008 trigger (no source) BEFORE dropping the column/table it reads
DROP TRIGGER IF EXISTS product_price_history_update;
CREATE TRIGGER product_price_history_update
AFTER UPDATE ON products
WHEN (
       OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
)
BEGIN
    INSERT INTO product_price_history (product_id, field_name, old_value, new_value)
    SELECT NEW.id, field, old_v, new_v
    FROM (
                  SELECT 'cost_price'          AS field, OLD.cost_price          AS old_v, NEW.cost_price          AS new_v WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',             OLD.base_sell_price,             NEW.base_sell_price             WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'low_stock_threshold',         OLD.low_stock_threshold,         NEW.low_stock_threshold         WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    );
END;

DROP TABLE IF EXISTS price_change_source;
ALTER TABLE product_price_history DROP COLUMN source;

DELETE FROM applied_migrations WHERE filename = '130_price_history_source.sql';

COMMIT;
