-- Rollback for 169_pending_suggestion_subcategory.sql.
--
-- Drops the three columns, restoring pending_product_suggestions to its
-- pre-169 shape. ALTER TABLE ... DROP COLUMN operates on the CURRENT table
-- in place (no snapshot/recreate needed) — rows staged after the forward
-- migration survive; only the three dropped columns' values are lost, which
-- is correct, they did not exist before 169. No trigger/index references
-- these columns, so nothing else needs restoring.

PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions DROP COLUMN category_id;
ALTER TABLE pending_product_suggestions DROP COLUMN sub_category_short_code;
ALTER TABLE pending_product_suggestions DROP COLUMN sub_category;

DELETE FROM applied_migrations WHERE filename = '169_pending_suggestion_subcategory.sql';

COMMIT;
