-- 010_regions_salespersons.rollback.sql
-- Reverses 010_regions_salespersons.sql.
--
-- Drops the audit triggers, the new region_id column on customers, and
-- the regions + salespersons tables. customers.zone and the legacy
-- customer_regions table are untouched (they survive until migration 011).
--
-- Apply:
--   sqlite3 /Users/putty/Sendai-Boonsawat/sendy_erp/inventory_app/instance/inventory.db \
--       < /Users/putty/Sendai-Boonsawat/sendy_erp/data/migrations/010_regions_salespersons.rollback.sql

BEGIN;

DROP TRIGGER IF EXISTS audit_salespersons_delete;
DROP TRIGGER IF EXISTS audit_salespersons_update;
DROP TRIGGER IF EXISTS audit_salespersons_insert;
DROP TRIGGER IF EXISTS audit_regions_delete;
DROP TRIGGER IF EXISTS audit_regions_update;
DROP TRIGGER IF EXISTS audit_regions_insert;

DROP INDEX IF EXISTS idx_customers_region;
ALTER TABLE customers DROP COLUMN region_id;

DROP TABLE IF EXISTS salespersons;
DROP INDEX IF EXISTS idx_regions_parent;
DROP TABLE IF EXISTS regions;

COMMIT;
