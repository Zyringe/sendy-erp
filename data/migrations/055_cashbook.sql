-- 055_cashbook.sql
-- Cashbook module (สมุดบัญชีรายรับ-รายจ่าย) — a multi-account personal/
-- family operating cashbook, SEPARATE from the BSN VAT books
-- (sales_transactions / purchase_transactions).
--
-- Models two source workbooks (NoVat + Vat, same structure) discriminated
-- by vat_flag. Six accounts (392, LEX, SPX, ชฎามาศ, กิติยา, 904) each with
-- bank metadata; per-account transaction sheets carry:
--   วันที่ · ประเภท(รายรับ/รายจ่าย) · หมวดหมู่ · หมวดหมู่_ผู้ใช้ ·
--   จำนวนเงิน · รายละเอียด · หมายเหตุ
-- A salary_advances ledger (เบิกเงินล่วงหน้า) feeds HR payroll deductions
-- later via deducted_in_run_id → payroll_runs(id).
--
-- No seed rows: the importer creates accounts and upserts categories.
--
-- Dates are stored ISO/Gregorian (yyyy-mm-dd) internally; พ.ศ. conversion
-- is a display-layer concern only.
--
-- Apply:    sqlite3 .../inventory.db < .../migrations/055_cashbook.sql
--           (in practice the runner applies it: database.py::run_pending_migrations)
-- Rollback: 055_cashbook.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner
-- (database.py::run_pending_migrations) records every migration it
-- executes; a self-insert would duplicate-key crash on boot.
--
-- NOTE: employees.nickname already exists (added by 054_hr_module.sql,
-- verified via PRAGMA table_info(employees)); therefore this migration
-- adds NOTHING to the employees table. The HR-sync step will populate it.
--
-- created_by / owner-name columns are plain TEXT (NOT a users(id) FK —
-- that FK-to-users bug was fixed in 054 and is not repeated here).

BEGIN;

-- ── cashbook_accounts ─────────────────────────────────────────────────────
-- One row per cashbook account (bank/wallet). Importer creates these.
CREATE TABLE cashbook_accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    UNIQUE NOT NULL,   -- '392','LEX','SPX','ชฎามาศ','กิติยา','904'
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

CREATE INDEX idx_cashbook_accounts_code ON cashbook_accounts(code);

-- ── cashbook_categories ───────────────────────────────────────────────────
-- Reference list for dropdowns/reporting. Importer upserts on (name,direction).
CREATE TABLE cashbook_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK(direction IN ('income','expense')),
    source      TEXT    CHECK(source IN ('setup','imported') OR source IS NULL),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    sort_order  INTEGER NOT NULL DEFAULT 100,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(name, direction)
);

-- ── cashbook_transactions ─────────────────────────────────────────────────
-- The ledger. Importer full-replaces per (vat_flag, account_id) pair.
CREATE TABLE cashbook_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES cashbook_accounts(id),
    txn_date        TEXT    NOT NULL,             -- ISO yyyy-mm-dd
    direction       TEXT    NOT NULL CHECK(direction IN ('income','expense')),
    category        TEXT,
    user_category   TEXT,
    amount          REAL    NOT NULL,
    description     TEXT,
    note            TEXT,
    vat_flag        TEXT    NOT NULL CHECK(vat_flag IN ('novat','vat')),
    source_file     TEXT,
    source_sheet    TEXT,
    source_row      INTEGER,
    import_batch_id TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_cashbook_txn_account_date ON cashbook_transactions(account_id, txn_date);
CREATE INDEX idx_cashbook_txn_vat_account  ON cashbook_transactions(vat_flag, account_id);
CREATE INDEX idx_cashbook_txn_date         ON cashbook_transactions(txn_date);
CREATE INDEX idx_cashbook_txn_category     ON cashbook_transactions(category);

-- ── salary_advances ───────────────────────────────────────────────────────
-- เบิกเงินล่วงหน้า — feeds HR payroll deduction later. employee_id is
-- nullable (may be unmatched at import); raw_name keeps the sheet name/
-- nickname for later matching. deducted_in_run_id set when applied to a run.
CREATE TABLE salary_advances (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER REFERENCES employees(id),
    advance_date       TEXT    NOT NULL,
    amount             REAL    NOT NULL,
    raw_name           TEXT,                       -- e.g. 'บอล','หลุย','ริน'
    note               TEXT,
    deducted_in_run_id INTEGER REFERENCES payroll_runs(id),
    vat_flag           TEXT    CHECK(vat_flag IN ('novat','vat') OR vat_flag IS NULL),
    source_file        TEXT,
    import_batch_id    TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_salary_advances_emp ON salary_advances(employee_id);
CREATE INDEX idx_salary_advances_run ON salary_advances(deducted_in_run_id);

COMMIT;
