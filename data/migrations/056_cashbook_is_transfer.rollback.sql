-- 056_cashbook_is_transfer.rollback.sql
-- SQLite does not support DROP COLUMN in all versions (requires 3.35+).
-- Safe rollback: recreate cashbook_accounts without is_transfer, copying data.
-- Run this manually when needed; the migration runner does not auto-rollback.

BEGIN;

-- Step 1: recreate table without is_transfer
CREATE TABLE cashbook_accounts_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    UNIQUE NOT NULL,
    display_name       TEXT,
    bank_name          TEXT,
    bank_account_no    TEXT,
    account_owner_name TEXT,
    note               TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    sort_order         INTEGER NOT NULL DEFAULT 100,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Step 2: copy data (drop is_transfer column)
INSERT INTO cashbook_accounts_new
    (id, code, display_name, bank_name, bank_account_no,
     account_owner_name, note, is_active, sort_order, created_at, updated_at)
SELECT id, code, display_name, bank_name, bank_account_no,
       account_owner_name, note, is_active, sort_order, created_at, updated_at
FROM cashbook_accounts;

-- Step 3: swap
DROP TABLE cashbook_accounts;
ALTER TABLE cashbook_accounts_new RENAME TO cashbook_accounts;

-- Step 4: recreate index
CREATE INDEX IF NOT EXISTS idx_cashbook_accounts_code ON cashbook_accounts(code);

-- Step 5: record rollback
DELETE FROM applied_migrations WHERE filename = '056_cashbook_is_transfer.sql';

COMMIT;
