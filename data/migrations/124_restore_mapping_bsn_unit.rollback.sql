-- Rollback 124: drop bsn_unit, collapse back to UNIQUE(bsn_code) (mig-112 shape)
--
-- Rebuilds from the CURRENT table (post-124 rows, incl. any split rows added
-- after this migration ran) so nothing inserted later is lost. Collapses to
-- one row per bsn_code using the SAME tiebreak mig 112 used: mapped > pending,
-- non-ignored > ignored, lowest id.

PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE product_code_mapping_rollback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bsn_code TEXT NOT NULL,
  bsn_name TEXT NOT NULL,
  product_id INTEGER REFERENCES products(id),
  is_ignored INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ignore_reason TEXT,
  UNIQUE(bsn_code)
);
INSERT INTO product_code_mapping_rollback
    (bsn_code, bsn_name, product_id, is_ignored, created_at, ignore_reason)
SELECT bsn_code, bsn_name, product_id, is_ignored, created_at, ignore_reason
FROM product_code_mapping m
WHERE m.id = (
  SELECT x.id FROM product_code_mapping x WHERE x.bsn_code = m.bsn_code
  ORDER BY (x.product_id IS NULL), x.is_ignored, x.id LIMIT 1
);
DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_rollback RENAME TO product_code_mapping;
DELETE FROM applied_migrations WHERE filename = '124_restore_mapping_bsn_unit.sql';
COMMIT;
PRAGMA foreign_keys=ON;
