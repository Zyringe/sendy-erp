-- ============================================================================
-- Migration 087 — drop products.material, rename packaging→packaging_th, add packaging_short
--
-- Why
--   The locked 24-rule SKU naming doc has `material` as slot 8, but it was
--   never wired into build_sku_code (sku_code_utils.py:53) — the generator
--   skips it. Meanwhile `packaging` lives as a single Thai column whose
--   short-code (UN, PN, ...) is derived on every generate via the
--   PACKAGING_SHORT dict (sku_code_utils.py:16-28). Materializing
--   packaging_short as a column lets us read it directly + override per-product
--   when needed. Dropping material removes a slot the rule docs claim exists
--   but the system never honored.
--
--   Round-1 product-name normalization (`scripts/normalize_products_round1.py`)
--   depends on this shape: re-parse 1,962 names with material gone from the
--   rule and packaging stored as (Thai, short-code) pair.
--
-- What
--   1. DROP products.material — verified no triggers / views / indexes /
--      foreign keys reference it (grep clean as of 2026-05-28). The 620
--      rows with material populated are exported to a backup CSV BEFORE
--      this mig runs (see scripts/backup_material_pre_mig087.py) since
--      rollback cannot recover the data.
--   2. RENAME products.packaging → products.packaging_th. SQLite 3.51 supports
--      `ALTER TABLE ... RENAME COLUMN` and auto-rewrites references inside
--      indexes / triggers / views. We still drop+recreate the VIEW (needs new
--      packaging_short column added) and the index (to rename it).
--   3. ADD products.packaging_short TEXT.
--   4. Backfill packaging_short from the 11-value mapping in
--      sku_code_utils.PACKAGING_SHORT — the only Thai values currently in DB
--      (verified 2026-05-28: 208 แผง / 52 ตัว / 15 ถุง / 4 แพ็คถุง /
--      3 ซอง,อัดแผง,แพ็ค,แพ็คหัว / 1 1กลมี60ใบ,แบบหลอด,โหล — exhaustive).
--   5. Drop OLD packaging-check triggers (named products_packaging_check_*),
--      replace with two pairs of new triggers:
--        products_packaging_th_check_{insert,update}    (11-value Thai whitelist)
--        products_packaging_short_check_{insert,update} (11-value short whitelist)
--   6. Drop+recreate products_full VIEW exposing packaging_th + packaging_short
--      (replaces packaging in SELECT list).
--   7. Rename idx_products_packaging → idx_products_packaging_th.
--
-- How
--   Uses SQLite 3.35+ ALTER TABLE DROP/RENAME COLUMN (not table-rebuild) —
--   simpler, lower risk of trigger/view drift. material has no schema
--   references so DROP is safe; packaging RENAME auto-rewrites the VIEW and
--   trigger bodies, but we drop+recreate them explicitly for cleanliness +
--   to add packaging_short coverage.
--
-- Pre-mig safety (operator MUST run before deploying)
--   python sendy_erp/scripts/backup_material_pre_mig087.py
--     → writes sendy_erp/data/backups/material_values_pre_mig087_<ts>.csv
--   The runner auto-applies this mig on first sendy-up after the SQL lands,
--   so the backup must happen *before* the SQL file is deployed (or run via
--   railway ssh before pushing).
--
-- FK hazard: same recipe as mig 086 — PRAGMA foreign_keys = OFF before BEGIN
-- so the column drop/rename doesn't trip references from other tables.
-- ============================================================================

PRAGMA foreign_keys = OFF;
BEGIN;

-- 1) Drop dependents that reference packaging or that need full rewrite
DROP VIEW IF EXISTS products_full;
DROP TRIGGER IF EXISTS products_packaging_check_insert;
DROP TRIGGER IF EXISTS products_packaging_check_update;
DROP INDEX IF EXISTS idx_products_packaging;

-- 2) Schema changes
ALTER TABLE products DROP COLUMN material;
ALTER TABLE products RENAME COLUMN packaging TO packaging_th;
ALTER TABLE products ADD COLUMN packaging_short TEXT;

-- 3) Backfill packaging_short from the locked PACKAGING_SHORT mapping
UPDATE products SET packaging_short = CASE packaging_th
    WHEN 'ตัว'        THEN 'UN'
    WHEN 'แผง'        THEN 'PN'
    WHEN 'ถุง'        THEN 'BG'
    WHEN 'ซอง'        THEN 'SC'
    WHEN 'แพ็ค'       THEN 'PK'
    WHEN 'โหล'        THEN 'DZ'
    WHEN 'แพ็คหัว'    THEN 'HP'
    WHEN 'แพ็คถุง'    THEN 'PP'
    WHEN 'แบบหลอด'   THEN 'TB'
    WHEN 'อัดแผง'    THEN 'SP'
    WHEN '1กลมี60ใบ' THEN 'C60'
    ELSE NULL
END
WHERE packaging_th IS NOT NULL;

-- 4) Recreate products_full VIEW with packaging_th + packaging_short
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
    p.color_code, p.packaging_th, p.packaging_short, p.condition, p.pack_variant,
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

-- 5) Recreate index with new name
CREATE INDEX idx_products_packaging_th ON products(packaging_th);

-- 6) packaging_th whitelist triggers (11 approved Thai values)
CREATE TRIGGER products_packaging_th_check_insert
    BEFORE INSERT ON products
    WHEN NEW.packaging_th IS NOT NULL
         AND NEW.packaging_th NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_th must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

CREATE TRIGGER products_packaging_th_check_update
    BEFORE UPDATE ON products
    WHEN NEW.packaging_th IS NOT NULL
         AND NEW.packaging_th NOT IN (
             'แผง', 'ตัว', 'ถุง', 'แพ็คหัว', 'แพ็คถุง',
             'ซอง', 'อัดแผง', 'แพ็ค', 'แบบหลอด', 'โหล', '1กลมี60ใบ'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_th must be NULL or one of: แผง, ตัว, ถุง, แพ็คหัว, แพ็คถุง, ซอง, อัดแผง, แพ็ค, แบบหลอด, โหล, 1กลมี60ใบ');
    END;

-- 7) packaging_short whitelist triggers (11 short codes)
CREATE TRIGGER products_packaging_short_check_insert
    BEFORE INSERT ON products
    WHEN NEW.packaging_short IS NOT NULL
         AND NEW.packaging_short NOT IN (
             'UN', 'PN', 'BG', 'SC', 'PK', 'DZ', 'HP', 'PP', 'TB', 'SP', 'C60'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_short must be NULL or one of: UN, PN, BG, SC, PK, DZ, HP, PP, TB, SP, C60');
    END;

CREATE TRIGGER products_packaging_short_check_update
    BEFORE UPDATE ON products
    WHEN NEW.packaging_short IS NOT NULL
         AND NEW.packaging_short NOT IN (
             'UN', 'PN', 'BG', 'SC', 'PK', 'DZ', 'HP', 'PP', 'TB', 'SP', 'C60'
         )
    BEGIN
        SELECT RAISE(ABORT,
            'packaging_short must be NULL or one of: UN, PN, BG, SC, PK, DZ, HP, PP, TB, SP, C60');
    END;

-- 8) Record
INSERT INTO applied_migrations(filename, applied_at)
VALUES ('087_drop_material_split_packaging.sql', datetime('now','localtime'));

COMMIT;
PRAGMA foreign_keys = ON;
