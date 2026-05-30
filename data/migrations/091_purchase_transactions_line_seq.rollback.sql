-- ============================================================================
-- Rollback 091 — drop purchase_transactions.line_seq
--
-- SQLite versions before 3.35 cannot ALTER TABLE DROP COLUMN, so we use the
-- table-rebuild-preserving-current-rows pattern (the house style for column-add
-- rollbacks — mirrors 088's rollback rebuild).
--
-- Per feedback_rename_migration_safety: the rebuild reads the CURRENT table, so
-- ALL rows present when this runs are preserved (including the 4018 batch_id=37
-- rows the loader added) — only the `line_seq` column is dropped.
--
-- The rebuilt schema below = the EXACT pre-091 shape, including the original
-- foreign keys (suppliers / products / import_log, all NO ACTION) and the two
-- indexes (idx_pt_doc_base, idx_pt_supplier_id) — rebuilding without them would
-- silently drop referential integrity + perf indexes after rollback.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

DROP TABLE IF EXISTS purchase_transactions_old;

-- Pre-091 shape: base columns (mig pre-history) + synced_to_stock (init_db) +
-- doc_base + supplier_id (later migs). Verified against sqlite_master 2026-05-30.
CREATE TABLE purchase_transactions_old (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            INTEGER REFERENCES import_log(id),
    date_iso            TEXT    NOT NULL,
    doc_no              TEXT    NOT NULL,
    product_id          INTEGER REFERENCES products(id),
    bsn_code            TEXT,
    product_name_raw    TEXT,
    supplier            TEXT,
    supplier_code       TEXT,
    qty                 REAL,
    unit                TEXT,
    unit_price          REAL,
    vat_type            INTEGER,
    discount            TEXT,
    total               REAL,
    net                 REAL,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    synced_to_stock     INTEGER NOT NULL DEFAULT 0,
    doc_base            TEXT,
    supplier_id         INTEGER REFERENCES suppliers(id)
);

-- Copy CURRENT rows (drops only the line_seq column). Preserves all data
-- including the 4018 batch_id=37 rows.
INSERT INTO purchase_transactions_old
    (id, batch_id, date_iso, doc_no, product_id, bsn_code, product_name_raw,
     supplier, supplier_code, qty, unit, unit_price, vat_type, discount,
     total, net, created_at, synced_to_stock, doc_base, supplier_id)
SELECT
     id, batch_id, date_iso, doc_no, product_id, bsn_code, product_name_raw,
     supplier, supplier_code, qty, unit, unit_price, vat_type, discount,
     total, net, created_at, synced_to_stock, doc_base, supplier_id
FROM purchase_transactions;

DROP TABLE purchase_transactions;
ALTER TABLE purchase_transactions_old RENAME TO purchase_transactions;

-- Recreate the two original indexes (pre-091 shape).
CREATE INDEX idx_pt_doc_base    ON purchase_transactions(doc_base);
CREATE INDEX idx_pt_supplier_id ON purchase_transactions(supplier_id);

-- Remove the applied_migrations record so the runner re-applies 091 if replayed.
DELETE FROM applied_migrations WHERE filename = '091_purchase_transactions_line_seq.sql';

COMMIT;

PRAGMA foreign_keys = ON;
