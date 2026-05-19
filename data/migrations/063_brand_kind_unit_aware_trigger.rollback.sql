-- 063_brand_kind_unit_aware_trigger.rollback.sql
-- Manual rollback (the migration runner does not auto-rollback).
--
-- Restores the pre-063 by-product_code-only trigger (verbatim from
-- 021 / recreated by 061). No table is rebuilt, so the trigger can be
-- swapped directly with no DROP TABLE / RENAME hazard.
--
-- NOTE: the one-time brand_kind backfill from 063 is NOT reversed. The
--   backfilled values are the CORRECT resolver result; "undoing" them
--   would re-introduce the corruption, so the repair is intentionally
--   left in place on rollback.

BEGIN;

DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;

CREATE TRIGGER refresh_brand_kind_on_product_brand_change
AFTER UPDATE OF brand_id ON products
WHEN OLD.brand_id IS NOT NEW.brand_id
BEGIN
    UPDATE express_sales
       SET brand_kind = (
           SELECT CASE WHEN b.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
             FROM brands b WHERE b.id = NEW.brand_id
       )
     WHERE product_code IN (
         SELECT bsn_code FROM product_code_mapping WHERE product_id = NEW.id
     );
END;

DELETE FROM applied_migrations WHERE filename = '063_brand_kind_unit_aware_trigger.sql';

COMMIT;
