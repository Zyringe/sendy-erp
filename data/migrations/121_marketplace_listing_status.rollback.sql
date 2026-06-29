-- data/migrations/121_marketplace_listing_status.rollback.sql
BEGIN;
DROP TABLE IF EXISTS marketplace_listing_status;
DELETE FROM applied_migrations WHERE filename = '121_marketplace_listing_status.sql';
COMMIT;
