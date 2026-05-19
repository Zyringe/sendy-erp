-- 060_sr_writeoffs.rollback.sql
BEGIN;
DROP INDEX IF EXISTS idx_srwo_reason;
DROP INDEX IF EXISTS idx_srwo_doc_base;
DROP TABLE IF EXISTS sr_writeoffs;
DELETE FROM applied_migrations WHERE filename = '060_sr_writeoffs.sql';
COMMIT;
