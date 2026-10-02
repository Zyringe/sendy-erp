-- 199 — promotions.min_qty + min_qty_unit (#673): an ENFORCED minimum
-- quantity on a price promo. Put, 2026-10-02 (option A): an explicit number +
-- หน่วย stored on the promo, never derived from units_per_carton/units_per_box.
--
-- The gate lives in models/promotions.py (promo_gate / promo_price); this
-- migration only gives it somewhere to read from. Both columns NULL = no
-- minimum = exactly today's behaviour for every existing row (all NULL here).
--
-- CHECK (on min_qty_unit, so the rollback can drop it first): both NULL, or a
-- real positive number + a non-blank unit on a row that occupies the PRICE slot
-- and carries no bundle_buy / gift_desc (one threshold per row: a buy-N-get-M
-- row already has its own). The price-slot text is promo_slot_sql('')[0]
-- rendered verbatim; tests/test_mig199_promo_min_qty.py pins that, as mig 177's
-- test does for its triggers.
--
-- Audit triggers (176 bodies + the two columns) are recreated drop-first so a
-- change of either column alone is logged. database.py's DDL stays without
-- the columns (the convention 176 set for `source`); data/schema.sql carries
-- them via scripts/dump_schema.py.
--
-- Re-runnability: the triggers are drop-first; a second run dies at
-- `duplicate column name` (no ADD COLUMN IF NOT EXISTS), as 176 does.
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE promotions ADD COLUMN min_qty REAL;
ALTER TABLE promotions ADD COLUMN min_qty_unit TEXT CHECK (
  (min_qty IS NULL AND min_qty_unit IS NULL)
  OR (typeof(min_qty) IN ('integer','real') AND min_qty > 0
      AND min_qty_unit IS NOT NULL AND trim(min_qty_unit) <> ''
      AND (promo_type IN ('percent','fixed') OR (promo_type = 'mixed' AND discount_value IS NOT NULL))
      AND bundle_buy IS NULL AND gift_desc IS NULL)
);

DROP TRIGGER IF EXISTS audit_promotions_delete;
CREATE TRIGGER audit_promotions_delete
BEFORE DELETE ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', OLD.id, 'DELETE',
        json_object(
            'product_id',        OLD.product_id,
            'promo_name',        OLD.promo_name,
            'promo_type',        OLD.promo_type,
            'discount_value',    OLD.discount_value,
            'bundle_buy',        OLD.bundle_buy,
            'bundle_free',       OLD.bundle_free,
            'bundle_unit',       OLD.bundle_unit,
            'bundle_condition',  OLD.bundle_condition,
            'bundle_tiers_json', OLD.bundle_tiers_json,
            'gift_desc',         OLD.gift_desc,
            'gift_qty',          OLD.gift_qty,
            'source',            OLD.source,
            'min_qty',           OLD.min_qty,
            'min_qty_unit',      OLD.min_qty_unit,
            'is_active',         OLD.is_active
        )
    );
END;

DROP TRIGGER IF EXISTS audit_promotions_insert;
CREATE TRIGGER audit_promotions_insert
AFTER INSERT ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', NEW.id, 'INSERT',
        json_object(
            'product_id',        NEW.product_id,
            'promo_name',        NEW.promo_name,
            'promo_type',        NEW.promo_type,
            'discount_value',    NEW.discount_value,
            'bundle_buy',        NEW.bundle_buy,
            'bundle_free',       NEW.bundle_free,
            'bundle_unit',       NEW.bundle_unit,
            'bundle_condition',  NEW.bundle_condition,
            'bundle_tiers_json', NEW.bundle_tiers_json,
            'gift_desc',         NEW.gift_desc,
            'gift_qty',          NEW.gift_qty,
            'source',            NEW.source,
            'min_qty',           NEW.min_qty,
            'min_qty_unit',      NEW.min_qty_unit,
            'date_start',        NEW.date_start,
            'date_end',          NEW.date_end,
            'is_active',         NEW.is_active
        )
    );
END;

DROP TRIGGER IF EXISTS audit_promotions_update;
CREATE TRIGGER audit_promotions_update
AFTER UPDATE ON promotions
WHEN (
       OLD.product_id        IS NOT NEW.product_id
    OR OLD.promo_name        IS NOT NEW.promo_name
    OR OLD.promo_type        IS NOT NEW.promo_type
    OR OLD.discount_value    IS NOT NEW.discount_value
    OR OLD.bundle_buy        IS NOT NEW.bundle_buy
    OR OLD.bundle_free       IS NOT NEW.bundle_free
    OR OLD.bundle_unit       IS NOT NEW.bundle_unit
    OR OLD.bundle_condition  IS NOT NEW.bundle_condition
    OR OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
    OR OLD.gift_desc         IS NOT NEW.gift_desc
    OR OLD.gift_qty          IS NOT NEW.gift_qty
    OR OLD.source            IS NOT NEW.source
    OR OLD.min_qty           IS NOT NEW.min_qty
    OR OLD.min_qty_unit      IS NOT NEW.min_qty_unit
    OR OLD.date_start        IS NOT NEW.date_start
    OR OLD.date_end          IS NOT NEW.date_end
    OR OLD.is_active         IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'promotions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'product_id'        AS field, OLD.product_id        AS old_v, NEW.product_id        AS new_v WHERE OLD.product_id        IS NOT NEW.product_id
        UNION ALL SELECT 'promo_name',                OLD.promo_name,                NEW.promo_name                WHERE OLD.promo_name        IS NOT NEW.promo_name
        UNION ALL SELECT 'promo_type',                OLD.promo_type,                NEW.promo_type                WHERE OLD.promo_type        IS NOT NEW.promo_type
        UNION ALL SELECT 'discount_value',            OLD.discount_value,            NEW.discount_value            WHERE OLD.discount_value    IS NOT NEW.discount_value
        UNION ALL SELECT 'bundle_buy',                OLD.bundle_buy,                NEW.bundle_buy                WHERE OLD.bundle_buy        IS NOT NEW.bundle_buy
        UNION ALL SELECT 'bundle_free',               OLD.bundle_free,               NEW.bundle_free               WHERE OLD.bundle_free       IS NOT NEW.bundle_free
        UNION ALL SELECT 'bundle_unit',               OLD.bundle_unit,               NEW.bundle_unit               WHERE OLD.bundle_unit       IS NOT NEW.bundle_unit
        UNION ALL SELECT 'bundle_condition',          OLD.bundle_condition,          NEW.bundle_condition          WHERE OLD.bundle_condition  IS NOT NEW.bundle_condition
        UNION ALL SELECT 'bundle_tiers_json',         OLD.bundle_tiers_json,         NEW.bundle_tiers_json         WHERE OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
        UNION ALL SELECT 'gift_desc',                 OLD.gift_desc,                 NEW.gift_desc                 WHERE OLD.gift_desc         IS NOT NEW.gift_desc
        UNION ALL SELECT 'gift_qty',                  OLD.gift_qty,                  NEW.gift_qty                  WHERE OLD.gift_qty          IS NOT NEW.gift_qty
        UNION ALL SELECT 'source',                    OLD.source,                    NEW.source                    WHERE OLD.source            IS NOT NEW.source
        UNION ALL SELECT 'min_qty',                   OLD.min_qty,                   NEW.min_qty                   WHERE OLD.min_qty           IS NOT NEW.min_qty
        UNION ALL SELECT 'min_qty_unit',              OLD.min_qty_unit,              NEW.min_qty_unit              WHERE OLD.min_qty_unit      IS NOT NEW.min_qty_unit
        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start
        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end
        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active
    );
END;

COMMIT;
