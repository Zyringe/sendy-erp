-- 104_customer_contact_normalize.rollback.sql
-- Reverse mig 104. Run manually; the migration runner does not auto-rollback.
-- Requires SQLite 3.35+ for ALTER TABLE DROP COLUMN (dev + Railway run 3.51).
--
-- Order matters: the mig-104 audit_customers_update trigger references the new
-- fax/nickname columns, so restore the pre-104 (mig 022) trigger FIRST, then drop
-- the columns, then drop the staging table.
--
-- Apply:
--   sqlite3 .../inventory.db < .../data/migrations/104_customer_contact_normalize.rollback.sql
--   then: DELETE FROM applied_migrations WHERE filename='104_customer_contact_normalize.sql';

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

-- Step 1: restore the mig 022 audit_customers_update trigger (no fax/nickname)
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

-- Step 2: drop the staging table (its indexes drop with it)
DROP TABLE IF EXISTS customer_contact_review;

-- Step 3: drop the added customers columns
ALTER TABLE customers DROP COLUMN contact_normalized_by;
ALTER TABLE customers DROP COLUMN contact_normalized_at;
ALTER TABLE customers DROP COLUMN contact_orig_json;
ALTER TABLE customers DROP COLUMN nickname;
ALTER TABLE customers DROP COLUMN fax;

COMMIT;
