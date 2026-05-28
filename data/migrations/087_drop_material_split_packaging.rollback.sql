-- ============================================================================
-- Rollback 087 — restore packaging column, drop packaging_short, restore material
--
-- Per feedback_rename_migration_safety: reads CURRENT table (the post-mig
-- products), so any rows INSERTed after mig 087 ran are preserved — their
-- packaging_th value is carried back into the restored packaging column.
--
-- Data loss: products.material is restored as a NULL column. The 620 rows
-- that had material values pre-mig cannot be recovered from SQL alone —
-- restore from sendy_erp/data/backups/material_values_pre_mig087_<ts>.csv
-- if needed.
-- ============================================================================

PRAGMA foreign_keys = OFF;
BEGIN;

-- 1) Drop new dependents
DROP VIEW IF EXISTS products_full;
DROP TRIGGER IF EXISTS products_packaging_th_check_insert;
DROP TRIGGER IF EXISTS products_packaging_th_check_update;
DROP TRIGGER IF EXISTS products_packaging_short_check_insert;
DROP TRIGGER IF EXISTS products_packaging_short_check_update;
DROP INDEX IF EXISTS idx_products_packaging_th;

-- 2) Reverse schema changes (post-mig data survives — RENAME COLUMN preserves rows)
ALTER TABLE products DROP COLUMN packaging_short;
ALTER TABLE products RENAME COLUMN packaging_th TO packaging;
ALTER TABLE products ADD COLUMN material TEXT;

-- 3) Recreate original VIEW (mig 033 shape, sans packaging_short)
CREATE VIEW products_full AS
SELECT
    p.id, p.sku, p.product_name,
    c.name_th        AS category,
    p.series,
    b.name           AS brand,
    b.short_code     AS brand_short_code,
    b.is_own_brand   AS is_own_brand,
    p.model, p.size,
    cf.name_th       AS color_th,
    p.color_code, p.packaging, p.condition, p.pack_variant,
    p.family_id, p.unit_type, p.units_per_carton, p.units_per_box,
    p.cost_price, p.base_sell_price, p.hard_to_sell, p.is_active,
    COALESCE(s.quantity, 0) AS stock,
    p.shopee_stock, p.lazada_stock,
    p.created_at, p.updated_at
FROM products p
LEFT JOIN brands b              ON b.id   = p.brand_id
LEFT JOIN categories c          ON c.id   = p.category_id
LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
LEFT JOIN stock_levels s        ON s.product_id = p.id;

-- 4) Recreate original index
CREATE INDEX idx_products_packaging ON products(packaging);

-- 5) Recreate original packaging-check triggers
CREATE TRIGGER products_packaging_check_insert
    BEFORE INSERT ON products
    WHEN NEW.packaging IS NOT NULL
         AND NEW.packaging NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

CREATE TRIGGER products_packaging_check_update
    BEFORE UPDATE ON products
    WHEN NEW.packaging IS NOT NULL
         AND NEW.packaging NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

-- 6) Remove applied_migrations record
DELETE FROM applied_migrations WHERE filename = '087_drop_material_split_packaging.sql';

COMMIT;
PRAGMA foreign_keys = ON;
