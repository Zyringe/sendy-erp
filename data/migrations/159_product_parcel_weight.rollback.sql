-- Rollback for 159_product_parcel_weight.sql.
--
-- Restores products to its pre-159 shape (drops both weight columns) and puts
-- `audit_products_update` back to its pre-159 body, byte-identical.
--
-- ⚠ DROP ORDER IS LOAD-BEARING. `weight_source`'s CHECK references
-- `weight_kg`, so dropping `weight_kg` first fails with
--   "error in table products after drop column: no such column: weight_kg"
-- and leaves the transaction to roll back. Verified both orders empirically on
-- sqlite_version 3.51.0 before this file was written. weight_source first.
--
-- ⚠ The TRIGGER is dropped BEFORE the columns, for the same class of reason:
-- the 159 body references OLD.weight_kg / NEW.weight_kg, and a trigger left in
-- place would reference columns that no longer exist.
--
-- Rows inserted after the forward migration survive: `ALTER TABLE ... DROP
-- COLUMN` operates on the CURRENT table in place, so this rollback needs no
-- snapshot (verified — a row inserted post-forward with data in every column
-- came through both drops intact). Only the two dropped columns' values are
-- lost, which is the point: they did not exist before 159. If any real weights
-- have already been captured, export them before running this.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS audit_products_update;

ALTER TABLE products DROP COLUMN weight_source;
ALTER TABLE products DROP COLUMN weight_kg;

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
    );
END;

DELETE FROM applied_migrations WHERE filename='159_product_parcel_weight.sql';

COMMIT;
