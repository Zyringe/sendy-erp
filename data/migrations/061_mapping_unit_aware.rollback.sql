-- 061_mapping_unit_aware.rollback.sql
-- Manual rollback (the migration runner does not auto-rollback).
--
-- Collapses the composite UNIQUE(bsn_code, bsn_unit) back to a single
-- bsn_code UNIQUE. Only the catch-all rows (bsn_unit='') survive — any
-- per-unit OVERRIDE rows are INTENTIONALLY DROPPED here, because two rows
-- sharing a bsn_code cannot coexist under a single-column UNIQUE. This data
-- loss is accepted and documented for a manual rollback.

BEGIN;

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

DELETE FROM applied_migrations WHERE filename = '061_mapping_unit_aware.sql';

COMMIT;
