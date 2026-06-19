-- Migration 112: drop product_code_mapping.bsn_unit — pure bsn_code→product_id resolver
-- Precondition: 0 split-overrides (every bsn_code maps to exactly one product).
-- Collapses the 19 redundant non-blank rows to one row per bsn_code, keeping
-- the best (mapped > pending, non-ignored > ignored, lowest id tiebreak).

PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE product_code_mapping_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bsn_code TEXT NOT NULL,
  bsn_name TEXT NOT NULL,
  product_id INTEGER REFERENCES products(id),
  is_ignored INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ignore_reason TEXT,
  UNIQUE(bsn_code)
);
-- one row per bsn_code: pick the best (mapped > pending, non-ignored kept, lowest id tiebreak)
INSERT INTO product_code_mapping_new (bsn_code,bsn_name,product_id,is_ignored,created_at,ignore_reason)
SELECT bsn_code, bsn_name, product_id, is_ignored, created_at, ignore_reason
FROM product_code_mapping m
WHERE m.id = (
  SELECT x.id FROM product_code_mapping x WHERE x.bsn_code = m.bsn_code
  ORDER BY (x.product_id IS NULL), x.is_ignored, x.id  LIMIT 1
);
DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_new RENAME TO product_code_mapping;
COMMIT;
PRAGMA foreign_keys=ON;
