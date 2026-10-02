.bail on
-- Rollback 199. Run it with the sqlite3 CLI: sqlite3 "$DB" < this file.
--
-- `.bail on` (line 1) is load-bearing: without it the CLI reports the
-- precondition's RAISE(ABORT) and then keeps executing the next statements,
-- so the DROP COLUMNs below ran and ungated every live minimum anyway
-- (/interrogate on the PR 1 diff, 2026-10-02). The migration runner never
-- executes rollback files; it is a dot-command, so do not feed this file to
-- Python's executescript().
--
-- Precondition ABORTS when any is_active = 1 promo carries a minimum: dropping
-- the column would silently UNGATE it (its discount would apply to one piece).
-- Close or clear those rows first:
--     SELECT id, product_id, min_qty, min_qty_unit FROM promotions
--      WHERE is_active = 1 AND min_qty IS NOT NULL;
-- Inactive rows lose their minimum here; audit_log keeps it.
--
-- Order: precondition → drop the triggers (they reference the columns) →
-- restore the 176 bodies byte-identical (copied from data/schema.sql at
-- origin/main bc4183f, not retyped; tested against a literal) → drop
-- min_qty_unit (its CHECK names min_qty) → drop min_qty.
PRAGMA busy_timeout = 10000;

BEGIN;

DROP TABLE IF EXISTS temp._mig199_rb_precheck;
CREATE TEMP TABLE _mig199_rb_precheck AS
SELECT id FROM promotions WHERE is_active = 1 AND min_qty IS NOT NULL;

CREATE TEMP TRIGGER _mig199_rb_guard
BEFORE DELETE ON _mig199_rb_precheck
BEGIN
  SELECT RAISE(ABORT, '199 rollback precondition FAILED: an active promo carries a minimum quantity. See the query in this file header.');
END;

DELETE FROM _mig199_rb_precheck;
DROP TRIGGER _mig199_rb_guard;
DROP TABLE _mig199_rb_precheck;

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
            'source',            OLD.source,
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
            'source',            NEW.source,
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
    OR OLD.source            IS NOT NEW.source
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
        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start
        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end
        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active
    );
END;

ALTER TABLE promotions DROP COLUMN min_qty_unit;
ALTER TABLE promotions DROP COLUMN min_qty;

COMMIT;
