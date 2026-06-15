-- 104_customer_contact_normalize.sql
-- Customer contact-data normalization: structured fax/nickname fields, an
-- immutable original snapshot for lossless reversibility, normalization
-- provenance, and a staging/review table that holds proposed clean values
-- until they are auto-applied (high confidence) or confirmed by a human.
--
-- customers gains:
--   fax                   — fax number(s) extracted out of the phone/contact grab-bag
--   nickname              — how the team refers to the customer (เฮีย/เจ๊… labels)
--   contact_orig_json     — frozen JSON snapshot {name,phone,contact,address} taken
--                           BEFORE the first normalization. Written once, never
--                           overwritten. This is the lossless guarantee — every
--                           original character is recoverable, so any normalization
--                           is fully reversible.
--   contact_normalized_at — timestamp of the normalization write (NULL = untouched;
--                           also the merge-protect flag for the BSN re-import in P4)
--   contact_normalized_by — username that applied / confirmed the normalization
--
-- customer_contact_review — one staging row per customer being normalized. proposed_*
--   hold the normalizer's output; confidence is 'auto' (lossless + unambiguous → safe
--   to auto-apply) or 'review' (needs a human); status walks
--   pending → applied | confirmed | skipped. No FK to customers by design (orphan-safe,
--   mirrors customer_crm in mig 103), but keyed 1:1 by customers.code.
--
-- The audit_customers_update trigger (last set by mig 022) is dropped + recreated to
-- also capture fax/nickname changes — per the trigger-rebuild rule for column adds.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/104_customer_contact_normalize.sql
-- Rollback: 104_customer_contact_normalize.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

ALTER TABLE customers ADD COLUMN fax                   TEXT;
ALTER TABLE customers ADD COLUMN nickname              TEXT;
ALTER TABLE customers ADD COLUMN contact_orig_json     TEXT;
ALTER TABLE customers ADD COLUMN contact_normalized_at TEXT;
ALTER TABLE customers ADD COLUMN contact_normalized_by TEXT;

CREATE TABLE customer_contact_review (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code     TEXT    NOT NULL,
    original_json     TEXT    NOT NULL,
    proposed_name     TEXT,
    proposed_nickname TEXT,
    proposed_phone    TEXT,
    proposed_fax      TEXT,
    proposed_contact  TEXT,
    proposed_address  TEXT,
    proposed_region   TEXT,
    confidence        TEXT    NOT NULL CHECK (confidence IN ('auto','review')),
    issues_json       TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','applied','confirmed','skipped')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    reviewed_by       TEXT,
    reviewed_at       TEXT
);
CREATE UNIQUE INDEX idx_ccr_customer ON customer_contact_review(customer_code);
CREATE INDEX idx_ccr_status ON customer_contact_review(status, confidence);

-- ── extend audit_customers_update to also capture fax / nickname ───────────────
-- Recreated from the mig 022 form with two columns added to both the WHEN guard
-- and the UNION SELECT body. No data is touched; this is pure DDL.
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
    OR OLD.fax         IS NOT NEW.fax
    OR OLD.nickname    IS NOT NEW.nickname
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
        UNION ALL SELECT 'fax',                  OLD.fax,                 NEW.fax                 WHERE OLD.fax         IS NOT NEW.fax
        UNION ALL SELECT 'nickname',             OLD.nickname,            NEW.nickname            WHERE OLD.nickname    IS NOT NEW.nickname
        UNION ALL SELECT 'tax_id',               OLD.tax_id,              NEW.tax_id              WHERE OLD.tax_id      IS NOT NEW.tax_id
        UNION ALL SELECT 'credit_days',          OLD.credit_days,         NEW.credit_days         WHERE OLD.credit_days IS NOT NEW.credit_days
        UNION ALL SELECT 'contact',              OLD.contact,             NEW.contact             WHERE OLD.contact     IS NOT NEW.contact
        UNION ALL SELECT 'lat',                  OLD.lat,                 NEW.lat                 WHERE OLD.lat         IS NOT NEW.lat
        UNION ALL SELECT 'lng',                  OLD.lng,                 NEW.lng                 WHERE OLD.lng         IS NOT NEW.lng
    );
END;

COMMIT;
