-- 010_regions_salespersons.sql
-- Phase D2 of the schema refactor.
--
-- Adds two master tables that previously lived as denormalised TEXT
-- columns + a half-finished side table:
--
--   regions      — hierarchical sales region (parent_id supports the
--                  ภาคกลาง → กท / ทว / นอ tree, but stays NULL on seed:
--                  Put will fill name_th + parent_id manually as he
--                  works the data).
--
--   salespersons — lookup from short numeric route code (stored in
--                  customers.salesperson, e.g. "06") to the full
--                  display string (e.g. "ต๋อ /06"). Seeded from the
--                  12 unique values found in the legacy
--                  customer_regions.salesperson column.
--
-- Decisions locked with Put on 2026-04-30:
--   Q-A=(b)  separate salespersons lookup table; customers.salesperson
--            keeps the short numeric code as-is.
--   Q-B=(a)  drop customer_regions.region entirely — it was a mixed
--            bag of zone duplicates + unclear English tags. Backed up
--            in inventory-pre-phase-D2-2026-04-30_150643.db.
--   Q-C      parent_id stays NULL across all 35 regions for now.
--
-- Migration 011 (later, after the app stops reading the old columns)
-- will DROP customers.zone and DROP TABLE customer_regions.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/010_regions_salespersons.sql
--
-- Rollback: 010_regions_salespersons.rollback.sql

BEGIN;

-- ── regions ───────────────────────────────────────────────────────────────
CREATE TABLE regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    UNIQUE NOT NULL,           -- 2-3 char Thai (กท, ทว, ขก)
    name_th    TEXT,                               -- ชื่อเต็ม (กรุงเทพฯ, ทวีวัฒนา) — fillable later
    parent_id  INTEGER REFERENCES regions(id),
    sort_order INTEGER NOT NULL DEFAULT 100,
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_regions_parent ON regions(parent_id);

-- seed 35 rows from DISTINCT customers.zone (code only, no name_th, no parent)
INSERT INTO regions (code)
SELECT DISTINCT zone
FROM customers
WHERE zone IS NOT NULL AND zone != ''
ORDER BY zone;

-- ── salespersons ──────────────────────────────────────────────────────────
CREATE TABLE salespersons (
    code       TEXT    PRIMARY KEY,                -- numeric route code: '02', '06', '06-L'
    name       TEXT    NOT NULL,                   -- display: 'น้อย /02', 'TOU /06-L'
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- seed 12 rows by parsing customer_regions.salesperson "ชื่อ /รหัส" format.
-- code = SUBSTR(s, INSTR(s,'/')+1)  → keeps full code including suffix (e.g. '06-L').
-- name = full original string.
--
-- customer_regions is a legacy manually-managed table that only exists in
-- the original local DB — fresh deploys (Railway, CI, new dev clones) won't
-- have it, and the SELECT below would abort the migration. Create an empty
-- stand-in if missing so the seed yields 0 rows on those environments;
-- real installs keep their populated table untouched.
CREATE TABLE IF NOT EXISTS customer_regions (
    salesperson TEXT
);

INSERT INTO salespersons (code, name)
SELECT DISTINCT
    SUBSTR(salesperson, INSTR(salesperson, '/') + 1) AS code,
    salesperson                                      AS name
FROM customer_regions
WHERE salesperson IS NOT NULL
  AND salesperson != ''
  AND INSTR(salesperson, '/') > 0;

-- ── customers.region_id ───────────────────────────────────────────────────
ALTER TABLE customers ADD COLUMN region_id INTEGER REFERENCES regions(id);
CREATE INDEX idx_customers_region ON customers(region_id);

UPDATE customers
SET region_id = (SELECT id FROM regions WHERE regions.code = customers.zone)
WHERE zone IS NOT NULL AND zone != '';

-- ── audit triggers: regions ───────────────────────────────────────────────
CREATE TRIGGER audit_regions_insert
AFTER INSERT ON regions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'regions', NEW.id, 'INSERT',
        json_object(
            'code', NEW.code, 'name_th', NEW.name_th,
            'parent_id', NEW.parent_id, 'sort_order', NEW.sort_order
        )
    );
END;

CREATE TRIGGER audit_regions_update
AFTER UPDATE ON regions
WHEN (
       OLD.code       IS NOT NEW.code
    OR OLD.name_th    IS NOT NEW.name_th
    OR OLD.parent_id  IS NOT NEW.parent_id
    OR OLD.sort_order IS NOT NEW.sort_order
    OR OLD.note       IS NOT NEW.note
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'regions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'code'       AS field, OLD.code       AS old_v, NEW.code       AS new_v WHERE OLD.code       IS NOT NEW.code
        UNION ALL SELECT 'name_th',             OLD.name_th,            NEW.name_th             WHERE OLD.name_th    IS NOT NEW.name_th
        UNION ALL SELECT 'parent_id',           OLD.parent_id,          NEW.parent_id           WHERE OLD.parent_id  IS NOT NEW.parent_id
        UNION ALL SELECT 'sort_order',          OLD.sort_order,         NEW.sort_order          WHERE OLD.sort_order IS NOT NEW.sort_order
        UNION ALL SELECT 'note',                OLD.note,               NEW.note                WHERE OLD.note       IS NOT NEW.note
    );
END;

CREATE TRIGGER audit_regions_delete
BEFORE DELETE ON regions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'regions', OLD.id, 'DELETE',
        json_object('code', OLD.code, 'name_th', OLD.name_th, 'parent_id', OLD.parent_id)
    );
END;

-- ── audit triggers: salespersons ──────────────────────────────────────────
CREATE TRIGGER audit_salespersons_insert
AFTER INSERT ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', NEW.rowid, 'INSERT',
        json_object('code', NEW.code, 'name', NEW.name, 'is_active', NEW.is_active)
    );
END;

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

CREATE TRIGGER audit_salespersons_delete
BEFORE DELETE ON salespersons
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'salespersons', OLD.rowid, 'DELETE',
        json_object('code', OLD.code, 'name', OLD.name)
    );
END;

COMMIT;
