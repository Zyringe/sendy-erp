-- data/migrations/127_product_labels.rollback.sql
-- Drops both tables (indexes go with them). Run manually; the migration
-- runner does not auto-rollback.

BEGIN;
DROP TABLE IF EXISTS product_labels;
DROP TABLE IF EXISTS label_company_block;
DELETE FROM applied_migrations WHERE filename = '127_product_labels.sql';
COMMIT;
