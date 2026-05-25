-- ============================================================================
-- Migration 086 — promotions: extend for bundle / gift / mixed promo types
--
-- Why
--   `promotions` currently supports only promo_type ∈ ('percent','fixed').
--   The 1,969-row catalog normalization (2026-05-25) produced ~100 rows with
--   structured promo data we cannot store: bundle qty (ซื้อ 12 แถม 1),
--   bundle qty escalation tiers (12+1 / 24+3 / 50+10), pack-level conditions
--   (ซื้อยกลัง / ซื้อยกล่อง), free-gift items (different-SKU แถมดจ.สแตนเลส),
--   and bundle counting unit (ดอก, etc.).
--   Marketplace listings (TikTok / Lazada / Shopee — active scoping) will need
--   to auto-display these on product pages. Building the extension now, while
--   the table is empty in prod (0 rows), is the cheapest moment in its
--   lifecycle to relax CHECK constraints and add columns.
--
-- What
--   1. Make `discount_value` nullable (bundle/gift rows have no percent value).
--   2. Add 7 columns:
--        bundle_buy        INTEGER NULL  -- "ซื้อ 12 แถม 1" → 12
--        bundle_free       INTEGER NULL  -- "ซื้อ 12 แถม 1" → 1
--        bundle_unit       TEXT    NULL  -- counting unit (e.g. 'ดอก', matches products.unit_type)
--        bundle_condition  TEXT    NULL  -- pack-level qualifier: 'ยกลัง' or 'ยกล่อง'
--        bundle_tiers_json TEXT    NULL  -- JSON array for escalation: [{"buy":12,"free":1},{"buy":24,"free":3}]
--        gift_desc         TEXT    NULL  -- free-item description (different SKU)
--        gift_qty          TEXT    NULL  -- free-item qty as natural string (e.g. '20 ดอก', '1 ใบ')
--   3. Relax CHECK on `promo_type` to allow 'bundle','mixed','gift' (was percent/fixed only).
--   4. Add strict per-type shape integrity via CHECK: catches malformed inserts
--      (e.g. promo_type='bundle' AND bundle_buy IS NULL → REJECTED).
--   5. Add 2 indexes: idx_promotions_product (product_id), idx_promotions_active.
--   6. Add 3 audit triggers (INSERT/UPDATE/DELETE → audit_log). Table currently
--      has no audit coverage.
--   7. Add ON DELETE CASCADE on the product_id FK (closes orphan-promo risk).
--
-- How
--   SQLite cannot ALTER COLUMN to change CHECK / NOT NULL → table rebuild dance:
--     1) DROP existing dependent objects (none — table has no triggers/indexes).
--     2) CREATE promotions_new with extended schema + per-type CHECK.
--     3) INSERT…SELECT (column-explicit) from old to new.
--     4) DROP TABLE promotions + RENAME promotions_new → promotions.
--     5) CREATE 2 new indexes.
--     6) CREATE 3 new audit triggers.
--
-- Forward-compat note
--   When per-customer pricing overrides become a real need, the upgrade path
--   is a 1-line ALTER:
--     ALTER TABLE promotions ADD COLUMN customer_id INTEGER NULL
--       REFERENCES customers(id);
--     CREATE INDEX idx_promotions_customer ON promotions(customer_id, product_id);
--   The strict per-type CHECKs are orthogonal to customer_id and do not need
--   to change. Lookup logic in effective_price() would then prefer
--   `customer_id = ?` rows over `customer_id IS NULL` (baseline).
--
-- FK hazard: same recipe as mig 069 — PRAGMA foreign_keys = OFF before BEGIN
-- so the DROP+RENAME doesn't invalidate references from other tables.
-- promotions today is FK'd only FROM itself (product_id → products), and no
-- table references promotions(id), so the risk is low — but the pragma keeps
-- us safe if that ever changes.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1) Build new table with extended schema + relaxed type + strict per-type CHECKs.
DROP TABLE IF EXISTS promotions_new;

