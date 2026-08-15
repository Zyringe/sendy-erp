-- Rollback for 159_ar_followup_soft_delete.sql.
--
-- Restores ar_followup_log to its pre-159 shape: drops the partial index and
-- both soft-delete columns. Purely structural — the forward migration ran no
-- backfill and transformed no data, so there is no reverse-backfill here.
--
-- ⚠ Native operations on the CURRENT table, never a snapshot restore. Rows
-- inserted AFTER the forward migration ran (real outreach the team logged in
-- the meantime) MUST survive this rollback, and `ALTER TABLE ... DROP COLUMN`
-- rewrites the live table in place, so they do: only the two dropped columns'
-- values are lost, every other column of every row is untouched. Verified on
-- sqlite3.sqlite_version 3.51.0 (DROP COLUMN has existed since 3.35.0), and
-- pinned by tests/test_mig159_ar_followup_soft_delete.py.
--
-- ORDER IS LOAD-BEARING: SQLite refuses to DROP a column that a partial
-- index's WHERE clause references ("error in index ... after drop column").
-- `idx_ar_followup_active_customer` is `WHERE deleted_at IS NULL`, so the
-- index must go FIRST. `deleted_by` carries no index, no FK, no CHECK and no
-- trigger/view reference, so its order relative to `deleted_at` is free.
--
-- The three pre-existing indexes (idx_ar_followup_customer,
-- idx_ar_followup_log_date, idx_ar_followup_next_action) reference neither
-- dropped column and survive both directions byte-identical — SQLite carries
-- untouched indexes across a DROP COLUMN table rewrite.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_ar_followup_active_customer;

ALTER TABLE ar_followup_log DROP COLUMN deleted_by;
ALTER TABLE ar_followup_log DROP COLUMN deleted_at;

DELETE FROM applied_migrations WHERE filename='159_ar_followup_soft_delete.sql';

COMMIT;
