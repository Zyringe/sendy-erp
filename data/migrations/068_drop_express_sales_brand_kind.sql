-- ============================================================================
-- Migration 068 — drop the express_sales.brand_kind write-only cache.
--
-- After PR #33 (commit 98442df), every commission read path derives brand_kind
-- at read time from brands.is_own_brand via a CASE expression in
-- commission._BASE_QUERY and commission.get_invoice_line_breakdown. The cache
-- column was no longer read by any production code path, but was still
-- maintained by an INSERT trigger and a write-side recompute in import
-- scripts. This migration removes the column, the trigger that kept it
-- fresh, and the supporting index.
--
-- Pre-requisites already shipped on the fix/drop-express-brand-kind-cache
-- branch BEFORE this mig:
--   - scripts/import_express.py no longer writes brand_kind on INSERT.
--   - scripts/load_brand_map.py deleted (sole purpose was cache refresh).
--   - scripts/backfill_express_unit_normalize.py no longer recomputes;
--     function renamed normalize_and_recompute → normalize_units.
--   - scripts/isolate_issue30_impact.py deleted (issue #30 closed).
--   - inventory_app/commission.py + models.py + templates: comment hygiene.
--   - Tests: deleted test_migration_063_brand_kind_unit_aware.py entirely,
--     trimmed 4 cache-contract tests in test_commission_unit_aware.py,
--     trimmed 2 trigger-preservation tests in test_migration_061_*.py.
--
-- Closes #34.
--
-- Forward-only. The rollback file (068_drop_express_sales_brand_kind.rollback.sql)
-- restores the column + index DDL but does NOT repopulate cached values OR
-- recreate the unit-aware refresh trigger (which is non-trivial to inline
-- — see mig 063 for the full DDL if needed). The cache is provably
-- redundant post-PR #33, so restoring it would only re-introduce the
-- bug class this migration eliminates.
-- ============================================================================

BEGIN;

-- The trigger created by mig 063 (which replaced any mig 021-era version
-- with the same name). DROP TRIGGER IF EXISTS is idempotent and forward-safe.
DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;

-- SQLite requires the index to be dropped before ALTER TABLE DROP COLUMN
-- (the column is referenced by an index → can't be dropped while indexed).
DROP INDEX   IF EXISTS idx_express_sales_brandkind;

-- The column itself. Requires SQLite ≥ 3.35.0 (Mar 2021). Local dev: 3.51.0;
-- Railway runs Python 3.9 via Nixpacks on a Debian base — SQLite ≥ 3.40 in
-- practice. If the runtime SQLite is too old this statement fails loudly
-- and the migration runner halts — acceptable smoke failure.
ALTER TABLE express_sales DROP COLUMN brand_kind;

COMMIT;
