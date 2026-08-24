-- 171 — backfill products.packaging_th from packaging_short.
--
-- WHY. `naming_cascade.save_product` rebuilds product_name from the structured
-- columns and UPDATEs it unconditionally. `packaging_th` is the column that puts
-- the "(แผง)" / "(ถุง)" suffix into the name; `packaging_short` is the column that
-- puts "-PN" / "-BG" into sku_code. 36 active products carry the short code but
-- NOT the Thai value, so their stored name has the suffix while a rebuild drops it
-- — opening one in the Master Naming workbench and saving any unrelated field
-- destroyed the suffix (the `(แบบหุล)` failure, generalised).
--
-- SCOPE, and why each clause is load-bearing:
--   packaging_short IS NOT NULL  the sku_code generator reads packaging_short FIRST and
--                                only falls back to packaging_th when it is NULL, so
--                                filling th here CANNOT move a generated sku_code.
--                                4 rows have BOTH columns NULL (BLT-JMB-SD-#520-*,
--                                all stock 0) and are deliberately EXCLUDED: filling
--                                th there WOULD move the generated code (issue #383).
--   packaging_th IS NULL         only fill a hole; never overwrite a curated value.
--   product_name LIKE '%(x)%'    only where the name ALREADY carries the word, so this
--                                can only make a name round-trip — never ADD text.
--                                Measured 2026-08-24: all 36 in scope satisfy it.
--
-- Rows touched are recorded so the rollback is exact rather than a guess.
PRAGMA busy_timeout=10000;

-- IF NOT EXISTS + OR IGNORE, deliberately NOT drop-first: a second run must be a
-- no-op that PRESERVES the record of the first. Dropping the table would make a
-- re-run forget what it filled, and the rollback would then silently restore
-- nothing — which is worse than the un-rerunnable form it was meant to avoid.
CREATE TABLE IF NOT EXISTS mig171_packaging_th_backfill (
    product_id INTEGER PRIMARY KEY REFERENCES products(id),
    packaging_th TEXT NOT NULL
);

INSERT OR IGNORE INTO mig171_packaging_th_backfill (product_id, packaging_th)
SELECT p.id, m.th
  FROM products p
  JOIN (SELECT 'UN' AS short, 'ตัว'       AS th UNION ALL
        SELECT 'PN', 'แผง'      UNION ALL
        SELECT 'BG', 'ถุง'       UNION ALL
        SELECT 'SC', 'ซอง'      UNION ALL
        SELECT 'PK', 'แพ็ค'     UNION ALL
        SELECT 'DZ', 'โหล'      UNION ALL
        SELECT 'HP', 'แพ็คหัว'   UNION ALL
        SELECT 'PP', 'แพ็คถุง'   UNION ALL
        SELECT 'TB', 'แบบหลอด'  UNION ALL
        SELECT 'SP', 'อัดแผง'    UNION ALL
        SELECT 'C60', '1กลมี60ใบ') m ON m.short = p.packaging_short
 WHERE p.packaging_th IS NULL
   AND p.product_name LIKE '%(' || m.th || ')%';

UPDATE products
   SET packaging_th = (SELECT b.packaging_th FROM mig171_packaging_th_backfill b
                        WHERE b.product_id = products.id)
 WHERE id IN (SELECT product_id FROM mig171_packaging_th_backfill);
