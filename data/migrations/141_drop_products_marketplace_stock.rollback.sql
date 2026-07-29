-- ============================================================================
-- Rollback for 141 — restore products.shopee_stock / products.lazada_stock.
--
-- The data is genuinely discarded by the forward migration (these columns
-- were write-only — nothing read them; the live UI numbers always came from
-- platform_skus subqueries, unaffected by this table). Restoring both as
-- INTEGER NOT NULL DEFAULT 0 is therefore correct and lossless in the sense
-- that matters: no code path depended on their prior values.
--
-- Per the rename/cosmetic-migration safety rule, this INSERT...SELECT reads
-- from the CURRENT products table (not a pre-141 snapshot — none was taken;
-- none was needed since nothing of value is being recovered), so any rows
-- inserted after the forward migration survive the rollback.
--
-- Same FK-OFF dance as the forward migration.
-- ============================================================================

PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = OFF;

BEGIN IMMEDIATE;

DROP VIEW  IF EXISTS products_full;
DROP TABLE IF EXISTS products_old;

CREATE TABLE products_old (
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
    shopee_stock             INTEGER NOT NULL DEFAULT 0,        -- ← restored, zeroed (see header)
    lazada_stock             INTEGER NOT NULL DEFAULT 0,        -- ← restored, zeroed (see header)
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

INSERT INTO products_old
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

DROP TABLE products;
ALTER TABLE products_old RENAME TO products;

-- Recreate the 7 indexes.
CREATE INDEX idx_products_brand        ON products(brand_id);
CREATE INDEX idx_products_category     ON products(category_id);
CREATE INDEX idx_products_family       ON products(family_id);
CREATE INDEX idx_products_color_code   ON products(color_code);
CREATE INDEX idx_products_sub_category ON products(sub_category);
CREATE UNIQUE INDEX idx_products_sku_code ON products(sku_code) WHERE sku_code IS NOT NULL;
CREATE INDEX idx_products_packaging_th ON products(packaging_th);

-- Recreate products_full WITH p.shopee_stock, p.lazada_stock.
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
    p.shopee_stock, p.lazada_stock,
    p.created_at, p.updated_at
FROM products p
LEFT JOIN brands b              ON b.id   = p.brand_id
LEFT JOIN categories c          ON c.id   = p.category_id
LEFT JOIN color_finish_codes cf ON cf.code = p.color_code
LEFT JOIN stock_levels s        ON s.product_id = p.id;

-- Recreate the 9 triggers verbatim (same live SQL the forward migration
-- carried — none reference either column, so nothing here changes shape).

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

DELETE FROM applied_migrations WHERE filename = '141_drop_products_marketplace_stock.sql';

COMMIT;

PRAGMA foreign_keys = ON;
