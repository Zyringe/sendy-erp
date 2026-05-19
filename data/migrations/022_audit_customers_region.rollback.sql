-- 022_audit_customers_region.rollback.sql
-- Restore audit_customers_update to its pre-region_id form (matches the
-- trigger created by 003_audit_triggers.sql).
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/022_audit_customers_region.rollback.sql
--
-- Verify:
--   sqlite3 .../inventory.db ".schema audit_customers_update"
--     → should NOT mention region_id

BEGIN;

DROP TRIGGER IF EXISTS audit_customers_update;

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

COMMIT;
