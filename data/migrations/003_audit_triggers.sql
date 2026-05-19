-- 003_audit_triggers.sql
-- Phase A2 of the schema refactor.
-- Activates audit_log for the master tables that get hand-edited:
--   products, customers, suppliers.
-- Each table gets 3 triggers (INSERT / UPDATE / DELETE).
-- changed_fields is JSON: INSERT logs the inserted row, DELETE logs the
-- deleted row, UPDATE logs only fields that actually changed (as
-- {field: [old, new]} pairs).
--
-- The `user` column is left NULL for trigger-driven entries — Flask
-- session context isn't available inside SQLite. App-level audit (with
-- known user) can write directly to audit_log.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/003_audit_triggers.sql
--
-- Rollback: 003_audit_triggers.rollback.sql
--
-- Requires SQLite >= 3.38 for built-in json_object(). Verified on macOS default.

BEGIN;

-- ── products ──────────────────────────────────────────────────────────────
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

-- ── customers ─────────────────────────────────────────────────────────────
CREATE TRIGGER audit_customers_insert
AFTER INSERT ON customers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customers',
        (SELECT MAX(rowid) FROM customers WHERE code = NEW.code),
        'INSERT',
        json_object(
            'code', NEW.code, 'name', NEW.name,
            'salesperson', NEW.salesperson, 'zone', NEW.zone,
            'phone', NEW.phone, 'credit_days', NEW.credit_days
        )
    );
END;

CREATE TRIGGER audit_customers_update
AFTER UPDATE ON customers
WHEN (
       OLD.name        IS NOT NEW.name
    OR OLD.salesperson IS NOT NEW.salesperson
    OR OLD.zone        IS NOT NEW.zone
    OR OLD.address     IS NOT NEW.address
    OR OLD.phone       IS NOT NEW.phone
    OR OLD.tax_id      IS NOT NEW.tax_id
    OR OLD.credit_days IS NOT NEW.credit_days
    OR OLD.contact     IS NOT NEW.contact
    OR OLD.lat         IS NOT NEW.lat
    OR OLD.lng         IS NOT NEW.lng
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'customers',
           (SELECT rowid FROM customers WHERE code = NEW.code),
           'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'name'        AS field, OLD.name        AS old_v, NEW.name        AS new_v WHERE OLD.name        IS NOT NEW.name
        UNION ALL SELECT 'salesperson',          OLD.salesperson,         NEW.salesperson         WHERE OLD.salesperson IS NOT NEW.salesperson
        UNION ALL SELECT 'zone',                 OLD.zone,                NEW.zone                WHERE OLD.zone        IS NOT NEW.zone
        UNION ALL SELECT 'address',              OLD.address,             NEW.address             WHERE OLD.address     IS NOT NEW.address
        UNION ALL SELECT 'phone',                OLD.phone,               NEW.phone               WHERE OLD.phone       IS NOT NEW.phone
        UNION ALL SELECT 'tax_id',               OLD.tax_id,              NEW.tax_id              WHERE OLD.tax_id      IS NOT NEW.tax_id
        UNION ALL SELECT 'credit_days',          OLD.credit_days,         NEW.credit_days         WHERE OLD.credit_days IS NOT NEW.credit_days
        UNION ALL SELECT 'contact',              OLD.contact,             NEW.contact             WHERE OLD.contact     IS NOT NEW.contact
        UNION ALL SELECT 'lat',                  OLD.lat,                 NEW.lat                 WHERE OLD.lat         IS NOT NEW.lat
        UNION ALL SELECT 'lng',                  OLD.lng,                 NEW.lng                 WHERE OLD.lng         IS NOT NEW.lng
    );
END;

CREATE TRIGGER audit_customers_delete
BEFORE DELETE ON customers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customers',
        (SELECT rowid FROM customers WHERE code = OLD.code),
        'DELETE',
        json_object(
            'code', OLD.code, 'name', OLD.name, 'phone', OLD.phone,
            'address', OLD.address, 'tax_id', OLD.tax_id
        )
    );
END;

-- ── suppliers ─────────────────────────────────────────────────────────────
CREATE TRIGGER audit_suppliers_insert
AFTER INSERT ON suppliers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'suppliers', NEW.id, 'INSERT',
        json_object(
            'name', NEW.name, 'display_name', NEW.display_name,
            'contact_info', NEW.contact_info, 'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_suppliers_update
AFTER UPDATE ON suppliers
WHEN (
       OLD.name         IS NOT NEW.name
    OR OLD.display_name IS NOT NEW.display_name
    OR OLD.contact_info IS NOT NEW.contact_info
    OR OLD.note         IS NOT NEW.note
    OR OLD.is_active    IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'suppliers', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'name'         AS field, OLD.name         AS old_v, NEW.name         AS new_v WHERE OLD.name         IS NOT NEW.name
        UNION ALL SELECT 'display_name',          OLD.display_name,         NEW.display_name         WHERE OLD.display_name IS NOT NEW.display_name
        UNION ALL SELECT 'contact_info',          OLD.contact_info,         NEW.contact_info         WHERE OLD.contact_info IS NOT NEW.contact_info
        UNION ALL SELECT 'note',                  OLD.note,                 NEW.note                 WHERE OLD.note         IS NOT NEW.note
        UNION ALL SELECT 'is_active',             OLD.is_active,            NEW.is_active            WHERE OLD.is_active    IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_suppliers_delete
BEFORE DELETE ON suppliers
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'suppliers', OLD.id, 'DELETE',
        json_object(
            'name', OLD.name, 'display_name', OLD.display_name,
            'contact_info', OLD.contact_info
        )
    );
END;

COMMIT;
