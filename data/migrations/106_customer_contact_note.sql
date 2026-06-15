-- 106_customer_contact_note.sql
-- Give billing/delivery-schedule notes (e.g. "วางบิล25-30 เก็บ5,6,7", weekday lists) their own
-- home so the contact field stays people-only. Adds:
--   customers.contact_note               — the dedicated notes field
--   customer_contact_review.proposed_note — the normalizer's proposed note for review
-- Extends audit_customers_update (last set by mig 104) to also capture contact_note.
--
-- Apply:
--   sqlite3 .../inventory.db < .../data/migrations/106_customer_contact_note.sql
-- Rollback: 106_customer_contact_note.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

ALTER TABLE customers ADD COLUMN contact_note TEXT;
ALTER TABLE customer_contact_review ADD COLUMN proposed_note TEXT;

-- ── extend audit_customers_update to also capture contact_note ───────────────
DROP TRIGGER IF EXISTS audit_customers_update;
CREATE TRIGGER audit_customers_update
AFTER UPDATE ON customers
WHEN (
       OLD.name         IS NOT NEW.name
    OR OLD.salesperson  IS NOT NEW.salesperson
    OR OLD.zone         IS NOT NEW.zone
    OR OLD.region_id    IS NOT NEW.region_id
    OR OLD.address      IS NOT NEW.address
    OR OLD.phone        IS NOT NEW.phone
    OR OLD.fax          IS NOT NEW.fax
    OR OLD.nickname     IS NOT NEW.nickname
    OR OLD.contact_note IS NOT NEW.contact_note
    OR OLD.tax_id       IS NOT NEW.tax_id
    OR OLD.credit_days  IS NOT NEW.credit_days
    OR OLD.contact      IS NOT NEW.contact
    OR OLD.lat          IS NOT NEW.lat
    OR OLD.lng          IS NOT NEW.lng
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'customers',
           (SELECT rowid FROM customers WHERE code = NEW.code),
           'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'name'         AS field, OLD.name         AS old_v, NEW.name         AS new_v WHERE OLD.name         IS NOT NEW.name
        UNION ALL SELECT 'salesperson',           OLD.salesperson,          NEW.salesperson          WHERE OLD.salesperson  IS NOT NEW.salesperson
        UNION ALL SELECT 'zone',                  OLD.zone,                 NEW.zone                 WHERE OLD.zone         IS NOT NEW.zone
        UNION ALL SELECT 'region_id',             OLD.region_id,            NEW.region_id            WHERE OLD.region_id    IS NOT NEW.region_id
        UNION ALL SELECT 'address',               OLD.address,              NEW.address              WHERE OLD.address      IS NOT NEW.address
        UNION ALL SELECT 'phone',                 OLD.phone,                NEW.phone                WHERE OLD.phone        IS NOT NEW.phone
        UNION ALL SELECT 'fax',                   OLD.fax,                  NEW.fax                  WHERE OLD.fax          IS NOT NEW.fax
        UNION ALL SELECT 'nickname',              OLD.nickname,             NEW.nickname             WHERE OLD.nickname     IS NOT NEW.nickname
        UNION ALL SELECT 'contact_note',          OLD.contact_note,         NEW.contact_note         WHERE OLD.contact_note IS NOT NEW.contact_note
        UNION ALL SELECT 'tax_id',                OLD.tax_id,               NEW.tax_id               WHERE OLD.tax_id       IS NOT NEW.tax_id
        UNION ALL SELECT 'credit_days',           OLD.credit_days,          NEW.credit_days          WHERE OLD.credit_days  IS NOT NEW.credit_days
        UNION ALL SELECT 'contact',               OLD.contact,              NEW.contact              WHERE OLD.contact      IS NOT NEW.contact
        UNION ALL SELECT 'lat',                   OLD.lat,                  NEW.lat                  WHERE OLD.lat          IS NOT NEW.lat
        UNION ALL SELECT 'lng',                   OLD.lng,                  NEW.lng                  WHERE OLD.lng          IS NOT NEW.lng
    );
END;

COMMIT;
