-- 130_price_history_source.sql
-- Adds source/provenance tracking to product_price_history so "where did this
-- price come from" is answerable from the DB itself, not just memory.
--
-- product_price_history is populated by the trigger product_price_history_update
-- (AFTER UPDATE ON products, added in 008). A trigger can't know WHY a change
-- happened, so this migration adds a context mechanism:
--   1. a nullable `source` column on product_price_history (NULL = unspecified),
--   2. a single-row context table `price_change_source` the trigger reads,
--   3. a rewritten trigger that stamps source from that context row.
-- The app write-path (models.update_product / recalculate_product_wacc) sets the
-- context row in the SAME transaction right before UPDATE products; SQLite's
-- single-writer semantics mean the trigger reads that connection's own value.
-- Paths that set nothing default to NULL — nothing breaks.
--
-- Apply: restart the app (database.py::run_pending_migrations auto-applies).
-- Rollback: data/migrations/130_price_history_source.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations (the runner records it).

BEGIN;

-- 1. nullable source column (NULL = unspecified / legacy pre-130 rows)
ALTER TABLE product_price_history ADD COLUMN source TEXT;

-- 2. single-row context table the trigger reads to learn WHY a change happened.
CREATE TABLE IF NOT EXISTS price_change_source (
    id     INTEGER PRIMARY KEY CHECK(id = 1),
    source TEXT
);
INSERT OR IGNORE INTO price_change_source (id, source) VALUES (1, NULL);

-- 3. rewrite the trigger to stamp source from the context row.
--    WHEN clause + UNION-ALL body are unchanged from 008; the only addition is
--    the source column pulled from price_change_source.
DROP TRIGGER IF EXISTS product_price_history_update;
CREATE TRIGGER product_price_history_update
AFTER UPDATE ON products
WHEN (
       OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
)
BEGIN
    INSERT INTO product_price_history (product_id, field_name, old_value, new_value, source)
    SELECT NEW.id, field, old_v, new_v,
           (SELECT source FROM price_change_source WHERE id = 1)
    FROM (
                  SELECT 'cost_price'          AS field, OLD.cost_price          AS old_v, NEW.cost_price          AS new_v WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',             OLD.base_sell_price,             NEW.base_sell_price             WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'low_stock_threshold',         OLD.low_stock_threshold,         NEW.low_stock_threshold         WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    );
END;

-- 4. backfill the 6 สีฝุ่น manual price-sets from 2026-07-04 (pids 472-477).
--    These base_sell_price rows (0 -> price) were set by Put by hand.
UPDATE product_price_history
   SET source = 'manual: Put ราคาตั้ง 2026-07-04 (ราคาตั้ง/ลัง ÷ 20)'
 WHERE product_id IN (472,473,474,475,476,477)
   AND field_name = 'base_sell_price'
   AND date(changed_at) = '2026-07-04'
   AND source IS NULL;

COMMIT;
