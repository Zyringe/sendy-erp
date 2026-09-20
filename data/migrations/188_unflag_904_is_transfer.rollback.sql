-- Rollback 188 — 904 becomes a transfer account again.
--
-- Restores the row byte-identically: 188 changes only `is_transfer` and
-- leaves `updated_at` alone, so setting the flag back is the whole inverse.
-- Setting it on a 904 someone has since re-activated is still safe — a
-- transfer account is refused by every pay-from guard.
--
-- Run manually; the migration runner does not auto-rollback.

PRAGMA busy_timeout = 10000;

BEGIN;

UPDATE cashbook_accounts
   SET is_transfer = 1
 WHERE code = '904'
   AND is_transfer = 0;

DELETE FROM applied_migrations WHERE filename = '188_unflag_904_is_transfer.sql';

COMMIT;