CREATE TABLE promotions_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    promo_name        TEXT    NOT NULL,
    promo_type        TEXT    NOT NULL,
    discount_value    REAL,                                            -- was NOT NULL; now nullable
    date_start        TEXT,
    date_end          TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),

    -- NEW columns (all nullable; CHECK enforces presence per promo_type)
    bundle_buy        INTEGER,
    bundle_free       INTEGER,
    bundle_unit       TEXT,
    bundle_condition  TEXT,
    bundle_tiers_json TEXT,
    gift_desc         TEXT,
    gift_qty          TEXT,

    -- Type enum + shape integrity per type
    CHECK (
        promo_type IN ('percent','fixed','bundle','mixed','gift')
        AND (bundle_condition IS NULL OR bundle_condition IN ('ยกลัง','ยกล่อง'))
        AND CASE promo_type
            WHEN 'percent' THEN
                discount_value IS NOT NULL
                AND discount_value BETWEEN 0 AND 100
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
            WHEN 'fixed' THEN
                discount_value IS NOT NULL
                AND discount_value > 0
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
            WHEN 'bundle' THEN
                bundle_buy IS NOT NULL AND bundle_free IS NOT NULL
                AND gift_desc IS NULL AND gift_qty IS NULL
                AND discount_value IS NULL
            WHEN 'gift' THEN
                gift_desc IS NOT NULL AND gift_qty IS NOT NULL
                AND bundle_buy IS NULL AND bundle_free IS NULL
                AND bundle_tiers_json IS NULL
                AND discount_value IS NULL
            WHEN 'mixed' THEN
                -- At least one structured field populated; any combination valid.
                (discount_value IS NOT NULL
                 OR bundle_buy IS NOT NULL
                 OR gift_desc IS NOT NULL)
        END
    )
);

-- 2) Copy data with EXPLICIT column list (column order discipline — mig 069 pattern).
INSERT INTO promotions_new
    (id, product_id, promo_name, promo_type, discount_value,
     date_start, date_end, is_active, created_at)
SELECT
     id, product_id, promo_name, promo_type, discount_value,
     date_start, date_end, is_active, created_at
FROM promotions;

-- 3) Table swap. DROP TABLE removes any attached triggers and indexes
-- (none on the old promotions table, but defensive).
DROP TABLE promotions;
ALTER TABLE promotions_new RENAME TO promotions;

-- 4) Indexes on the new table.
CREATE INDEX idx_promotions_product ON promotions(product_id);
CREATE INDEX idx_promotions_active  ON promotions(is_active, product_id);

-- 5) Audit triggers (modeled on mig 070 audit_transactions_* pattern).
--    Tracks all 14 mutable fields. UPDATE trigger uses WHEN clause to skip
--    no-op updates and json_group_object to emit only changed fields.

CREATE TRIGGER audit_promotions_insert
AFTER INSERT ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', NEW.id, 'INSERT',
        json_object(
            'product_id',        NEW.product_id,
            'promo_name',        NEW.promo_name,
            'promo_type',        NEW.promo_type,
            'discount_value',    NEW.discount_value,
            'bundle_buy',        NEW.bundle_buy,
            'bundle_free',       NEW.bundle_free,
            'bundle_unit',       NEW.bundle_unit,
            'bundle_condition',  NEW.bundle_condition,
            'bundle_tiers_json', NEW.bundle_tiers_json,
            'gift_desc',         NEW.gift_desc,
            'gift_qty',          NEW.gift_qty,
            'date_start',        NEW.date_start,
            'date_end',          NEW.date_end,
            'is_active',         NEW.is_active
        )
    );
END;

