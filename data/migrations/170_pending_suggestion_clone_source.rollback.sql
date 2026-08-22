-- Rollback for 170_pending_suggestion_clone_source.sql.
--
-- Drops clone_source_pid, restoring pending_product_suggestions to its
-- pre-170 shape. ALTER TABLE ... DROP COLUMN operates on the CURRENT table
-- in place (no snapshot/recreate needed) — rows staged after the forward
-- migration survive; only the dropped column's values are lost, which is
-- correct, they did not exist before 170. No trigger/index references this
-- column, so nothing else needs restoring.

PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions DROP COLUMN clone_source_pid;

DELETE FROM applied_migrations WHERE filename = '170_pending_suggestion_clone_source.sql';

COMMIT;
