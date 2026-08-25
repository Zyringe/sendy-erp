-- Rollback 173. SQLite supports DROP COLUMN from 3.35; both columns are
-- nullable add-ons with no index/constraint, so dropping them is exact.
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions DROP COLUMN brand_other_short_code;
ALTER TABLE pending_product_suggestions DROP COLUMN brand_other_name_th;

COMMIT;
