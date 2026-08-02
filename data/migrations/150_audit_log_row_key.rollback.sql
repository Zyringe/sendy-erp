-- ============================================================================
-- Rollback for 150 — restore the pre-150 trigger bodies (no row_key writes)
-- on customers/salespersons/commission_assignments/customer_crm, then drop
-- audit_log.row_key.
--
-- SQLite 3.35+ supports DROP COLUMN directly (this codebase already relies
-- on it — e.g. migrations 004, 005, 010, 015, 017, 025, 026, 147). row_key
-- has no FK, no CHECK referencing another column, no generated-column
-- dependency and no view referencing it, so no table rebuild is needed.
--
-- Genuinely lossy: rows written after 150 (with a real row_key) lose that
-- column's data on rollback, same as any other rollback. `row_id` and every
-- other column are untouched — nothing else about audit_log's history is
-- lost.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

-- ── customers (PK: code) ────────────────────────────────────────────────
DROP TRIGGER IF EXISTS audit_customers_delete;
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

DROP TRIGGER IF EXISTS audit_customers_insert;
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

-- ── salespersons (PK: code) ─────────────────────────────────────────────
DROP TRIGGER IF EXISTS audit_salespersons_delete;
CREATE TRIGGER audit_salespersons_delete
BEFORE DELETE ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', OLD.rowid, 'DELETE',
        json_object('code', OLD.code, 'name', OLD.name)
    );
END;

DROP TRIGGER IF EXISTS audit_salespersons_insert;
CREATE TRIGGER audit_salespersons_insert
AFTER INSERT ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', NEW.rowid, 'INSERT',
        json_object('code', NEW.code, 'name', NEW.name, 'is_active', NEW.is_active)
    );
END;

DROP TRIGGER IF EXISTS audit_salespersons_update;
CREATE TRIGGER audit_salespersons_update
AFTER UPDATE ON salespersons
WHEN (
       OLD.code      IS NOT NEW.code
    OR OLD.name      IS NOT NEW.name
    OR OLD.is_active IS NOT NEW.is_active
    OR OLD.note      IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'salespersons', NEW.rowid, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'      AS field, OLD.code      AS old_v, NEW.code      AS new_v WHERE OLD.code      IS NOT NEW.code
        UNION ALL SELECT 'name',               OLD.name,               NEW.name               WHERE OLD.name      IS NOT NEW.name
        UNION ALL SELECT 'is_active',          OLD.is_active,          NEW.is_active          WHERE OLD.is_active IS NOT NEW.is_active
        UNION ALL SELECT 'note',               OLD.note,               NEW.note               WHERE OLD.note      IS NOT NEW.note
    );
END;

-- ── commission_assignments (PK: salesperson_code) ───────────────────────
DROP TRIGGER IF EXISTS audit_commission_assignments_delete;
CREATE TRIGGER audit_commission_assignments_delete
BEFORE DELETE ON commission_assignments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_assignments', OLD.rowid, 'DELETE',
        json_object('salesperson_code', OLD.salesperson_code, 'tier_id', OLD.tier_id));
END;

DROP TRIGGER IF EXISTS audit_commission_assignments_insert;
CREATE TRIGGER audit_commission_assignments_insert
AFTER INSERT ON commission_assignments
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES ('commission_assignments', NEW.rowid, 'INSERT',
        json_object('salesperson_code', NEW.salesperson_code,
                    'tier_id', NEW.tier_id,
                    'effective_from', NEW.effective_from,
                    'note', NEW.note));
END;

DROP TRIGGER IF EXISTS audit_commission_assignments_update;
CREATE TRIGGER audit_commission_assignments_update
AFTER UPDATE ON commission_assignments
WHEN (
       OLD.tier_id        IS NOT NEW.tier_id
    OR OLD.effective_from IS NOT NEW.effective_from
    OR OLD.note           IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'commission_assignments', NEW.rowid, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'tier_id'        AS field, OLD.tier_id        AS old_v, NEW.tier_id        AS new_v WHERE OLD.tier_id        IS NOT NEW.tier_id
        UNION ALL SELECT 'effective_from',          OLD.effective_from,          NEW.effective_from          WHERE OLD.effective_from IS NOT NEW.effective_from
        UNION ALL SELECT 'note',                    OLD.note,                    NEW.note                    WHERE OLD.note           IS NOT NEW.note
    );
END;

-- ── customer_crm (PK: customer_code) ────────────────────────────────────
DROP TRIGGER IF EXISTS audit_customer_crm_delete;
CREATE TRIGGER audit_customer_crm_delete
BEFORE DELETE ON customer_crm
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customer_crm',
        (SELECT rowid FROM customer_crm WHERE customer_code = OLD.customer_code),
        'DELETE',
        json_object(
            'customer_code', OLD.customer_code,
            'tags', OLD.tags,
            'next_call_date', OLD.next_call_date,
            'call_target_days', OLD.call_target_days
        )
    );
END;

DROP TRIGGER IF EXISTS audit_customer_crm_insert;
CREATE TRIGGER audit_customer_crm_insert
AFTER INSERT ON customer_crm
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'customer_crm',
        (SELECT MAX(rowid) FROM customer_crm WHERE customer_code = NEW.customer_code),
        'INSERT',
        json_object(
            'customer_code', NEW.customer_code,
            'tags', NEW.tags,
            'next_call_date', NEW.next_call_date,
            'call_target_days', NEW.call_target_days,
            'updated_by', NEW.updated_by
        )
    );
END;

DROP TRIGGER IF EXISTS audit_customer_crm_update;
CREATE TRIGGER audit_customer_crm_update
AFTER UPDATE ON customer_crm
WHEN (
       OLD.tags             IS NOT NEW.tags
    OR OLD.next_call_date   IS NOT NEW.next_call_date
    OR OLD.call_target_days IS NOT NEW.call_target_days
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'customer_crm',
           (SELECT rowid FROM customer_crm WHERE customer_code = NEW.customer_code),
           'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'tags'             AS field, OLD.tags             AS old_v, NEW.tags             AS new_v WHERE OLD.tags             IS NOT NEW.tags
        UNION ALL SELECT 'next_call_date',            OLD.next_call_date,            NEW.next_call_date            WHERE OLD.next_call_date   IS NOT NEW.next_call_date
        UNION ALL SELECT 'call_target_days',          OLD.call_target_days,          NEW.call_target_days          WHERE OLD.call_target_days IS NOT NEW.call_target_days
    );
END;

ALTER TABLE audit_log DROP COLUMN row_key;

COMMIT;
