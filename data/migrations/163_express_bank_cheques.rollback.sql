-- Rollback for 163_express_bank_cheques.sql — a new table, so it just goes.
BEGIN;
DROP INDEX IF EXISTS idx_bank_cheques_due;
DROP INDEX IF EXISTS idx_bank_cheques_party;
DROP TABLE IF EXISTS express_bank_cheques;
COMMIT;
