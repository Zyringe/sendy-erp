-- Rollback for mig 070 — drop only the 4 triggers added by mig 070.
-- Leaves the pre-existing audit_* triggers (mig 023) untouched.
BEGIN;
DROP TRIGGER IF EXISTS audit_transactions_insert;
DROP TRIGGER IF EXISTS audit_received_payments_insert;
DROP TRIGGER IF EXISTS audit_received_payments_update;
DROP TRIGGER IF EXISTS audit_received_payments_delete;
COMMIT;
