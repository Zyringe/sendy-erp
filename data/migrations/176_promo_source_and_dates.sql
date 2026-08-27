-- 176 — promo provenance: mark the 2026-06-01 catalog batch as
-- source='catalog-import' and stamp its date_start, so later phases (a price
-- resolver, mig 177's promo-stacking trigger, the quote skill) can tell a
-- bulk-imported promo from one Put created by hand at
-- /products/<id>/promotions/new.
--
-- promotions has NO source column today and 0 rows carry any date (verified
-- 2026-08-26: 566 active promos, ALL matching `promo_name LIKE 'catalog
-- 2026-06-01%'`; percent 425 / fixed 65 / bundle 48 / mixed 28; 2 products
-- (pid 445, pid 1811) carry 2 active promos each and are stamped the same as
-- everything else — no special case needed, the WHERE clause already covers
-- both rows for each). Any OTHER promo (a hand-made one via the products
-- route, or a future catalog batch with a different promo_name) is left
-- source IS NULL, which the CHECK allows.
--
-- date_end and is_active are NOT touched by this migration.
--
-- Audit triggers (data/schema.sql:3672-3757) build their JSON with an
-- explicit column list, so they are recreated FIRST (drop-first), before the
-- stamping UPDATE below, so that UPDATE's own audit_log row also carries the
-- new `source` value rather than being written by the old (source-blind)
-- trigger body.
--
-- KNOWN SILENT CHANGE: review_rules.py::_get_active_promo_on_date matches
-- promos against a date window. Stamping date_start='2026-06-01' means a
-- bill dated BEFORE 2026-06-01 stops matching these promos in ตรวจบิล.
-- Expected no-op for weekly batches (all post-June); written down here so
-- nobody hunts it later on a historical re-review.
--
-- Re-runnability: everything below the ALTER TABLE is DROP-first / idempotent,
-- but a second run of this file dies at `duplicate column name: source`
-- (SQLite has no ADD COLUMN IF NOT EXISTS) — the runner never repeats an
-- applied migration, so this only matters for a hand rehearsal, same as 173.
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE promotions ADD COLUMN source TEXT
  CHECK (source IS NULL OR source IN ('catalog-import','manual'));

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

UPDATE promotions
   SET source = 'catalog-import',
       date_start = COALESCE(date_start, '2026-06-01')
 WHERE promo_name LIKE 'catalog 2026-06-01%';

COMMIT;
