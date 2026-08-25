-- rollback 172 — drop the triggers. The columns are left in place: an
-- ALTER TABLE DROP COLUMN would rewrite two large tables, and a nullable
-- column nothing reads is inert.
DROP TRIGGER IF EXISTS sales_transactions_change_needs_declaration;
DROP TRIGGER IF EXISTS audit_sales_transactions_update;
DROP TRIGGER IF EXISTS audit_sales_transactions_insert;
DROP TRIGGER IF EXISTS audit_sales_transactions_delete;
DROP TRIGGER IF EXISTS purchase_transactions_change_needs_declaration;
DROP TRIGGER IF EXISTS audit_purchase_transactions_update;
DROP TRIGGER IF EXISTS audit_purchase_transactions_insert;
DROP TRIGGER IF EXISTS audit_purchase_transactions_delete;
