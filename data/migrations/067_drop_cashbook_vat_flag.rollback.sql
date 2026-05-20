-- 067_drop_cashbook_vat_flag.rollback.sql
-- Restores vat_flag column on cashbook_transactions and salary_advances.
-- All existing rows default to 'novat' (the only value that ever existed in
-- live data before 067 dropped the column).
-- Run manually; the migration runner does not auto-rollback.

BEGIN;

-- ── cashbook_transactions ────────────────────────────────────────────────
CREATE TABLE cashbook_transactions_old (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES cashbook_accounts(id),
    txn_date        TEXT    NOT NULL,
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

INSERT INTO cashbook_transactions_old
    (id, account_id, txn_date, direction, category, user_category,
     amount, description, note, vat_flag, source_file, source_sheet,
     source_row, import_batch_id, created_at)
SELECT id, account_id, txn_date, direction, category, user_category,
       amount, description, note, 'novat', source_file, source_sheet,
       source_row, import_batch_id, created_at
FROM cashbook_transactions;

DROP TABLE cashbook_transactions;
ALTER TABLE cashbook_transactions_old RENAME TO cashbook_transactions;

CREATE INDEX idx_cashbook_txn_account_date ON cashbook_transactions(account_id, txn_date);
CREATE INDEX idx_cashbook_txn_vat_account  ON cashbook_transactions(vat_flag, account_id);
CREATE INDEX idx_cashbook_txn_date         ON cashbook_transactions(txn_date);
CREATE INDEX idx_cashbook_txn_category     ON cashbook_transactions(category);

-- ── salary_advances ──────────────────────────────────────────────────────
CREATE TABLE salary_advances_old (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER REFERENCES employees(id),
    advance_date       TEXT    NOT NULL,
    amount             REAL    NOT NULL,
    raw_name           TEXT,
    note               TEXT,
    deducted_in_run_id INTEGER REFERENCES payroll_runs(id),
    vat_flag           TEXT    CHECK(vat_flag IN ('novat','vat') OR vat_flag IS NULL),
    source_file        TEXT,
    import_batch_id    TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO salary_advances_old
    (id, employee_id, advance_date, amount, raw_name, note,
     deducted_in_run_id, vat_flag, source_file, import_batch_id, created_at)
SELECT id, employee_id, advance_date, amount, raw_name, note,
       deducted_in_run_id, 'novat', source_file, import_batch_id, created_at
FROM salary_advances;

DROP TABLE salary_advances;
ALTER TABLE salary_advances_old RENAME TO salary_advances;

CREATE INDEX idx_salary_advances_emp ON salary_advances(employee_id);
CREATE INDEX idx_salary_advances_run ON salary_advances(deducted_in_run_id);

-- Record rollback
DELETE FROM applied_migrations WHERE filename = '067_drop_cashbook_vat_flag.sql';

COMMIT;
