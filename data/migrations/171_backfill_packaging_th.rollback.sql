-- Rollback 171 — return packaging_th to NULL for exactly the rows 171 filled.
-- Driven by the snapshot table, not by a predicate: a predicate like
-- "packaging_th IS NOT NULL AND packaging_short IS NOT NULL" would also blank
-- every row that legitimately had both before the migration ran.
PRAGMA busy_timeout=10000;

UPDATE products
   SET packaging_th = NULL
 WHERE id IN (SELECT product_id FROM mig171_packaging_th_backfill);

DROP TABLE IF EXISTS mig171_packaging_th_backfill;
