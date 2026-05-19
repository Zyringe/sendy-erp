-- 064_express_unit_normalize_enforced.rollback.sql
-- Manual rollback (the migration runner does not auto-rollback).
--
-- Drops the bsn_unit_alias snapshot table and de-registers the migration.
-- The historical express_sales.unit normalization and the brand_kind
-- recompute are NOT reversed: the normalized units and recomputed
-- brand_kind are the CORRECT values (raw acronyms were the defect), so
-- undoing them would re-introduce the corruption. This mirrors the 063
-- rollback's stance on its backfill.

BEGIN;

DROP TABLE IF EXISTS bsn_unit_alias;

DELETE FROM applied_migrations
 WHERE filename = '064_express_unit_normalize_enforced.sql';

COMMIT;
