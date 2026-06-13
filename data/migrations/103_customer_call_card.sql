-- 103_customer_call_card.sql
-- Call-card feature: an append-only call log + a mutable per-customer CRM meta row.
--   customer_call_log  — append-only (kind/note/call/data_flag), soft-delete via
--                        deleted_at/deleted_by; NO audit triggers needed.
--   customer_crm       — mutable master-style row (tags, next_call_date,
--                        call_target_days) overwritten in place; gets 3 audit_log
--                        triggers per Sendy's precedent for mutable tables.
--
-- Canonical key: customer_crm.customer_code stores the ar_followup customer_key:
--   customers.code when the customer has a master row, else the customer NAME for
--   orphan customers. No FK by design — orphan customers exist in
--   sales_transactions without a customers row.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/103_customer_call_card.sql
--
-- Rollback: 103_customer_call_card.rollback.sql

PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

CREATE TABLE customer_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('note','call','data_flag')),
    body          TEXT,
    created_by    TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    deleted_at    TEXT,
    deleted_by    TEXT
);
CREATE INDEX idx_call_log_customer ON customer_call_log(customer_code, created_at);

CREATE TABLE customer_crm (
    customer_code    TEXT    PRIMARY KEY,
    tags             TEXT,
    next_call_date   TEXT,
    call_target_days INTEGER,
    updated_by       TEXT,
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── customer_crm audit triggers ───────────────────────────────────────────
-- customer_crm is a mutable master-style table (values overwrite in place), so
-- it follows the 3-trigger audit_log idiom from migration 003 (customers).
-- customer_code is a TEXT PK, so row_id uses the rowid subquery (same as the
-- customers triggers). The UPDATE trigger fires only on meaningful changes
-- (tags / next_call_date / call_target_days) and records only changed fields;
-- a bump to updated_at / updated_by alone does NOT trigger an audit row.
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

COMMIT;
