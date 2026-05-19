-- 022_audit_customers_region.sql
-- Extend audit_customers_update trigger to capture region_id changes.
--
-- Background: migration 003 created audit_customers_update covering the
-- columns that existed at the time. Migration 010 added customers.region_id
-- (FK → regions.id) but never extended the trigger, so region reassignments
-- have been silent in audit_log since 2026-04-30.
--
-- This migration drops the existing trigger and recreates it with region_id
-- in both the WHEN clause and the UNION SELECT body.
--
-- No data is touched; the trigger is purely DDL.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/022_audit_customers_region.sql
--
-- Verify:
--   sqlite3 .../inventory.db ".schema audit_customers_update"
--     → output should mention region_id in both WHEN and UNION SELECT
--
-- Rollback: 022_audit_customers_region.rollback.sql

BEGIN;

DROP TRIGGER IF EXISTS audit_customers_update;

CREATE TRIGGER audit_customers_update
AFTER UPDATE ON customers
WHEN (
       OLD.name        IS NOT NEW.name
    OR OLD.salesperson IS NOT NEW.salesperson
    OR OLD.zone        IS NOT NEW.zone
    OR OLD.region_id   IS NOT NEW.region_id
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
        UNION ALL SELECT 'region_id',            OLD.region_id,           NEW.region_id           WHERE OLD.region_id   IS NOT NEW.region_id
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
