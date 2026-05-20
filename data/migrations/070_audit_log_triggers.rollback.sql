-- Rollback for mig 070 — drop only the 4 triggers + index added by mig 070.
-- Leaves the pre-existing audit_* triggers (mig 023) untouched.
-- Also removes the bookkeeping row from applied_migrations so the runner
-- re-applies 070 on the next boot (matches the pattern of 058 / 067).
BEGIN;
DROP TRIGGER IF EXISTS audit_transactions_insert;
DROP TRIGGER IF EXISTS audit_received_payments_insert;
DROP TRIGGER IF EXISTS audit_received_payments_update;
DROP TRIGGER IF EXISTS audit_received_payments_delete;
DROP INDEX  IF EXISTS idx_audit_log_table_time;
DELETE FROM applied_migrations WHERE filename = '070_audit_log_triggers.sql';
COMMIT;
