-- Rollback 168. Drops the provenance columns; `note` itself is untouched, so
-- rows keep whatever text they were last written with. After this the importer
-- can no longer distinguish a correction from a truncation — revert the code
-- with it.
PRAGMA busy_timeout = 10000;
BEGIN IMMEDIATE;
ALTER TABLE express_payments_out DROP COLUMN note_source;
ALTER TABLE express_credit_notes DROP COLUMN note_source;
DELETE FROM applied_migrations WHERE filename='168_express_note_source.sql';
COMMIT;
