-- ============================================================================
-- Rollback for mig 068.
--
-- Restores:
--   - express_sales.brand_kind column (TEXT, NULL-allowed, CHECK constraint)
--   - idx_express_sales_brandkind index
--
-- Does NOT restore:
--   - The refresh_brand_kind_on_product_brand_change trigger (mig 063).
--     If you genuinely need it back, copy the trigger DDL from
--     data/migrations/063_brand_kind_unit_aware_trigger.sql and apply
--     separately AFTER this rollback.
--   - The cached brand_kind values. Run the pre-mig-068 version of
--     scripts/backfill_express_unit_normalize.py to repopulate
--     (or accept brand_kind = NULL on all rows; commission queries
--     derive at read time and don't depend on the column either way).
--
-- Caveat: rolling back is rarely useful — PR #33 already shipped the
-- read-time CASE derive that makes the cache redundant. Restoring this
-- column without the trigger means the column is permanently NULL and
-- provides zero behavioural value. Prefer NOT rolling back; instead
-- write a NEW forward migration that addresses whatever motivated the
-- rollback.
-- ============================================================================

BEGIN;

ALTER TABLE express_sales ADD COLUMN brand_kind TEXT
    CHECK(brand_kind IS NULL OR brand_kind IN ('own', 'third_party'));

CREATE INDEX IF NOT EXISTS idx_express_sales_brandkind
    ON express_sales(brand_kind);

COMMIT;
