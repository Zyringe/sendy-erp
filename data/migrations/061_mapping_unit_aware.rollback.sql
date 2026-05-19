-- 061_mapping_unit_aware.rollback.sql
-- Manual rollback (the migration runner does not auto-rollback).
--
-- Collapses the composite UNIQUE(bsn_code, bsn_unit) back to a single
-- bsn_code UNIQUE. Only the catch-all rows (bsn_unit='') survive — any
-- per-unit OVERRIDE rows are INTENTIONALLY DROPPED here, because two rows
-- sharing a bsn_code cannot coexist under a single-column UNIQUE. This data
-- loss is accepted and documented for a manual rollback.
--
-- TRIGGER HAZARD (fixed 2026-05-20, Codex adversarial review medium finding):
--   refresh_brand_kind_on_product_brand_change references product_code_mapping.
--   SQLite >=3.25 re-validates trigger bodies during DROP TABLE / ALTER TABLE
--   RENAME, so the table swap below aborts with "no such table:
--   main.product_code_mapping" on any DB where migration 021 ran (the same
--   failure the 061 forward migration was hot-fixed for). Fix: drop the
--   trigger before the swap and recreate the pre-061 by-code-only trigger
--   after the RENAME. DROP ... IF EXISTS keeps this idempotent.
--   If migration 063 was applied, run 063's rollback FIRST (it restores this
--   same pre-061 trigger); running this alone is still safe and idempotent.

BEGIN;

DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;

CREATE TABLE product_code_mapping_old (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bsn_code      TEXT    UNIQUE NOT NULL,
    bsn_name      TEXT    NOT NULL,
    product_id    INTEGER REFERENCES products(id),
    is_ignored    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    ignore_reason TEXT
);

-- catch-all rows only (bsn_unit=''); override rows dropped (see header)
INSERT INTO product_code_mapping_old
    (id, bsn_code, bsn_name, product_id, is_ignored, created_at,
     ignore_reason)
SELECT
     id, bsn_code, bsn_name, product_id, is_ignored, created_at,
     ignore_reason
FROM product_code_mapping
WHERE bsn_unit = '';

DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_old RENAME TO product_code_mapping;

-- Recreate the pre-061 by-product_code-only trigger (verbatim from 021) now
-- that product_code_mapping exists again (post-RENAME).
CREATE TRIGGER refresh_brand_kind_on_product_brand_change
AFTER UPDATE OF brand_id ON products
WHEN OLD.brand_id IS NOT NEW.brand_id
BEGIN
    UPDATE express_sales
       SET brand_kind = (
           SELECT CASE WHEN b.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
             FROM brands b WHERE b.id = NEW.brand_id
       )
     WHERE product_code IN (
         SELECT bsn_code FROM product_code_mapping WHERE product_id = NEW.id
     );
END;

DELETE FROM applied_migrations WHERE filename = '061_mapping_unit_aware.sql';

COMMIT;
