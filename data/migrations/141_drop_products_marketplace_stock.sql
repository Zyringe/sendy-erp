-- ============================================================================
-- Migration 141 — drop products.shopee_stock / products.lazada_stock
--
-- Apply:   restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 141_drop_products_marketplace_stock.rollback.sql
--
-- Why
--   Phase D1 of projects/sendy-products-page-improve/plan.md (Put's call,
--   Decision #9 — "full clean of the dead columns incl. migration"). These
--   two columns were write-only from the start: the numbers shown on
--   /products and /products/<id> have always come from correlated
--   subqueries over platform_skus (aliased with the SAME names, so the
--   alias shadowed the real column everywhere it was read). The real
--   columns were:
--     - written by /products/new -> create_structured_product
--     - written by create_product() (test-only)
--     - decremented by models/bsn_sync.py on every sale
--     - read by NOTHING
--   PR #324 (P2a, 2026-07-29) removed every remaining app write path,
--   including database.py's ADD COLUMN self-heal block. This migration is a
--   pure schema operation with nothing racing it.
--
-- How (same 7-step rebuild recipe as migration 097 — SQLite cannot DROP a
-- COLUMN referenced by a VIEW/complex history in one step here since we also
-- need to touch products_full):
--   1) DROP the products_full VIEW (it references p.shopee_stock/lazada_stock).
--   2) CREATE products_new = current 31 columns MINUS the 2 (-> 29).
--   3) INSERT...SELECT with EXPLICIT column lists (never *).
--   4) DROP TABLE products (drops its 7 indexes + 9 triggers), RENAME
--      products_new -> products.
--   5) Recreate the 7 indexes.
--   6) Recreate products_full WITHOUT p.shopee_stock, p.lazada_stock.
--   7) Recreate all 9 triggers verbatim (none reference either column).
--
-- FK hazard: 29 tables carry a FK to products(id). PRAGMA foreign_keys=OFF
-- BEFORE BEGIN and back ON AFTER COMMIT (PRAGMA is a no-op inside a txn) so
-- references resolve against the new table once it exists under the old
-- name. Mirrors migrations 061 / 069 / 097.
-- ============================================================================

PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

-- 1) Drop the view so DROP TABLE doesn't leave it pointing at a vanished table.
DROP VIEW  IF EXISTS products_full;
DROP TABLE IF EXISTS products_new;

-- 2) CREATE products_new = current schema MINUS shopee_stock, lazada_stock.
CREATE TABLE products_new (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name             TEXT    NOT NULL,
    units_per_carton         INTEGER NOT NULL DEFAULT 1,
    units_per_box            INTEGER NOT NULL DEFAULT 1,
    unit_type                TEXT    NOT NULL DEFAULT 'ตัว',
    hard_to_sell             INTEGER NOT NULL DEFAULT 0,
    cost_price               REAL    NOT NULL DEFAULT 0.0,
    base_sell_price          REAL    NOT NULL DEFAULT 0.0,
    low_stock_threshold      INTEGER NOT NULL DEFAULT 10,
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    brand_id                 INTEGER REFERENCES brands(id),
    category_id              INTEGER REFERENCES categories(id),
    family_id                INTEGER REFERENCES product_families(id),
    color_code               TEXT    REFERENCES color_finish_codes(code),
    packaging_th             TEXT,
    series                   TEXT,
    model                    TEXT,
    size                     TEXT,
    condition                TEXT,
    pack_variant             TEXT,
    sub_category             TEXT,
    sku_code                 TEXT,
    sku_code_locked          INTEGER NOT NULL DEFAULT 0
                                  CHECK(sku_code_locked IN (0, 1)),
    sub_category_short_code  TEXT,
    packaging_short          TEXT,
    opening_cost             REAL    NOT NULL DEFAULT 0.0,
    created_via              TEXT
);

-- 3) Copy data (EXPLICIT column list — never SELECT *).
INSERT INTO products_new
    (id, product_name,
     units_per_carton, units_per_box,
     unit_type, hard_to_sell, cost_price, base_sell_price,
     low_stock_threshold, is_active, created_at, updated_at,
     brand_id, category_id, family_id, color_code,
     packaging_th, series, model, size, condition, pack_variant,
     sub_category, sku_code, sku_code_locked,
     sub_category_short_code, packaging_short,
     opening_cost, created_via)
SELECT
     id, product_name,
     units_per_carton, units_per_box,
     unit_type, hard_to_sell, cost_price, base_sell_price,
     low_stock_threshold, is_active, created_at, updated_at,
     brand_id, category_id, family_id, color_code,
     packaging_th, series, model, size, condition, pack_variant,
     sub_category, sku_code, sku_code_locked,
     sub_category_short_code, packaging_short,
     opening_cost, created_via
