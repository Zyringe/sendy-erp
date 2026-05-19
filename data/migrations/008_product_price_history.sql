-- 008_product_price_history.sql
-- Phase E2 of the schema refactor.
-- Creates product_price_history — a structured timeline of pricing-related
-- changes on products. Captures cost_price, base_sell_price and
-- low_stock_threshold transitions as numeric (old, new) pairs so we can
-- query price history without parsing audit_log JSON.
--
-- Trigger pattern matches 003_audit_triggers.sql:
--   - AFTER UPDATE ON products with WHEN clause that fires only when one
--     of the watched fields actually changes (IS NOT handles NULL).
--   - Body uses INSERT … SELECT … UNION ALL with per-row WHERE so only
--     the fields that changed produce a row.
--
-- This co-exists with audit_products_update (003); audit_log keeps the
-- JSON snapshot, product_price_history gives a typed/indexed timeline
-- for charts and price trend reports.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/008_product_price_history.sql
--
-- Rollback: 008_product_price_history.rollback.sql

BEGIN;

CREATE TABLE product_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    field_name  TEXT    NOT NULL CHECK(field_name IN (
                    'cost_price',
                    'base_sell_price',
                    'low_stock_threshold'
                )),
    old_value   REAL,
    new_value   REAL,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_pph_product_time
    ON product_price_history(product_id, changed_at DESC);

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

COMMIT;
