-- ============================================================================
-- Rollback for 069 — restore products.units_per_carton / units_per_box as
-- NULL-able columns (no DEFAULT). Mirror of the up migration with the two
-- target columns reverted to their pre-069 shape.
--
-- Does NOT null-out values that may have been backfilled to 1 by the forward
-- migration — once "1" is in the table we have no way to know which of those
-- rows were originally NULL vs originally 1. If true revert is needed, that's
-- a manual data step.
--
-- Deletes the applied_migrations row for 069 so the runner will re-apply on
-- next boot if desired.
--
-- Same FK-OFF dance as the up migration (PRAGMA outside BEGIN/COMMIT).
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;

DROP VIEW  IF EXISTS products_full;
DROP TABLE IF EXISTS products_old;

-- Recreate table with the PRE-069 shape (units columns nullable, no default).
CREATE TABLE products_old (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sku                      INTEGER UNIQUE NOT NULL,
    product_name             TEXT    NOT NULL,
    units_per_carton         INTEGER,                       -- ← reverted to nullable
    units_per_box            INTEGER,                       -- ← reverted to nullable
    unit_type                TEXT    NOT NULL DEFAULT 'ตัว',
    hard_to_sell             INTEGER NOT NULL DEFAULT 0,
    cost_price               REAL    NOT NULL DEFAULT 0.0,
    base_sell_price          REAL    NOT NULL DEFAULT 0.0,
    low_stock_threshold      INTEGER NOT NULL DEFAULT 10,
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    shopee_stock             INTEGER NOT NULL DEFAULT 0,
    lazada_stock             INTEGER NOT NULL DEFAULT 0,
    brand_id                 INTEGER REFERENCES brands(id),
    category_id              INTEGER REFERENCES categories(id),
    family_id                INTEGER REFERENCES product_families(id),
    color_code               TEXT    REFERENCES color_finish_codes(code),
    packaging                TEXT,
    series                   TEXT,
    model                    TEXT,
    size                     TEXT,
    condition                TEXT,
    pack_variant             TEXT,
    sub_category             TEXT,
    sku_code                 TEXT,
    sku_code_locked          INTEGER NOT NULL DEFAULT 0
                                  CHECK(sku_code_locked IN (0, 1)),
    material                 TEXT,
    sub_category_short_code  TEXT
);

INSERT INTO products_old
    (id, sku, product_name,
     units_per_carton, units_per_box,
     unit_type, hard_to_sell, cost_price, base_sell_price,
     low_stock_threshold, is_active, created_at, updated_at,
     shopee_stock, lazada_stock,
     brand_id, category_id, family_id, color_code,
     packaging, series, model, size, condition, pack_variant,
     sub_category, sku_code, sku_code_locked,
     material, sub_category_short_code)
SELECT
     id, sku, product_name,
     units_per_carton, units_per_box,
     unit_type, hard_to_sell, cost_price, base_sell_price,
     low_stock_threshold, is_active, created_at, updated_at,
     shopee_stock, lazada_stock,
     brand_id, category_id, family_id, color_code,
     packaging, series, model, size, condition, pack_variant,
     sub_category, sku_code, sku_code_locked,
     material, sub_category_short_code
FROM products;

DROP TABLE products;
ALTER TABLE products_old RENAME TO products;

-- Recreate products_full VIEW (same as up).
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

-- Recreate indexes.
CREATE INDEX idx_products_brand        ON products(brand_id);
CREATE INDEX idx_products_category     ON products(category_id);
CREATE INDEX idx_products_family       ON products(family_id);
CREATE INDEX idx_products_color_code   ON products(color_code);
CREATE INDEX idx_products_packaging    ON products(packaging);
CREATE INDEX idx_products_sub_category ON products(sub_category);
CREATE UNIQUE INDEX idx_products_sku_code ON products(sku_code) WHERE sku_code IS NOT NULL;

-- Recreate triggers (same as up).

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
            'sku', NEW.sku,
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
    OR OLD.sku                 IS NOT NEW.sku
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
        UNION ALL SELECT 'sku',                 OLD.sku,                 NEW.sku                 WHERE OLD.sku                 IS NOT NEW.sku
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
            'sku', OLD.sku,
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
    INSERT INTO product_price_history (product_id, field_name, old_value, new_value)
    SELECT NEW.id, field, old_v, new_v
    FROM (
                  SELECT 'cost_price'          AS field, OLD.cost_price          AS old_v, NEW.cost_price          AS new_v WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',             OLD.base_sell_price,             NEW.base_sell_price             WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'low_stock_threshold',         OLD.low_stock_threshold,         NEW.low_stock_threshold         WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    );
END;

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

-- Clean up applied_migrations bookkeeping so the runner will re-apply if desired.
DELETE FROM applied_migrations WHERE filename = '069_products_units_not_null.sql';

COMMIT;

PRAGMA foreign_keys = ON;