FROM products;

-- 4) Swap.
DROP TABLE products;
ALTER TABLE products_new RENAME TO products;

-- 5) Recreate the 7 indexes.
CREATE INDEX idx_products_brand        ON products(brand_id);
CREATE INDEX idx_products_category     ON products(category_id);
CREATE INDEX idx_products_family       ON products(family_id);
CREATE INDEX idx_products_color_code   ON products(color_code);
CREATE INDEX idx_products_sub_category ON products(sub_category);
CREATE UNIQUE INDEX idx_products_sku_code ON products(sku_code) WHERE sku_code IS NOT NULL;
CREATE INDEX idx_products_packaging_th ON products(packaging_th);

-- 6) Recreate products_full WITHOUT p.shopee_stock, p.lazada_stock.
CREATE VIEW products_full AS
SELECT
    p.id, p.product_name,
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
    p.created_at, p.updated_at
FROM products p
LEFT JOIN brands b              ON b.id   = p.brand_id
LEFT JOIN categories c          ON c.id   = p.category_id
LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
LEFT JOIN stock_levels s        ON s.product_id = p.id;

-- 7) Recreate the 9 triggers verbatim (live SQL as of 2026-07-29 — the
--    audit/price-history triggers gained a `source` column, and
--    product_price_history_update now stamps price_change_source, since
--    migration 097 last touched this table; neither references the two
--    dropped columns).

CREATE TRIGGER update_product_timestamp
    AFTER UPDATE ON products
    BEGIN
        UPDATE products SET updated_at = datetime('now','localtime') WHERE id = NEW.id;
    END;

CREATE TRIGGER audit_products_insert
AFTER INSERT ON products
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'products', NEW.id, 'INSERT',
        json_object(
            'product_name', NEW.product_name,
            'unit_type', NEW.unit_type,
            'cost_price', NEW.cost_price,
            'base_sell_price', NEW.base_sell_price,
            'low_stock_threshold', NEW.low_stock_threshold,
            'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_products_update
AFTER UPDATE ON products
WHEN (
       OLD.product_name        IS NOT NEW.product_name
    OR OLD.unit_type           IS NOT NEW.unit_type
    OR OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.units_per_carton    IS NOT NEW.units_per_carton
    OR OLD.units_per_box       IS NOT NEW.units_per_box
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    OR OLD.hard_to_sell        IS NOT NEW.hard_to_sell
    OR OLD.is_active           IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_name'        AS field, OLD.product_name        AS old_v, NEW.product_name        AS new_v WHERE OLD.product_name        IS NOT NEW.product_name
        UNION ALL SELECT 'unit_type',           OLD.unit_type,           NEW.unit_type           WHERE OLD.unit_type           IS NOT NEW.unit_type
        UNION ALL SELECT 'cost_price',          OLD.cost_price,          NEW.cost_price          WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',     OLD.base_sell_price,     NEW.base_sell_price     WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'units_per_carton',    OLD.units_per_carton,    NEW.units_per_carton    WHERE OLD.units_per_carton    IS NOT NEW.units_per_carton
        UNION ALL SELECT 'units_per_box',       OLD.units_per_box,       NEW.units_per_box       WHERE OLD.units_per_box       IS NOT NEW.units_per_box
        UNION ALL SELECT 'low_stock_threshold', OLD.low_stock_threshold, NEW.low_stock_threshold WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
        UNION ALL SELECT 'hard_to_sell',        OLD.hard_to_sell,        NEW.hard_to_sell        WHERE OLD.hard_to_sell        IS NOT NEW.hard_to_sell
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active           IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_products_delete
BEFORE DELETE ON products
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'products', OLD.id, 'DELETE',
        json_object(
            'product_name', OLD.product_name,
            'unit_type', OLD.unit_type,
            'cost_price', OLD.cost_price,
            'base_sell_price', OLD.base_sell_price,
            'is_active', OLD.is_active
        )
    );
END;

CREATE TRIGGER product_price_history_update
AFTER UPDATE ON products
WHEN (
       OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
)
BEGIN
    INSERT INTO product_price_history (product_id, field_name, old_value, new_value, source)
    SELECT NEW.id, field, old_v, new_v,
           (SELECT source FROM price_change_source WHERE id = 1)
    FROM (
                  SELECT 'cost_price'          AS field, OLD.cost_price          AS old_v, NEW.cost_price          AS new_v WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',             OLD.base_sell_price,             NEW.base_sell_price             WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'low_stock_threshold',         OLD.low_stock_threshold,         NEW.low_stock_threshold         WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    );
END;

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

COMMIT;

PRAGMA foreign_keys = ON;