CREATE TRIGGER audit_promotions_update
AFTER UPDATE ON promotions
WHEN (
       OLD.product_id        IS NOT NEW.product_id
    OR OLD.promo_name        IS NOT NEW.promo_name
    OR OLD.promo_type        IS NOT NEW.promo_type
    OR OLD.discount_value    IS NOT NEW.discount_value
    OR OLD.bundle_buy        IS NOT NEW.bundle_buy
    OR OLD.bundle_free       IS NOT NEW.bundle_free
    OR OLD.bundle_unit       IS NOT NEW.bundle_unit
    OR OLD.bundle_condition  IS NOT NEW.bundle_condition
    OR OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
    OR OLD.gift_desc         IS NOT NEW.gift_desc
    OR OLD.gift_qty          IS NOT NEW.gift_qty
    OR OLD.date_start        IS NOT NEW.date_start
    OR OLD.date_end          IS NOT NEW.date_end
    OR OLD.is_active         IS NOT NEW.is_active
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'promotions', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
                  SELECT 'product_id'        AS field, OLD.product_id        AS old_v, NEW.product_id        AS new_v WHERE OLD.product_id        IS NOT NEW.product_id
        UNION ALL SELECT 'promo_name',                OLD.promo_name,                NEW.promo_name                WHERE OLD.promo_name        IS NOT NEW.promo_name
        UNION ALL SELECT 'promo_type',                OLD.promo_type,                NEW.promo_type                WHERE OLD.promo_type        IS NOT NEW.promo_type
        UNION ALL SELECT 'discount_value',            OLD.discount_value,            NEW.discount_value            WHERE OLD.discount_value    IS NOT NEW.discount_value
        UNION ALL SELECT 'bundle_buy',                OLD.bundle_buy,                NEW.bundle_buy                WHERE OLD.bundle_buy        IS NOT NEW.bundle_buy
        UNION ALL SELECT 'bundle_free',               OLD.bundle_free,               NEW.bundle_free               WHERE OLD.bundle_free       IS NOT NEW.bundle_free
        UNION ALL SELECT 'bundle_unit',               OLD.bundle_unit,               NEW.bundle_unit               WHERE OLD.bundle_unit       IS NOT NEW.bundle_unit
        UNION ALL SELECT 'bundle_condition',          OLD.bundle_condition,          NEW.bundle_condition          WHERE OLD.bundle_condition  IS NOT NEW.bundle_condition
        UNION ALL SELECT 'bundle_tiers_json',         OLD.bundle_tiers_json,         NEW.bundle_tiers_json         WHERE OLD.bundle_tiers_json IS NOT NEW.bundle_tiers_json
        UNION ALL SELECT 'gift_desc',                 OLD.gift_desc,                 NEW.gift_desc                 WHERE OLD.gift_desc         IS NOT NEW.gift_desc
        UNION ALL SELECT 'gift_qty',                  OLD.gift_qty,                  NEW.gift_qty                  WHERE OLD.gift_qty          IS NOT NEW.gift_qty
        UNION ALL SELECT 'date_start',                OLD.date_start,                NEW.date_start                WHERE OLD.date_start        IS NOT NEW.date_start
        UNION ALL SELECT 'date_end',                  OLD.date_end,                  NEW.date_end                  WHERE OLD.date_end          IS NOT NEW.date_end
        UNION ALL SELECT 'is_active',                 OLD.is_active,                 NEW.is_active                 WHERE OLD.is_active         IS NOT NEW.is_active
    );
END;

CREATE TRIGGER audit_promotions_delete
BEFORE DELETE ON promotions
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    VALUES (
        'promotions', OLD.id, 'DELETE',
        json_object(
            'product_id',        OLD.product_id,
            'promo_name',        OLD.promo_name,
            'promo_type',        OLD.promo_type,
            'discount_value',    OLD.discount_value,
            'bundle_buy',        OLD.bundle_buy,
            'bundle_free',       OLD.bundle_free,
            'bundle_unit',       OLD.bundle_unit,
            'bundle_condition',  OLD.bundle_condition,
            'bundle_tiers_json', OLD.bundle_tiers_json,
            'gift_desc',         OLD.gift_desc,
            'gift_qty',          OLD.gift_qty,
            'is_active',         OLD.is_active
        )
    );
END;

COMMIT;

PRAGMA foreign_keys = ON;
