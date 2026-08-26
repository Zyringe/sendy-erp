-- Rollback 176.
--
-- Order matters: drop the three audit triggers 176 installed (they
-- reference NEW.source / OLD.source, which is about to stop existing) →
-- recreate the ORIGINAL trigger bodies (byte-identical to
-- origin/main:data/schema.sql, copied not retyped) → un-stamp date_start
-- ONLY on the rows THIS migration stamped (source='catalog-import' AND
-- date_start='2026-06-01' — a date an operator set afterwards no longer
-- matches that pair and survives) → DROP COLUMN source last, since the
-- un-stamp UPDATE above still needs to read it.
PRAGMA busy_timeout = 10000;

BEGIN;

DROP TRIGGER IF EXISTS audit_promotions_delete;
DROP TRIGGER IF EXISTS audit_promotions_insert;
DROP TRIGGER IF EXISTS audit_promotions_update;

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
            'is_active',         OLD.is_active
        )
    );
END;

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
            'date_start',        NEW.date_start,
            'date_end',          NEW.date_end,
            'is_active',         NEW.is_active
        )
    );
END;

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
        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start
        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end
        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active
    );
END;

UPDATE promotions
   SET date_start = NULL
 WHERE source = 'catalog-import'
   AND date_start = '2026-06-01';

ALTER TABLE promotions DROP COLUMN source;

COMMIT;
