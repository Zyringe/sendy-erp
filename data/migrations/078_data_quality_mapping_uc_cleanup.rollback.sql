-- 078_data_quality_mapping_uc_cleanup.rollback.sql
-- Restores prior state from the 3 snapshot tables, then drops them.
-- Run manually; the migration runner does not auto-rollback.

BEGIN;

-- 1. Restore products.unit_type
UPDATE products
SET unit_type = (
    SELECT prior_unit_type FROM migration_078_snapshot_products
    WHERE migration_078_snapshot_products.id = products.id
)
WHERE id IN (SELECT id FROM migration_078_snapshot_products);

-- 2. Restore unit_conversions: re-INSERT any deleted rows + restore updated ratios.
-- INSERT OR IGNORE for rows that already exist (UPDATEs); ratios then reset below.
INSERT OR IGNORE INTO unit_conversions (product_id, bsn_unit, ratio)
SELECT product_id, bsn_unit, prior_ratio FROM migration_078_snapshot_uc;

UPDATE unit_conversions
SET ratio = (
    SELECT prior_ratio FROM migration_078_snapshot_uc s
    WHERE s.product_id = unit_conversions.product_id
      AND s.bsn_unit  = unit_conversions.bsn_unit
)
WHERE (product_id, bsn_unit) IN (
    SELECT product_id, bsn_unit FROM migration_078_snapshot_uc
);

-- 3. Restore product_code_mapping rows that were UPDATEd. (DELETEd rows are
-- restored by the re-INSERT below.)
UPDATE product_code_mapping
SET bsn_unit   = (SELECT prior_bsn_unit   FROM migration_078_snapshot_mapping s WHERE s.id = product_code_mapping.id),
    product_id = (SELECT prior_product_id FROM migration_078_snapshot_mapping s WHERE s.id = product_code_mapping.id)
WHERE id IN (SELECT id FROM migration_078_snapshot_mapping);

-- Re-INSERT the 2 DELETEd rows (id 59, id 150) if they're missing.
INSERT OR IGNORE INTO product_code_mapping
    (id, bsn_code, bsn_name, product_id, bsn_unit)
SELECT id, prior_bsn_code, prior_bsn_name, prior_product_id, prior_bsn_unit
FROM migration_078_snapshot_mapping
WHERE id IN (59, 150);

-- 4. Reverse pid 771 stock reconciliation entry from section 5 of the forward
--    mig. As of mig 080 (2026-05-25, `after_transaction_delete` trigger),
--    DELETE FROM transactions automatically decrements stock_levels by
--    OLD.quantity_change. So this rollback now just DELETEs and lets the
--    business trigger handle the -44 reversal.
--    (Before mig 080 this block hand-decremented stock_levels FIRST; that
--    manual UPDATE has been removed to avoid double-counting.)
DELETE FROM transactions WHERE product_id = 771 AND reference_no = 'MIG_078';

-- 5. Drop snapshot tables.
DROP TABLE IF EXISTS migration_078_snapshot_products;
DROP TABLE IF EXISTS migration_078_snapshot_uc;
DROP TABLE IF EXISTS migration_078_snapshot_mapping;

COMMIT;
