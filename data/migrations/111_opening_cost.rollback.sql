-- 111_opening_cost.rollback.sql
-- Drops the opening_cost column added in 111_opening_cost.sql.
-- SQLite >= 3.35 supports ALTER TABLE DROP COLUMN (prod + dev run 3.51).
-- After running this, also: DELETE FROM applied_migrations WHERE filename='111_opening_cost.sql';
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

ALTER TABLE products DROP COLUMN opening_cost;

COMMIT;
