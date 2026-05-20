-- 067_drop_cashbook_vat_flag.sql
-- Drops vat_flag column + related CHECK constraint and index from
-- cashbook_transactions and salary_advances.
--
-- Rationale: when the cashbook module shipped (mig 055) the design assumed
-- a separate "Vat" workbook would be imported alongside the NoVat one.  In
-- practice no such Vat workbook exists — the actual VAT bookkeeping lives
-- in BSN's sales_transactions / purchase_transactions (with vat_type ×1.07).
-- NoVat_Account.xlsx is a personal/family cash-tracking sheet only, so the
-- vat_flag column has been dead weight (278/278 rows = 'novat'; 7/7 salary
-- advances = 'novat').
--
-- Table rebuild needed because the CHECK constraint references vat_flag.
-- No triggers/views touch these tables (verified via sqlite_master scan);
-- only indexes need recreating.
--
-- Apply:    via database.py::run_pending_migrations (automatic on boot)
-- Rollback: 067_drop_cashbook_vat_flag.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here.

BEGIN;

-- ── cashbook_transactions ────────────────────────────────────────────────
CREATE TABLE cashbook_transactions_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES cashbook_accounts(id),
    txn_date        TEXT    NOT NULL,
    direction       TEXT    NOT NULL CHECK(direction IN ('income','expense')),
    category        TEXT,
    user_category   TEXT,
    amount          REAL    NOT NULL,
    description     TEXT,
    note            TEXT,
    source_file     TEXT,
    source_sheet    TEXT,
    source_row      INTEGER,
    import_batch_id TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO cashbook_transactions_new
    (id, account_id, txn_date, direction, category, user_category,
     amount, description, note, source_file, source_sheet, source_row,
     import_batch_id, created_at)
SELECT id, account_id, txn_date, direction, category, user_category,
       amount, description, note, source_file, source_sheet, source_row,
       import_batch_id, created_at
FROM cashbook_transactions;

DROP TABLE cashbook_transactions;
ALTER TABLE cashbook_transactions_new RENAME TO cashbook_transactions;

CREATE INDEX idx_cashbook_txn_account_date ON cashbook_transactions(account_id, txn_date);
CREATE INDEX idx_cashbook_txn_date         ON cashbook_transactions(txn_date);
CREATE INDEX idx_cashbook_txn_category     ON cashbook_transactions(category);
-- idx_cashbook_txn_vat_account intentionally dropped (no vat_flag to index)

-- ── salary_advances ──────────────────────────────────────────────────────
CREATE TABLE salary_advances_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER REFERENCES employees(id),
    advance_date       TEXT    NOT NULL,
    amount             REAL    NOT NULL,
    raw_name           TEXT,
    note               TEXT,
    deducted_in_run_id INTEGER REFERENCES payroll_runs(id),
    source_file        TEXT,
    import_batch_id    TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO salary_advances_new
    (id, employee_id, advance_date, amount, raw_name, note,
     deducted_in_run_id, source_file, import_batch_id, created_at)
SELECT id, employee_id, advance_date, amount, raw_name, note,
       deducted_in_run_id, source_file, import_batch_id, created_at
FROM salary_advances;

DROP TABLE salary_advances;
ALTER TABLE salary_advances_new RENAME TO salary_advances;

CREATE INDEX idx_salary_advances_emp ON salary_advances(employee_id);
CREATE INDEX idx_salary_advances_run ON salary_advances(deducted_in_run_id);

COMMIT;
