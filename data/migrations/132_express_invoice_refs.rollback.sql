-- 132_express_invoice_refs.rollback.sql
-- Rolls back 132_express_invoice_refs.sql — drops the side table and
-- de-registers the migration. Any captured YOUREF/REMARK values are
-- discarded (they are re-derivable from the DBF on the next daily import).
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/132_express_invoice_refs.rollback.sql
--
-- Verify (should be 0):
--   SELECT COUNT(*) FROM sqlite_master WHERE name='express_invoice_refs';

BEGIN;

DROP TABLE IF EXISTS express_invoice_refs;

DELETE FROM applied_migrations WHERE filename = '132_express_invoice_refs.sql';

COMMIT;
