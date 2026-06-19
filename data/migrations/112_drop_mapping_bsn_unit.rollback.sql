-- Rollback 112: restore product_code_mapping.bsn_unit + UNIQUE(bsn_code, bsn_unit)
-- Rebuilds from the CURRENT table (post-112 rows) so inserts after the forward
-- migration are preserved (per rename-rollback rule).

PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE product_code_mapping_rollback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bsn_code TEXT NOT NULL,
  bsn_name TEXT NOT NULL,
  product_id INTEGER REFERENCES products(id),
  is_ignored INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  bsn_unit TEXT NOT NULL DEFAULT '',
  ignore_reason TEXT,
  UNIQUE(bsn_code, bsn_unit)
);
INSERT INTO product_code_mapping_rollback
    (bsn_code, bsn_name, product_id, is_ignored, created_at, bsn_unit, ignore_reason)
SELECT bsn_code, bsn_name, product_id, is_ignored, created_at, '' AS bsn_unit, ignore_reason
FROM product_code_mapping;
DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_rollback RENAME TO product_code_mapping;
DELETE FROM applied_migrations WHERE filename = '112_drop_mapping_bsn_unit.sql';
COMMIT;
PRAGMA foreign_keys=ON;
