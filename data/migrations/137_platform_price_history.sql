-- 137_platform_price_history.sql
-- Typed timeline of MARKETPLACE (Shopee/Lazada) price changes — the marketplace
-- analog of 008_product_price_history (which tracks INTERNAL cost/base prices).
--
-- Watches platform_skus.price and platform_skus.special_price. Fed automatically
-- by the import upsert (models/platform_skus.py::import_platform_skus,
-- ON CONFLICT(platform,variation_id) DO UPDATE — NO DELETE) and by the in-app
-- update_platform_sku path. The AFTER UPDATE trigger sees OLD->NEW and records one
-- row per changed field. Stock-only / mapping-only updates (internal_product_id,
-- qty_per_sale, stock via bsn_sync) do NOT fire it — the WHEN gate is on the price
-- fields only, and IS NOT handles NULL special_price.
--
-- NATURE OF THE DATA (read before trusting it): this is an IMPORT-DIFF log, not an
-- event log. changed_at is the moment platform_skus was written (= import time),
-- not necessarily when the price changed on the platform. A price edited directly
-- in Seller Center is captured only once a fresh export is re-imported into Sendy.
-- TikTok is out of scope: it is not in platform_skus (CHECK platform IN shopee/lazada).
--
-- Trigger pattern matches 008_product_price_history / 003_audit_triggers:
--   AFTER UPDATE ... WHEN (field IS NOT field) ... INSERT ... SELECT ... UNION ALL
--   with per-row WHERE so only the fields that actually changed produce a row.
--
-- Rollback: 137_platform_price_history.rollback.sql

BEGIN;

CREATE TABLE platform_price_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT    NOT NULL,
    variation_id        TEXT,
    internal_product_id INTEGER REFERENCES products(id),
    field_name          TEXT    NOT NULL CHECK(field_name IN ('price','special_price')),
    old_value           REAL,
    new_value           REAL,
    changed_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    source              TEXT
);

CREATE INDEX idx_plat_price_hist_product
    ON platform_price_history(internal_product_id, changed_at DESC);

CREATE INDEX idx_plat_price_hist_variation
    ON platform_price_history(platform, variation_id, changed_at DESC);

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

COMMIT;
