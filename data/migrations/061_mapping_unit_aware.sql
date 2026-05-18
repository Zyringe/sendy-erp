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

BEGIN;

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

COMMIT;
