-- rollback 173 — drop the triggers AND the columns, so the schema returns to
-- exactly its pre-173 shape and a rollback-then-reapply cycle works.
--
-- Column drops rewrite the table, which is why the first version skipped them.
-- The tables are ~20k and ~4k rows and audit_log ~440k, so the rewrite is
-- seconds — cheaper than leaving a schema that claims to have been rolled back
-- but has not been.

DROP TRIGGER IF EXISTS sales_transactions_change_needs_declaration;
DROP TRIGGER IF EXISTS audit_sales_transactions_update;
DROP TRIGGER IF EXISTS audit_sales_transactions_insert;
DROP TRIGGER IF EXISTS audit_sales_transactions_delete;
DROP TRIGGER IF EXISTS purchase_transactions_change_needs_declaration;
DROP TRIGGER IF EXISTS audit_purchase_transactions_update;
DROP TRIGGER IF EXISTS audit_purchase_transactions_insert;
DROP TRIGGER IF EXISTS audit_purchase_transactions_delete;

ALTER TABLE sales_transactions    DROP COLUMN change_source;
ALTER TABLE sales_transactions    DROP COLUMN change_actor;
ALTER TABLE sales_transactions    DROP COLUMN change_reason;
ALTER TABLE sales_transactions    DROP COLUMN change_token;
ALTER TABLE purchase_transactions DROP COLUMN change_source;
ALTER TABLE purchase_transactions DROP COLUMN change_actor;
ALTER TABLE purchase_transactions DROP COLUMN change_reason;
ALTER TABLE purchase_transactions DROP COLUMN change_token;
ALTER TABLE audit_log             DROP COLUMN change_source;
ALTER TABLE audit_log             DROP COLUMN change_reason;
