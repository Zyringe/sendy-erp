-- 011_expenses.rollback.sql
-- Reverses 011_expenses.sql.
--
-- Drops triggers + tables in FK-safe order: expense_log first (it
-- references companies + expense_categories), then the two parent
-- tables.
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/011_expenses.rollback.sql

BEGIN;

DROP TRIGGER IF EXISTS audit_expense_log_delete;
DROP TRIGGER IF EXISTS audit_expense_log_update;
DROP TRIGGER IF EXISTS audit_expense_log_insert;
DROP TRIGGER IF EXISTS audit_expense_categories_delete;
DROP TRIGGER IF EXISTS audit_expense_categories_update;
DROP TRIGGER IF EXISTS audit_expense_categories_insert;
DROP TRIGGER IF EXISTS audit_companies_delete;
DROP TRIGGER IF EXISTS audit_companies_update;
DROP TRIGGER IF EXISTS audit_companies_insert;

DROP INDEX IF EXISTS idx_expense_log_category;
DROP INDEX IF EXISTS idx_expense_log_company;
DROP INDEX IF EXISTS idx_expense_log_date;
DROP TABLE IF EXISTS expense_log;
DROP TABLE IF EXISTS expense_categories;
DROP TABLE IF EXISTS companies;

COMMIT;
