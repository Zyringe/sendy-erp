-- ============================================================================
-- Rollback for migration 140 — restore CHECK(platform IN ('shopee','lazada'))
-- on platform_skus, platform_products, ecommerce_listings.
--
-- Forensic path only: INSERT..SELECT reads from the CURRENT table, so it will
-- FAIL LOUDLY (CHECK constraint violation) if any row has platform='tiktok'
-- by the time this runs — accepted per plan.md Phase 1 (do not "fix" this by
-- deleting tiktok rows silently; that decision belongs to whoever runs the
-- rollback).
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- ── platform_skus ───────────────────────────────────────────────────────────
CREATE TABLE platform_skus_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform             TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    product_id_str       TEXT,
    product_name         TEXT    NOT NULL,
    variation_id         TEXT,
    variation_name       TEXT,
    parent_sku           TEXT,
    seller_sku           TEXT,
    price                REAL,
    special_price        REAL,
    stock                INTEGER,
    internal_product_id  INTEGER REFERENCES products(id),
    qty_per_sale         REAL    NOT NULL DEFAULT 1,
    raw_json             TEXT,
    imported_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    weight_kg            REAL,
    length_cm            REAL,
    width_cm             REAL,
    height_cm            REAL,
    gtin                 TEXT,
    special_price_start  TEXT,
    special_price_end    TEXT,
    variation_image_url  TEXT,
    is_ignored           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, variation_id)
);

INSERT INTO platform_skus_new
    (id, platform, product_id_str, product_name, variation_id, variation_name,
     parent_sku, seller_sku, price, special_price, stock, internal_product_id,
     qty_per_sale, raw_json, imported_at, weight_kg, length_cm, width_cm,
     height_cm, gtin, special_price_start, special_price_end,
     variation_image_url, is_ignored)
SELECT
     id, platform, product_id_str, product_name, variation_id, variation_name,
     parent_sku, seller_sku, price, special_price, stock, internal_product_id,
     qty_per_sale, raw_json, imported_at, weight_kg, length_cm, width_cm,
     height_cm, gtin, special_price_start, special_price_end,
     variation_image_url, is_ignored
FROM platform_skus;

DROP TABLE platform_skus;
ALTER TABLE platform_skus_new RENAME TO platform_skus;

CREATE TRIGGER platform_skus_price_history_update
AFTER UPDATE ON platform_skus
WHEN (
       OLD.price         IS NOT NEW.price
    OR OLD.special_price IS NOT NEW.special_price
)
BEGIN
    INSERT INTO platform_price_history
        (platform, variation_id, internal_product_id, field_name, old_value, new_value, source)
    SELECT NEW.platform, NEW.variation_id, NEW.internal_product_id, field, old_v, new_v, 'platform_skus.update'
    FROM (
                  SELECT 'price'         AS field, OLD.price         AS old_v, NEW.price         AS new_v WHERE OLD.price         IS NOT NEW.price
        UNION ALL SELECT 'special_price',          OLD.special_price,          NEW.special_price          WHERE OLD.special_price IS NOT NEW.special_price
    );
END;

-- ── platform_products ───────────────────────────────────────────────────────
CREATE TABLE platform_products_new (
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
    image_urls        TEXT,
    dts_info          TEXT,
    raw_json          TEXT,
    imported_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, product_id_str)
);

INSERT INTO platform_products_new
    (id, platform, product_id_str, parent_sku, product_name, name_en,
     description, category_id_str, category_name, brand, place_of_origin,
     material, warranty_policy, warranty_period, status, cover_image_url,
     image_urls, dts_info, raw_json, imported_at)
SELECT
     id, platform, product_id_str, parent_sku, product_name, name_en,
     description, category_id_str, category_name, brand, place_of_origin,
     material, warranty_policy, warranty_period, status, cover_image_url,
     image_urls, dts_info, raw_json, imported_at
FROM platform_products;

DROP TABLE platform_products;
ALTER TABLE platform_products_new RENAME TO platform_products;

CREATE INDEX idx_platform_products_parent_sku
    ON platform_products(platform, parent_sku);

-- ── ecommerce_listings ──────────────────────────────────────────────────────
CREATE TABLE ecommerce_listings_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT    NOT NULL CHECK(platform IN ('shopee','lazada')),
    item_name    TEXT    NOT NULL,
    variation    TEXT,
    seller_sku   TEXT,
    listing_key  TEXT    NOT NULL UNIQUE,
    sample_price REAL,
    product_id   INTEGER REFERENCES products(id),
    is_ignored   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    qty_per_sale REAL    NOT NULL DEFAULT 1
);

INSERT INTO ecommerce_listings_new
    (id, platform, item_name, variation, seller_sku, listing_key,
     sample_price, product_id, is_ignored, created_at, qty_per_sale)
SELECT
     id, platform, item_name, variation, seller_sku, listing_key,
     sample_price, product_id, is_ignored, created_at, qty_per_sale
FROM ecommerce_listings;

DROP TABLE ecommerce_listings;
ALTER TABLE ecommerce_listings_new RENAME TO ecommerce_listings;

CREATE INDEX idx_el_platform ON ecommerce_listings(platform, product_id);

COMMIT;

PRAGMA foreign_keys = ON;
