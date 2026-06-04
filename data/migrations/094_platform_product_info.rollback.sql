-- Rollback 094 — reverse the platform product-level info migration.
--
-- Forward mig was purely additive (1 new table + 8 nullable columns), so a
-- clean drop fully reverts it with no data loss beyond what 094 itself added.
--
-- platform_skus column removal uses ALTER TABLE DROP COLUMN. SQLite supports
-- DROP COLUMN since 3.35.0 (this environment runs 3.51.0), and the existing
-- rollback convention already relies on it (see mig 017's rollback), so a
-- table-rebuild is unnecessary. None of the 8 dropped columns are referenced by
-- an index, generated column, view, or trigger, so DROP COLUMN is unconditional.

BEGIN;

DROP INDEX IF EXISTS idx_platform_products_parent_sku;
DROP TABLE IF EXISTS platform_products;

ALTER TABLE platform_skus DROP COLUMN variation_image_url;
ALTER TABLE platform_skus DROP COLUMN special_price_end;
ALTER TABLE platform_skus DROP COLUMN special_price_start;
ALTER TABLE platform_skus DROP COLUMN gtin;
ALTER TABLE platform_skus DROP COLUMN height_cm;
ALTER TABLE platform_skus DROP COLUMN width_cm;
ALTER TABLE platform_skus DROP COLUMN length_cm;
ALTER TABLE platform_skus DROP COLUMN weight_kg;

DELETE FROM applied_migrations WHERE filename = '094_platform_product_info.sql';

COMMIT;
