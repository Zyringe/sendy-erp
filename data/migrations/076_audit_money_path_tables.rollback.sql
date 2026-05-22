-- Rollback for mig 076 — drop 9 audit triggers across the 3 money-path tables.

BEGIN;

DROP TRIGGER IF EXISTS audit_paid_invoices_insert;
DROP TRIGGER IF EXISTS audit_paid_invoices_update;
DROP TRIGGER IF EXISTS audit_paid_invoices_delete;

DROP TRIGGER IF EXISTS audit_credit_note_amounts_insert;
DROP TRIGGER IF EXISTS audit_credit_note_amounts_update;
DROP TRIGGER IF EXISTS audit_credit_note_amounts_delete;

DROP TRIGGER IF EXISTS audit_cashbook_transactions_insert;
DROP TRIGGER IF EXISTS audit_cashbook_transactions_update;
DROP TRIGGER IF EXISTS audit_cashbook_transactions_delete;

DELETE FROM applied_migrations WHERE filename = '076_audit_money_path_tables.sql';
COMMIT;
