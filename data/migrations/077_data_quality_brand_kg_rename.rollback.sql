-- 077_data_quality_brand_kg_rename.rollback.sql
-- Restores prior (brand_id, unit_type) per pid from migration_077_snapshot,
-- then drops the 3 new third-party brand rows (FOUR STARS / BEYOND / ALTECO).
-- Run manually; the migration runner does not auto-rollback.

BEGIN;

-- 1. Restore brand_id from snapshot.
UPDATE products
SET brand_id = (
    SELECT prior_brand_id
    FROM migration_077_snapshot
    WHERE migration_077_snapshot.id = products.id
)
WHERE id IN (SELECT id FROM migration_077_snapshot);

-- 2. Restore unit_type from snapshot. (Only the 17 kg-rename pids actually
--    differ; the other ~97 rows have prior_unit_type == current, so this is
--    a no-op for them.)
UPDATE products
SET unit_type = (
    SELECT prior_unit_type
    FROM migration_077_snapshot
    WHERE migration_077_snapshot.id = products.id
)
WHERE id IN (SELECT id FROM migration_077_snapshot);

-- 3. Drop the 3 new brand rows. Must happen AFTER products.brand_id has been
--    restored to prior values (NULL or existing IDs) — the FK from
--    products.brand_id → brands.id would otherwise block the DELETE.
--    NOT EXISTS guard: skip the DELETE for any brand still referenced by a
--    product (e.g., if Put manually assigned another product to FOUR STARS
--    between forward-mig and rollback). Without this, the DELETE would
--    error out and leave brand_id UPDATEs from steps 1-2 already committed.
DELETE FROM brands
WHERE code IN ('four_stars', 'beyond', 'alteco')
  AND NOT EXISTS (SELECT 1 FROM products WHERE brand_id = brands.id);

-- 4. Snapshot table is no longer needed after rollback.
DROP TABLE IF EXISTS migration_077_snapshot;

COMMIT;
