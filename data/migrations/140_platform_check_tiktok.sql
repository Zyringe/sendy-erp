-- ============================================================================
-- Migration 140 — unlock 'tiktok' in the platform CHECK constraints
--
-- Why
--   TikTok Shop is launching (projects/ecommerce-revamp). The revamped
--   /ecommerce page needs to accept 'tiktok' as a first-class platform value
--   alongside 'shopee'/'lazada' on the 3 marketplace-listing tables. Data
--   import (API/manual keying) is a LATER phase — this migration only widens
--   the schema so those rows are not rejected once that lands.
--
--   marketplace_orders (sales-booking, a separate arc — see
--   projects/ecommerce-revamp/plan.md "Out of scope") is deliberately NOT
--   touched here.
--
-- How
--   SQLite can't ALTER a CHECK constraint in place → table rebuild for each
--   of the 3 tables (mirrors migration 097's recipe):
--     1) CREATE <table>_new with CHECK(platform IN ('shopee','lazada','tiktok')).
--     2) INSERT..SELECT with an EXPLICIT column list (never *) — preserves id,
--        so the listing_bundles.listing_id FK into ecommerce_listings keeps
--        resolving after the swap.
--     3) DROP the old table (also drops its indexes/triggers), RENAME _new.
--     4) Recreate the indexes/trigger that were dropped with the old table.
--
-- FK hazard: `listing_bundles.listing_id REFERENCES ecommerce_listings(id)`
-- is the only FK dependent on any of these 3 tables. PRAGMA foreign_keys=OFF
-- BEFORE BEGIN (it's a no-op inside a txn) lets DROP TABLE proceed with that
-- dependent still pointing at the old name; the RENAME back to the original
-- name makes it resolve again once the transaction commits.
--
-- Rollback: 140_platform_check_tiktok.rollback.sql — reverses to the 2-value
-- CHECK. INSERT..SELECT reads from the CURRENT table (not a snapshot) so any
-- row added after this migration survives the round trip — EXCEPT a row with
-- platform='tiktok', which will fail the narrower CHECK. That's a forensic-
-- only rollback path once real tiktok data exists (see plan.md); acceptable.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- ── platform_skus ───────────────────────────────────────────────────────────
CREATE TABLE platform_skus_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform             TEXT    NOT NULL CHECK(platform IN ('shopee','lazada','tiktok')),
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

-- Recreate the price-history trigger (mig 137) — auto-dropped with the old table.
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
    platform          TEXT    NOT NULL CHECK(platform IN ('shopee','lazada','tiktok')),
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
-- listing_bundles.listing_id REFERENCES ecommerce_listings(id) — the only FK
-- dependent among the 3 rebuilt tables (foreign_keys=OFF above covers it).
CREATE TABLE ecommerce_listings_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT    NOT NULL CHECK(platform IN ('shopee','lazada','tiktok')),
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
