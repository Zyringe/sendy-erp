-- Rollback 171 — return packaging_th to NULL for exactly the rows 171 filled.
-- Driven by the snapshot table, not by a predicate: a predicate like
-- "packaging_th IS NOT NULL AND packaging_short IS NOT NULL" would also blank
-- every row that legitimately had both before the migration ran.
PRAGMA busy_timeout=10000;

BEGIN;

-- ⚠ The value guard is load-bearing, not belt-and-braces. Keying on product_id ALONE
-- would blank a row an operator has since CURATED through /naming: forward sets 'แผง',
-- someone corrects it to 'ถุง', rollback then destroys 'ถุง' and does not even restore
-- 'แผง'. Only undo rows that still hold exactly what this migration put there.
UPDATE products
   SET packaging_th = NULL
 WHERE id IN (SELECT product_id FROM mig171_packaging_th_backfill)
   AND packaging_th = (SELECT b.packaging_th FROM mig171_packaging_th_backfill b
                        WHERE b.product_id = products.id);

DROP TABLE IF EXISTS mig171_packaging_th_backfill;

-- Without this the runner would skip 171 forever: the data is reverted but the
-- filename stays in applied_migrations, so re-applying is impossible. Migration
-- 170's rollback does the same.
DELETE FROM applied_migrations WHERE filename = '171_backfill_packaging_th.sql';

COMMIT;
