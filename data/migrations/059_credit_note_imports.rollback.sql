-- 059_credit_note_imports.rollback.sql
BEGIN;
DROP INDEX IF EXISTS idx_cni_ref_inv;
DROP INDEX IF EXISTS idx_cni_doc_base;
DROP TABLE IF EXISTS credit_note_imports;
COMMIT;
