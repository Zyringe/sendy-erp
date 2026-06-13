-- Rollback 102_marketplace_amount_review.sql
-- After running: DELETE FROM applied_migrations WHERE filename='102_marketplace_amount_review.sql';

BEGIN;
DROP TABLE IF EXISTS marketplace_amount_review;
COMMIT;
