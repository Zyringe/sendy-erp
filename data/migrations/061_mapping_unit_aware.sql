-- 061_mapping_unit_aware.sql
-- Unit-aware BSN code → product mapping.
--
-- Design rationale:
--   product_code_mapping mapped ONE bsn_code → ONE product. But the catalog
--   deliberately splits ตัว vs แผง (and similar) as SEPARATE SKUs, so the
--   SAME BSN code sold in a different unit must route to a DIFFERENT product
--   (e.g. 030บ3043 sold "แผง" → the แผง SKU, sold "ตัว" → the ตัว SKU).
--
--   Solution: key the mapping by (bsn_code, bsn_unit). bsn_unit='' is the
--   CATCH-ALL row = exactly today's behavior (used when no unit-specific
--   override exists). The feature is opt-in: this migration backfills every
--   existing row to bsn_unit='' so there is ZERO behavior change until a
--   per-unit override row is explicitly added.
--
--   SQLite cannot ALTER a UNIQUE constraint, so the table is rebuilt
--   (CREATE _new → copy → DROP → RENAME). Nothing FK-references
--   product_code_mapping (it only references products), so the swap is safe.
--
-- Original product_code_mapping DDL (verified via sqlite_master 2026-05-19):
--   CREATE TABLE product_code_mapping (
--       id          INTEGER PRIMARY KEY AUTOINCREMENT,
--       bsn_code    TEXT UNIQUE NOT NULL,
--       bsn_name    TEXT NOT NULL,
--       product_id  INTEGER REFERENCES products(id),
--       is_ignored  INTEGER NOT NULL DEFAULT 0,
--       created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
--     , ignore_reason TEXT)
--   Only index: implicit sqlite_autoindex from the bsn_code UNIQUE.
--   The new composite UNIQUE(bsn_code, bsn_unit) autoindex serves
--   resolution lookups — no extra explicit index needed.
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
--   every migration it executes automatically.
--
-- TRIGGER HAZARD (fixed 2026-05-19): trigger refresh_brand_kind_on_product_brand_change
--   (migration 021, AFTER UPDATE OF brand_id ON products) has a body that
--   references product_code_mapping. SQLite >=3.25 re-validates every trigger
--   body during DROP TABLE / ALTER TABLE RENAME, so the table swap below aborts
--   with "no such table: main.product_code_mapping" on any DB where 021 already
--   ran (= Railway's volume DB running 061 fresh; local never re-runs 061 since
--   it is already in applied_migrations). Fix: drop the trigger before the swap
--   and recreate it verbatim (from 021) after. DROP ... IF EXISTS keeps this
--   idempotent and safe against the crash-loop-left state.

BEGIN;

DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;
DROP TABLE   IF EXISTS product_code_mapping_new;

CREATE TABLE product_code_mapping_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bsn_code      TEXT    NOT NULL,
    bsn_name      TEXT    NOT NULL,
    product_id    INTEGER REFERENCES products(id),
    is_ignored    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    bsn_unit      TEXT    NOT NULL DEFAULT '',
    ignore_reason TEXT,
    UNIQUE(bsn_code, bsn_unit)
);

-- explicit named columns: 037 appended ignore_reason via ADD COLUMN, so
-- column order differs between a fresh-schema DB and a 037-migrated DB —
-- never SELECT *. Every existing row becomes the bsn_unit='' catch-all.
INSERT INTO product_code_mapping_new
    (id, bsn_code, bsn_name, product_id, is_ignored, created_at,
     bsn_unit, ignore_reason)
SELECT
     id, bsn_code, bsn_name, product_id, is_ignored, created_at,
     '', ignore_reason
FROM product_code_mapping;

DROP TABLE product_code_mapping;
ALTER TABLE product_code_mapping_new RENAME TO product_code_mapping;

-- Recreate the trigger verbatim from 021_brand_kind_trigger.sql now that
-- product_code_mapping exists again (post-RENAME).
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

COMMIT;
