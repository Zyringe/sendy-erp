-- ============================================================================
-- Rollback for 151 — drop the (table_name, row_key) index and the four
-- PK-immutability guard triggers. Nothing lossy: the index carries no data
-- of its own, and the guards only ever reject writes, they never produce
-- any.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS customers_code_immutable;
DROP TRIGGER IF EXISTS salespersons_code_immutable;
DROP TRIGGER IF EXISTS commission_assignments_salesperson_code_immutable;
DROP TRIGGER IF EXISTS customer_crm_customer_code_immutable;

DROP INDEX IF EXISTS idx_audit_log_table_row_key;

COMMIT;
