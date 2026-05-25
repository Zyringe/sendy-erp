-- ============================================================================
-- Rollback for 086 — restore `promotions` table to its pre-086 shape:
--   * 9 columns (drop the 7 bundle/gift columns)
--   * promo_type CHECK back to ('percent','fixed') only
--   * discount_value back to NOT NULL
--   * Drop the 3 audit triggers + 2 indexes
--   * Drop the ON DELETE CASCADE on product_id
--
-- SAFETY ABORT
--   If ANY row exists with promo_type ∈ ('bundle','mixed','gift'), this
--   rollback ABORTs without changing the schema. Those rows cannot be
--   represented in the pre-086 schema (their type would violate the original
--   CHECK), and dropping them silently would be data loss.
--
--   Operator workflow if the abort fires:
--     1) Export the offending rows:
--          sqlite3 inventory.db ".mode csv" \
--            "SELECT * FROM promotions WHERE promo_type IN ('bundle','mixed','gift')" \
--            > /tmp/promotions_to_keep.csv
--     2) Decide manually: keep as 'percent'-equivalent, drop, or migrate to
--        a parallel storage (e.g. notes / external CSV).
--     3) DELETE/UPDATE the offending rows in the live `promotions` table.
--     4) Re-run this rollback file.
--
-- Deletes the applied_migrations row for 086 so the runner will re-apply on
-- next boot if desired.
--
-- FK hazard: same recipe as forward — PRAGMA foreign_keys = OFF outside the
-- transaction.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- SAFETY ABORT — refuse rollback if any extended-type rows exist.
-- RAISE(ABORT, ...) only works inside triggers, so we use a temp-table CHECK:
-- if COUNT > 0, the INSERT fails with `CHECK constraint failed:
-- _mig086_rollback_safety` and the surrounding transaction rolls back.
-- Schema stays in post-086 state. See file header for export recovery.
CREATE TEMP TABLE _mig086_rollback_safety (
    extended_row_count INTEGER NOT NULL CHECK (extended_row_count = 0)
);
INSERT INTO _mig086_rollback_safety
    SELECT COUNT(*) FROM promotions WHERE promo_type IN ('bundle','mixed','gift');
DROP TABLE _mig086_rollback_safety;

-- Drop the audit triggers + indexes (added by 086).
DROP TRIGGER IF EXISTS audit_promotions_insert;
DROP TRIGGER IF EXISTS audit_promotions_update;
DROP TRIGGER IF EXISTS audit_promotions_delete;
DROP INDEX   IF EXISTS idx_promotions_product;
DROP INDEX   IF EXISTS idx_promotions_active;

DROP TABLE IF EXISTS promotions_old;

-- Recreate table with the PRE-086 shape: 9 columns, original CHECK,
-- discount_value NOT NULL, no ON DELETE CASCADE on product_id.
CREATE TABLE promotions_old (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    promo_name      TEXT    NOT NULL,
    promo_type      TEXT    NOT NULL CHECK(promo_type IN ('percent','fixed')),
    discount_value  REAL    NOT NULL,
    date_start      TEXT,
    date_end        TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Copy data with explicit column list. Only the 9 original columns are kept;
-- bundle_* / gift_* columns are dropped. The abort guard above guarantees
-- no extended-type rows exist, so every remaining row is percent/fixed and
-- has a non-NULL discount_value (the original schema's NOT NULL contract).
INSERT INTO promotions_old
    (id, product_id, promo_name, promo_type, discount_value,
     date_start, date_end, is_active, created_at)
SELECT
     id, product_id, promo_name, promo_type, discount_value,
     date_start, date_end, is_active, created_at
FROM promotions;

DROP TABLE promotions;
ALTER TABLE promotions_old RENAME TO promotions;

-- Delete the applied_migrations row so the runner picks up 086 again if
-- the .sql file is replayed.
DELETE FROM applied_migrations WHERE filename = '086_promotions_extend_for_bundle_gift.sql';

COMMIT;

PRAGMA foreign_keys = ON;
