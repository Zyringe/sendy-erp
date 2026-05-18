-- 011_expenses.sql
-- Phase E3 of the schema refactor.
--
-- Adds three master tables to support per-company expense tracking
-- and unblock proper P&L reporting (currently only the revenue side
-- exists in sales_transactions / purchase_transactions).
--
--   companies          — 2 legal entities (BSN, SD). Reused by E1
--                        (purchase_orders) and any future per-company
--                        FK on existing tables.
--
--   expense_categories — 8-category seed (rent, salary, shipping,
--                        utilities, office, platform fees, gov/tax,
--                        other). Sort order leaves gaps for future
--                        inserts.
--
--   expense_log        — one row per non-purchase expense. Amount is
--                        split into pre-VAT base + VAT input (so VAT
--                        deductions can be tracked separately on the
--                        tax filing side).
--
-- Decisions locked with Put on 2026-04-30:
--   Q1=(b)  companies as a real FK target (not a TEXT enum) so the
--           same table can be reused by E1 / future P&L joins.
--   Q2=(b)  amount_pre_vat + vat_amount split (matches ใบกำกับ; no
--           vat_type flag needed since expenses are entered manually).
--   Q3=(a)  flat expense_log only — recurring (rent, salary) become
--           repeated inserts; no separate recurring_template table.
--   Q4      no receipt_path / attachment column for now; can ALTER
--           ADD later.
--
-- Apply:
--   sqlite3 /Users/putty/Documents/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Documents/Sendai-Boonsawat/sendy_erp/data/migrations/011_expenses.sql
--
-- Rollback: 011_expenses.rollback.sql

BEGIN;

-- ── companies ─────────────────────────────────────────────────────────────
CREATE TABLE companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,            -- 'BSN', 'SD'
    name_th     TEXT    NOT NULL,                    -- legal Thai name
    short_name  TEXT,                                -- display short
    tax_id      TEXT,                                -- เลขผู้เสียภาษี 13 หลัก (NULL until known)
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO companies (code, name_th, short_name) VALUES
    ('BSN', 'บุญสวัสดิ์ นำชัย', 'BSN'),
    ('SD',  'เซ็นไดเทรดดิ้ง',  'Sendai Trading');

-- ── expense_categories ────────────────────────────────────────────────────
CREATE TABLE expense_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    UNIQUE NOT NULL,
    name_th     TEXT    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO expense_categories (code, name_th, sort_order) VALUES
    ('rent',       'ค่าเช่า',                       10),
    ('salary',     'เงินเดือน',                      20),
    ('shipping',   'ค่าขนส่ง',                      30),
    ('utilities',  'ค่าน้ำค่าไฟ',                    40),
    ('office',     'ค่าใช้จ่ายสำนักงาน',             50),
    ('platform',   'ค่าธรรมเนียมแพลตฟอร์ม',         60),
    ('tax_fees',   'ภาษี / ค่าธรรมเนียมราชการ',     70),
    ('other',      'อื่นๆ',                         999);

-- ── expense_log ───────────────────────────────────────────────────────────
CREATE TABLE expense_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date_iso        TEXT    NOT NULL,                -- YYYY-MM-DD (Gregorian)
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    category_id     INTEGER NOT NULL REFERENCES expense_categories(id),
    amount_pre_vat  REAL    NOT NULL,                 -- ยอดก่อน VAT
    vat_amount      REAL    NOT NULL DEFAULT 0,        -- VAT input ที่จ่าย (0 ถ้าไม่มี)
    description     TEXT,                              -- รายละเอียดสั้น ๆ
    doc_no          TEXT,                              -- เลขใบกำกับ (NULL ได้)
    created_by      TEXT,                              -- username (NULL ถ้า system)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_expense_log_date     ON expense_log(date_iso);
CREATE INDEX idx_expense_log_company  ON expense_log(company_id);
CREATE INDEX idx_expense_log_category ON expense_log(category_id);

-- ── audit triggers: companies ─────────────────────────────────────────────
CREATE TRIGGER audit_companies_insert
AFTER INSERT ON companies
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'companies', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'short_name', NEW.short_name, 'tax_id', NEW.tax_id,
            'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_companies_update
