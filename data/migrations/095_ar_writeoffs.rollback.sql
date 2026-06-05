-- Rollback 095 — drop the AR write-off ledger.
-- Purely additive forward migration → safe to drop. (Any recorded write-off
-- decisions are lost; re-record from the accountant package if rolled back.)

BEGIN;

DROP INDEX IF EXISTS idx_ar_writeoffs_customer;
DROP INDEX IF EXISTS idx_ar_writeoffs_doc;
DROP TABLE IF EXISTS ar_writeoffs;

DELETE FROM applied_migrations WHERE filename = '095_ar_writeoffs.sql';

COMMIT;
