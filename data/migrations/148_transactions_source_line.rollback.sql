-- ============================================================================
-- Rollback for 148 — drop the ledger's source-line provenance columns and the
-- purchase-line identity index.
--
-- SQLite 3.35+ supports ALTER TABLE ... DROP COLUMN directly, and these two
-- columns have no FK, no generated-column dependency, no view and no trigger
-- referencing them (the six triggers on `transactions` enumerate only the
-- original columns), so no table rebuild is needed. Same shape as mig 147's
-- rollback. Local + Railway are on SQLite 3.51.
--
-- Drop the index BEFORE the columns it does not reference, purely for ordering
-- clarity: it lives on purchase_transactions, not on transactions.
--
-- Genuinely lossy, and that is correct: the provenance columns are a derived
-- link that migration 148 can rebuild from scratch by replaying the same
-- positional pairing. No stock, cost, document number or business value is
-- touched — WACC simply returns to pairing by position.
--
-- ⚠ Coordinate with the application: PR1's write path in _sync_bsn_to_stock
-- and PR2's read path in wacc.py both reference these columns. Roll the code
-- back BEFORE (or with) this migration — dropping the columns under a
-- deployed reader will 500 on every recalculation.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_purchase_txn_doc_code_line;

ALTER TABLE transactions DROP COLUMN source_line_seq;
ALTER TABLE transactions DROP COLUMN source_bsn_code;

DELETE FROM applied_migrations WHERE filename = '148_transactions_source_line.sql';

COMMIT;