AFTER UPDATE ON companies
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.short_name IS NOT NEW.short_name
    OR OLD.tax_id     IS NOT NEW.tax_id
    OR OLD.is_active  IS NOT NEW.is_active
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'companies', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th            WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'short_name',          OLD.short_name,         NEW.short_name         WHERE OLD.short_name IS NOT NEW.short_name
        UNION ALL SELECT 'tax_id',              OLD.tax_id,             NEW.tax_id             WHERE OLD.tax_id     IS NOT NEW.tax_id
        UNION ALL SELECT 'is_active',           OLD.is_active,          NEW.is_active          WHERE OLD.is_active  IS NOT NEW.is_active
        UNION ALL SELECT 'note',                OLD.note,               NEW.note               WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_companies_delete
BEFORE DELETE ON companies
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'companies', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th, 'tax_id', OLD.tax_id)
    );
END;

-- ── audit triggers: expense_categories ────────────────────────────────────
CREATE TRIGGER audit_expense_categories_insert
AFTER INSERT ON expense_categories
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_categories', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'sort_order', NEW.sort_order, 'is_active', NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_expense_categories_update
AFTER UPDATE ON expense_categories
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.sort_order IS NOT NEW.sort_order
    OR OLD.is_active  IS NOT NEW.is_active
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'expense_categories', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th            WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'sort_order',          OLD.sort_order,         NEW.sort_order         WHERE OLD.sort_order IS NOT NEW.sort_order
        UNION ALL SELECT 'is_active',           OLD.is_active,          NEW.is_active          WHERE OLD.is_active  IS NOT NEW.is_active
        UNION ALL SELECT 'note',                OLD.note,               NEW.note               WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_expense_categories_delete
BEFORE DELETE ON expense_categories
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_categories', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th)
    );
END;

-- ── audit triggers: expense_log ───────────────────────────────────────────
CREATE TRIGGER audit_expense_log_insert
AFTER INSERT ON expense_log
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_log', NEW.id, 'INSERT',
        json_object(
            'date_iso', NEW.date_iso, 'company_id', NEW.company_id,
            'category_id', NEW.category_id, 'amount_pre_vat', NEW.amount_pre_vat,
            'vat_amount', NEW.vat_amount, 'description', NEW.description,
            'doc_no', NEW.doc_no, 'created_by', NEW.created_by
        )
    );
END;

CREATE TRIGGER audit_expense_log_update
AFTER UPDATE ON expense_log
WHEN (
       OLD.date_iso       IS NOT NEW.date_iso
    OR OLD.company_id     IS NOT NEW.company_id
    OR OLD.category_id    IS NOT NEW.category_id
    OR OLD.amount_pre_vat IS NOT NEW.amount_pre_vat
    OR OLD.vat_amount     IS NOT NEW.vat_amount
    OR OLD.description    IS NOT NEW.description
    OR OLD.doc_no         IS NOT NEW.doc_no
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'expense_log', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'date_iso'       AS field, OLD.date_iso       AS old_v, NEW.date_iso       AS new_v WHERE OLD.date_iso       IS NOT NEW.date_iso
        UNION ALL SELECT 'company_id',              OLD.company_id,              NEW.company_id              WHERE OLD.company_id     IS NOT NEW.company_id
        UNION ALL SELECT 'category_id',             OLD.category_id,             NEW.category_id             WHERE OLD.category_id    IS NOT NEW.category_id
        UNION ALL SELECT 'amount_pre_vat',          OLD.amount_pre_vat,          NEW.amount_pre_vat          WHERE OLD.amount_pre_vat IS NOT NEW.amount_pre_vat
        UNION ALL SELECT 'vat_amount',              OLD.vat_amount,              NEW.vat_amount              WHERE OLD.vat_amount     IS NOT NEW.vat_amount
        UNION ALL SELECT 'description',             OLD.description,             NEW.description             WHERE OLD.description    IS NOT NEW.description
        UNION ALL SELECT 'doc_no',                  OLD.doc_no,                  NEW.doc_no                  WHERE OLD.doc_no         IS NOT NEW.doc_no
    );
END;

CREATE TRIGGER audit_expense_log_delete
BEFORE DELETE ON expense_log
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'expense_log', OLD.id, 'DELETE',
        json_object(
            'date_iso', OLD.date_iso, 'company_id', OLD.company_id,
            'category_id', OLD.category_id, 'amount_pre_vat', OLD.amount_pre_vat,
            'vat_amount', OLD.vat_amount, 'doc_no', OLD.doc_no
        )
    );
END;

COMMIT;
