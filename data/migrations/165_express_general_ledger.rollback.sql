-- Rollback for 165_express_general_ledger.sql — three new tables, so they go.
BEGIN;
DROP INDEX IF EXISTS idx_gl_lines_account;
DROP INDEX IF EXISTS idx_gl_lines_voucher;
DROP INDEX IF EXISTS idx_gl_vouchers_date;
DROP TABLE IF EXISTS express_gl_lines;
DROP TABLE IF EXISTS express_gl_vouchers;
DROP TABLE IF EXISTS express_gl_accounts;
COMMIT;
