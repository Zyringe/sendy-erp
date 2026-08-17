-- ============================================================================
-- 159 — products.weight_kg + products.weight_source.
--
-- WHY. TikTok Shop requires a parcel weight per variant before a listing can
-- go live, and this ERP has nowhere to keep one: `PRAGMA table_info(products)`
-- has no weight column and no weight/parcel/shipping table exists anywhere in
-- the schema (measured 2026-08-15). The only weight data the business owns
-- today is `platform_skus.weight_kg`, which is whatever was typed into Shopee
-- or Lazada at listing time — it covers 79 of the 145 SKUs cited by the TikTok
-- listing files, and it is NOT a scale reading.
--
-- Put's call (2026-08-17): the team weighs the remaining SKUs and the number
-- lives in the ERP, per product.
--
-- UNIT. `weight_kg` is the weight of ONE `products.unit_type` unit — so for a
-- product whose unit_type is 'โหล' this is the weight of a dozen, not of one
-- piece. That matches the weigh sheet the team works from
-- (`E-Commerce/TikTok/01_product-info/weights.py::sheet_rows` prints
-- `unit_type` on every row) and it is what makes a parcel weight derivable as
-- `weight_kg * qty_per_sale`. Getting this backwards is not academic: a
-- marketplace weight compared without normalising `qty_per_sale` read the 14in
-- disc as 5.0 kg against a documented 0.90, a 5.5x shipping over-declaration.
--
-- WHY TWO COLUMNS, NOT ONE. Codex round 2 refused a bare `weight_kg`: with
-- both scale readings and borrowed marketplace numbers eventually landing in
-- the same column, nothing downstream could tell which ones are trustworthy,
-- and the cheapest possible future shortcut ("seed the column from Shopee to
-- save the team a day") would erase the distinction silently and for good.
-- `weight_source` makes the provenance a column the database itself enforces
-- rather than a convention someone remembers.
--
-- The CHECKs pin both halves of the invariant, verified empirically on
-- sqlite_version 3.51.0 before this file was written:
--   * a non-positive weight is refused;
--   * a source without a weight is refused;
--   * a weight without a source is refused;
--   * NULL/NULL is accepted, which is every existing row — no backfill runs
--     here, so the ~2,000 products stay untouched and simply read "unknown".
--
-- AUDIT. `audit_products_update` enumerates its columns explicitly, so a new
-- column is invisible to it until the trigger is rewritten — a weight could be
-- changed with no audit row. The trigger is recreated below with both columns
-- added to the WHEN guard and to the changed-fields list. This is the whole
-- blast radius: grepping the schema for the other two products audit triggers
-- shows INSERT and DELETE log the row id only and do not enumerate columns.
--
-- ⚠ NOT re-runnable on its own: `ALTER TABLE ... ADD COLUMN` has no IF NOT
-- EXISTS form, so a second apply fails with `duplicate column name: weight_kg`.
-- Run 159_product_parcel_weight.rollback.sql first. The trigger half IS
-- re-runnable (DROP ... IF EXISTS before CREATE).
--
-- Rehearsed forward + rollback on a throwaway clone before commit; the
-- rollback returns `audit_products_update` byte-identical to its pre-159 body.
-- sqlite3.sqlite_version on this machine: 3.51.0. Prod runs 3.46.1.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE products ADD COLUMN weight_kg REAL
    CHECK (weight_kg IS NULL OR weight_kg > 0);

-- ⚠ This CHECK references weight_kg, which makes the DROP order in the
-- rollback load-bearing: weight_source must be dropped FIRST or SQLite fails
-- with "error in table products after drop column: no such column: weight_kg".
--
-- ⚠ The `IS NOT NULL` tests must come BEFORE the `IN` list, and this is not
-- style. A CHECK fails only when its expression is FALSE; NULL passes. The
-- readable-looking version
--     (weight_source IS NULL AND weight_kg IS NULL)
--  OR (weight_source IN (...) AND weight_kg IS NOT NULL)
-- evaluates to FALSE OR NULL = NULL for (weight_kg=0.9, weight_source=NULL)
-- because `NULL IN (...)` is NULL — so it ACCEPTED exactly the row this
-- constraint exists to refuse: a weight nobody can vouch for. Caught by
-- tests/test_product_weight.py, not by hand-probing; the hand probe had
-- tried the three other corners and missed this one.
ALTER TABLE products ADD COLUMN weight_source TEXT
    CHECK (
        (weight_kg IS NULL     AND weight_source IS NULL)
     OR (weight_kg IS NOT NULL AND weight_source IS NOT NULL
         AND weight_source IN ('measured', 'marketplace', 'estimated'))
    );

DROP TRIGGER IF EXISTS audit_products_update;

CREATE TRIGGER audit_products_update
AFTER UPDATE ON products
WHEN (
       OLD.product_name        IS NOT NEW.product_name
    OR OLD.unit_type           IS NOT NEW.unit_type
    OR OLD.cost_price          IS NOT NEW.cost_price
    OR OLD.base_sell_price     IS NOT NEW.base_sell_price
    OR OLD.units_per_carton    IS NOT NEW.units_per_carton
    OR OLD.units_per_box       IS NOT NEW.units_per_box
    OR OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
    OR OLD.hard_to_sell        IS NOT NEW.hard_to_sell
    OR OLD.is_active           IS NOT NEW.is_active
    OR OLD.weight_kg           IS NOT NEW.weight_kg
    OR OLD.weight_source       IS NOT NEW.weight_source
)
BEGIN
    INSERT INTO audit_log (table_name, row_id, action, changed_fields)
    SELECT 'products', NEW.id, 'UPDATE',
           json_group_object(field, json_array(old_v, new_v))
    FROM (
        SELECT 'product_name'        AS field, OLD.product_name        AS old_v, NEW.product_name        AS new_v WHERE OLD.product_name        IS NOT NEW.product_name
        UNION ALL SELECT 'unit_type',           OLD.unit_type,           NEW.unit_type           WHERE OLD.unit_type           IS NOT NEW.unit_type
        UNION ALL SELECT 'cost_price',          OLD.cost_price,          NEW.cost_price          WHERE OLD.cost_price          IS NOT NEW.cost_price
        UNION ALL SELECT 'base_sell_price',     OLD.base_sell_price,     NEW.base_sell_price     WHERE OLD.base_sell_price     IS NOT NEW.base_sell_price
        UNION ALL SELECT 'units_per_carton',    OLD.units_per_carton,    NEW.units_per_carton    WHERE OLD.units_per_carton    IS NOT NEW.units_per_carton
        UNION ALL SELECT 'units_per_box',       OLD.units_per_box,       NEW.units_per_box       WHERE OLD.units_per_box       IS NOT NEW.units_per_box
        UNION ALL SELECT 'low_stock_threshold', OLD.low_stock_threshold, NEW.low_stock_threshold WHERE OLD.low_stock_threshold IS NOT NEW.low_stock_threshold
        UNION ALL SELECT 'hard_to_sell',        OLD.hard_to_sell,        NEW.hard_to_sell        WHERE OLD.hard_to_sell        IS NOT NEW.hard_to_sell
        UNION ALL SELECT 'is_active',           OLD.is_active,           NEW.is_active           WHERE OLD.is_active           IS NOT NEW.is_active
        UNION ALL SELECT 'weight_kg',           OLD.weight_kg,           NEW.weight_kg           WHERE OLD.weight_kg           IS NOT NEW.weight_kg
        UNION ALL SELECT 'weight_source',       OLD.weight_source,       NEW.weight_source       WHERE OLD.weight_source       IS NOT NEW.weight_source
    );
END;

COMMIT;
