-- 002_supplier_catalogue.rollback.sql
-- Rolls back 002_supplier_catalogue.sql.
--
-- Reversibility:
--   The forward migration only creates new tables + indexes and seeds one
--   row in `suppliers`. No existing tables are altered. Dropping these
--   tables cleanly restores the DB to its pre-migration state. Any
--   catalogue/mapping data captured between forward+rollback is discarded
--   (intended).
--
-- Note (2026-04-29): forward migration was patched after first apply to fix
--   the supplier_product_mapping CHECK constraint. The DB has the patched
--   shape; running 002 fresh will yield the same shape.
--
-- Pre-flight (recommended):
--   1. Stop the Flask app.
--   2. Take a backup:
--        /Users/putty/Sendai-Boonsawat/sendy_erp/scripts/backup_db.sh
--   3. Confirm no triggers reference these tables (none expected at v002):
--        sqlite3 .../inventory.db "SELECT name, sql FROM sqlite_master
--          WHERE type='trigger' AND sql LIKE '%supplier_catalogue%';"
--
-- Apply rollback:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/002_supplier_catalogue.rollback.sql
--
-- Verify (each should be 0):
--   sqlite3 .../inventory.db ".tables" | tr ' ' '\n' | grep -c '^supplier_'
--   sqlite3 .../inventory.db ".tables" | tr ' ' '\n' | grep -c '^suppliers$'
--
-- Restore from backup (full DB-level rollback, if rollback above fails):
--   See /Users/putty/Sendai-Boonsawat/sendy_erp/scripts/RESTORE.md

BEGIN;

-- Drop child tables first (FK ON DELETE CASCADE handles this on data, but
-- explicit drop order keeps SQLite happy if FKs are enforced).
DROP INDEX IF EXISTS idx_supplier_mapping_purchase;
DROP INDEX IF EXISTS idx_supplier_mapping_catalogue;
DROP INDEX IF EXISTS idx_supplier_mapping_product;
DROP TABLE IF EXISTS supplier_product_mapping;

DROP INDEX IF EXISTS idx_quick_updates_supplier;
DROP TABLE IF EXISTS supplier_quick_updates;

DROP INDEX IF EXISTS idx_catalogue_price_history_item;
DROP TABLE IF EXISTS supplier_catalogue_price_history;

DROP INDEX IF EXISTS idx_catalogue_items_name_norm;
DROP INDEX IF EXISTS idx_catalogue_items_supplier_active;
DROP TABLE IF EXISTS supplier_catalogue_items;

DROP INDEX IF EXISTS idx_catalogue_versions_supplier;
DROP TABLE IF EXISTS supplier_catalogue_versions;

DROP TABLE IF EXISTS suppliers;

COMMIT;
