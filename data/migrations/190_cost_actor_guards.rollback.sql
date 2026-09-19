-- 190 rollback — run BEFORE reverting any code (see the forward file's header).
-- Restores audit_products_update byte-identically (mig 159's text) and drops
-- written_by. Never revert PR 1 (#621) while the forward triggers exist.

BEGIN;

DROP TRIGGER IF EXISTS products_cost_needs_actor;
DROP TRIGGER IF EXISTS audit_products_cost_update;
DROP TRIGGER IF EXISTS product_cost_ledger_insert_needs_actor;
DROP TRIGGER IF EXISTS product_cost_ledger_update_needs_actor;
DROP TRIGGER IF EXISTS product_cost_ledger_delete_needs_actor;
DROP TRIGGER IF EXISTS conversion_cost_log_insert_needs_actor;
DROP TRIGGER IF EXISTS conversion_cost_log_written_by_is_stamped;
DROP TRIGGER IF EXISTS conversion_cost_log_stamp;
DROP TRIGGER IF EXISTS conversion_cost_log_update_needs_actor;
DROP TRIGGER IF EXISTS conversion_cost_log_written_by_is_final;
DROP TRIGGER IF EXISTS conversion_cost_log_delete_needs_actor;

DROP TRIGGER IF EXISTS audit_products_update;
CREATE TRIGGER audit_products_update
AFTER UPDATE ON products
WHEN (
       OLD.product_name        IS NOT NEW.product_name
    OR OLD.unit_type           IS NOT NEW.unit_type
    OR OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.units_per_carton    IS NOT NEW.units_per_carton
    OR OLD.units_per_box       IS NOT NEW.units_per_box
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    OR OLD.hard_to_sell        IS NOT NEW.hard_to_sell
    OR OLD.is_active           IS NOT NEW.is_active
    OR OLD.weight_kg           IS NOT NEW.weight_kg
    OR OLD.weight_source       IS NOT NEW.weight_source
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_name'        AS field, OLD.product_name        AS old_v, NEW.product_name        AS new_v WHERE OLD.product_name        IS NOT NEW.product_name
        UNION ALL SELECT 'unit_type',           OLD.unit_type,           NEW.unit_type           WHERE OLD.unit_type           IS NOT NEW.unit_type
        UNION ALL SELECT 'cost_price',          OLD.cost_price,          NEW.cost_price          WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',     OLD.base_sell_price,     NEW.base_sell_price     WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'units_per_carton',    OLD.units_per_carton,    NEW.units_per_carton    WHERE OLD.units_per_carton    IS NOT NEW.units_per_carton
        UNION ALL SELECT 'units_per_box',       OLD.units_per_box,       NEW.units_per_box       WHERE OLD.units_per_box       IS NOT NEW.units_per_box
        UNION ALL SELECT 'low_stock_threshold', OLD.low_stock_threshold, NEW.low_stock_threshold WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
        UNION ALL SELECT 'hard_to_sell',        OLD.hard_to_sell,        NEW.hard_to_sell        WHERE OLD.hard_to_sell        IS NOT NEW.hard_to_sell
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active           IS NOT NEW.is_active
        UNION ALL SELECT 'weight_kg',           OLD.weight_kg,           NEW.weight_kg           WHERE OLD.weight_kg           IS NOT NEW.weight_kg
        UNION ALL SELECT 'weight_source',       OLD.weight_source,       NEW.weight_source       WHERE OLD.weight_source       IS NOT NEW.weight_source
    );
END;

ALTER TABLE conversion_cost_log DROP COLUMN written_by;

COMMIT;
