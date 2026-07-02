-- Migration 124: restore product_code_mapping.bsn_unit — unit-aware resolver
--
-- Mig 112 (2026-06-09) dropped bsn_unit because nothing used it. Put's
-- 2026-07-02 pack/loose hinge-SKU split needs it back (see
-- projects/pack-loose-sku-split/plan.md, Phase 1): the same BSN code can bill
-- in two units (แผง/ตัว) that must resolve to two DIFFERENT products.
--
-- This is a forward migration, NOT mig 112's rollback (do not run that — it
-- DELETEs the 112 row from applied_migrations, which would desync prod).
-- Rebuilds the table from the CURRENT rows (1:1, ids preserved, no dedup
-- needed since mig 112 already collapsed to one row per bsn_code): every
-- existing row becomes the non-split catch-all (bsn_unit='').
--
-- Triggers/indexes on product_code_mapping (verified via sqlite_master on a
-- live-DB copy, 2026-07-02): only the UNIQUE constraint's auto-index exists —
-- no named indexes, no triggers. Nothing else to recreate.

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
  bsn_unit TEXT NOT NULL DEFAULT '',
  UNIQUE(bsn_code, bsn_unit)
);
INSERT INTO product_code_mapping_new
    (id, bsn_code, bsn_name, product_id, is_ignored, created_at, ignore_reason, bsn_unit)
SELECT id, bsn_code, bsn_name, product_id, is_ignored, created_at, ignore_reason, ''
FROM product_code_mapping;
DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_new RENAME TO product_code_mapping;
COMMIT;
PRAGMA foreign_keys=ON;
