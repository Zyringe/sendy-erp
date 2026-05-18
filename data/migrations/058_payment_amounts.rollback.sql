-- 058_payment_amounts.rollback.sql
-- SQLite does not support DROP COLUMN in all versions (requires 3.35+).
-- Safe rollback: recreate paid_invoices / received_payments without the new
-- amount / total columns, copying data. Run this manually when needed; the
-- migration runner does not auto-rollback.

BEGIN;

-- ===========================================================================
-- paid_invoices : drop `amount`
-- Original DDL (verified via sqlite_master 2026-05-18):
--   CREATE TABLE paid_invoices (
--       id     INTEGER PRIMARY KEY AUTOINCREMENT,
--       re_id  INTEGER NOT NULL REFERENCES received_payments(id),
--       iv_no  TEXT    NOT NULL,
--       UNIQUE(re_id, iv_no)
--   )
-- Indexes: sqlite_autoindex_paid_invoices_1 (implicit, from UNIQUE),
--          idx_pi_iv_no ON paid_invoices(iv_no)  (explicit)
-- ===========================================================================

-- Step 1: recreate table without `amount`
CREATE TABLE paid_invoices_new (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    re_id  INTEGER NOT NULL REFERENCES received_payments(id),
    iv_no  TEXT    NOT NULL,
    UNIQUE(re_id, iv_no)
);

-- Step 2: copy data (drop `amount` column)
INSERT INTO paid_invoices_new (id, re_id, iv_no)
SELECT id, re_id, iv_no
FROM paid_invoices;

-- Step 3: swap
DROP TABLE paid_invoices;
ALTER TABLE paid_invoices_new RENAME TO paid_invoices;

-- Step 4: recreate explicit indexes (UNIQUE autoindex is recreated implicitly)
CREATE INDEX IF NOT EXISTS idx_pi_iv_no ON paid_invoices(iv_no);

-- ===========================================================================
-- received_payments : drop `total`
-- Original DDL (verified via sqlite_master 2026-05-18):
--   CREATE TABLE received_payments (
--       id           INTEGER PRIMARY KEY AUTOINCREMENT,
--       re_no        TEXT    NOT NULL UNIQUE,
--       date_iso     TEXT    NOT NULL,
--       customer     TEXT    NOT NULL,
--       salesperson  TEXT,
--       cancelled    INTEGER NOT NULL DEFAULT 0,
--       imported_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
--   )
-- Indexes: sqlite_autoindex_received_payments_1 (implicit, from re_no UNIQUE).
--          No explicit indexes.
-- NOTE: paid_invoices.re_id REFERENCES received_payments(id). PRAGMA
-- foreign_keys is OFF in the running app, and the recreate keeps id values
-- identical, so existing links remain valid after the swap.
-- ===========================================================================

-- Step 1: recreate table without `total`
CREATE TABLE received_payments_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    re_no        TEXT    NOT NULL UNIQUE,
    date_iso     TEXT    NOT NULL,
    customer     TEXT    NOT NULL,
    salesperson  TEXT,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Step 2: copy data (drop `total` column)
INSERT INTO received_payments_new
    (id, re_no, date_iso, customer, salesperson, cancelled, imported_at)
SELECT id, re_no, date_iso, customer, salesperson, cancelled, imported_at
FROM received_payments;

-- Step 3: swap
DROP TABLE received_payments;
ALTER TABLE received_payments_new RENAME TO received_payments;

-- (received_payments has no explicit indexes; the re_no UNIQUE autoindex is
--  recreated implicitly by the CREATE TABLE above.)

-- Step 4: record rollback
DELETE FROM applied_migrations WHERE filename = '058_payment_amounts.sql';

COMMIT;
