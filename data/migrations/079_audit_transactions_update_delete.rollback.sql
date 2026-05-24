-- Rollback for mig 079 — drop only the 2 new triggers.
-- Leaves the pre-existing `audit_transactions_insert` (mig 070) untouched.
-- Also removes the bookkeeping row from applied_migrations so the runner
-- re-applies 079 on the next boot (matches the pattern of 058 / 067 / 070).
BEGIN;
DROP TRIGGER IF EXISTS audit_transactions_update;
DROP TRIGGER IF EXISTS audit_transactions_delete;
DELETE FROM applied_migrations WHERE filename = '079_audit_transactions_update_delete.sql';
COMMIT;
