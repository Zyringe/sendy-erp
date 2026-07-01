-- data/migrations/122_product_created_via.rollback.sql
-- SQLite >=3.35 DROP COLUMN; created_via is a plain nullable column.
BEGIN;
ALTER TABLE products DROP COLUMN created_via;
DELETE FROM applied_migrations WHERE filename = '122_product_created_via.sql';
COMMIT;
