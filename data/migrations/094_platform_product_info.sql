-- Migration 094 — platform product-level info for the "Full" Shopee/Lazada
-- product-export import.
--
-- The Seller Center *product* export carries product-grain fields (name,
-- description, category, brand, gallery images, warranty, ...) that the
-- existing `platform_skus` table (variation grain) has no home for. This adds:
--
--   1. NEW table `platform_products` (one row per marketplace product/listing,
--      keyed by (platform, product_id_str)). Holds the product-grain columns.
--      NO internal_product_id here on purpose — the ERP mapping stays at the
--      VARIATION grain on `platform_skus.internal_product_id`.
--
--   2. EXTEND `platform_skus` with 8 variation-grain columns the product export
--      provides per-SKU (dimensions/weight for shipping, GTIN, special-price
--      window, variation image). All nullable, no default.
--
-- Purely ADDITIVE — no data transform, no row writes, nothing existing dropped
-- or rewritten. Column names are FIXED: a parallel importer + spec depend on
-- these exact names.
--
-- Apply:    sqlite3 .../inventory.db < .../094_platform_product_info.sql
--           (or auto-applied by database.py::run_pending_migrations on boot)
-- Rollback: 094_platform_product_info.rollback.sql

BEGIN;

-- 1. Product-grain table.
CREATE TABLE IF NOT EXISTS platform_products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    platform          TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    product_id_str    TEXT    NOT NULL,
    parent_sku        TEXT,
    product_name      TEXT,
    name_en           TEXT,
    description       TEXT,
    category_id_str   TEXT,
    category_name     TEXT,
    brand             TEXT,
    place_of_origin   TEXT,
    material          TEXT,
    warranty_policy   TEXT,
    warranty_period   TEXT,
    status            TEXT,
    cover_image_url   TEXT,
    image_urls        TEXT,    -- JSON array of gallery image URLs
    dts_info          TEXT,
    raw_json          TEXT,
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, product_id_str)
);

-- The UNIQUE(platform, product_id_str) already creates an implicit index that
-- covers the (platform, product_id_str) lookup AND the (platform, ...) prefix,
-- so a separate index on those columns would be redundant. Add only the
-- forward-looking join helper for resolving a marketplace product back to a
-- parent_sku (the column the future products↔listing join will hang off).
CREATE INDEX IF NOT EXISTS idx_platform_products_parent_sku
    ON platform_products(platform, parent_sku);

-- 2. Variation-grain columns on the existing platform_skus table.
-- Plain ALTERs (no IF NOT EXISTS — SQLite has none for ADD COLUMN): the runner
-- is filename-keyed and never re-runs an applied migration, so a one-shot ALTER
-- is the established pattern here (see mig 017).
ALTER TABLE platform_skus ADD COLUMN weight_kg REAL;
ALTER TABLE platform_skus ADD COLUMN length_cm REAL;
ALTER TABLE platform_skus ADD COLUMN width_cm REAL;
ALTER TABLE platform_skus ADD COLUMN height_cm REAL;
ALTER TABLE platform_skus ADD COLUMN gtin TEXT;
ALTER TABLE platform_skus ADD COLUMN special_price_start TEXT;
ALTER TABLE platform_skus ADD COLUMN special_price_end TEXT;
ALTER TABLE platform_skus ADD COLUMN variation_image_url TEXT;

COMMIT;
