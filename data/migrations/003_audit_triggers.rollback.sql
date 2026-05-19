-- 003_audit_triggers.rollback.sql
-- Rolls back 003_audit_triggers.sql.
-- Drops the 9 audit triggers (3 per tracked table). audit_log table itself
-- remains (created by migration 001). Audit rows captured in the meantime
-- are retained — drop them separately if a clean slate is desired.
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/003_audit_triggers.rollback.sql
--
-- Verify (each should be 0):
--   sqlite3 .../inventory.db "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_%';"

BEGIN;

DROP TRIGGER IF EXISTS audit_products_insert;
DROP TRIGGER IF EXISTS audit_products_update;
DROP TRIGGER IF EXISTS audit_products_delete;

DROP TRIGGER IF EXISTS audit_customers_insert;
DROP TRIGGER IF EXISTS audit_customers_update;
DROP TRIGGER IF EXISTS audit_customers_delete;

DROP TRIGGER IF EXISTS audit_suppliers_insert;
DROP TRIGGER IF EXISTS audit_suppliers_update;
DROP TRIGGER IF EXISTS audit_suppliers_delete;

COMMIT;
