-- 062_credit_note_amounts.rollback.sql
BEGIN;
DROP INDEX IF EXISTS idx_cna_ref_invoice;
DROP TABLE IF EXISTS credit_note_amounts;
DELETE FROM applied_migrations WHERE filename = '062_credit_note_amounts.sql';
COMMIT;
