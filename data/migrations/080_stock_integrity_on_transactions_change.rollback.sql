-- Rollback for mig 080 — drop the 2 new business triggers.
-- `after_transaction_insert` (canonical) stays untouched.
BEGIN;
DROP TRIGGER IF EXISTS after_transaction_update;
DROP TRIGGER IF EXISTS after_transaction_delete;
DELETE FROM applied_migrations WHERE filename = '080_stock_integrity_on_transactions_change.sql';
COMMIT;
