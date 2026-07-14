-- Rollback 136_platform_skus_is_ignored.sql
-- After running: DELETE FROM applied_migrations WHERE filename='136_platform_skus_is_ignored.sql';
ALTER TABLE platform_skus DROP COLUMN is_ignored;
