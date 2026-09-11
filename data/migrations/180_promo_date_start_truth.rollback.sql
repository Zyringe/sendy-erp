-- Rollback 180 — restore every touched promotion's ORIGINAL date_start from
-- migration_180_snapshot, then drop the snapshot so sqlite_master comes back
-- byte-identical to the pre-migration schema.
--
-- Restores from the SNAPSHOT, never from a pattern: after the forward
-- migration a row whose true start legitimately equals date(created_at) is
-- indistinguishable from one this migration set, so any derived rollback would
-- corrupt the honest rows. Rows deleted since the forward run are simply not
-- matched.
--
-- The UPDATE re-fires promotions_one_per_slot_upd (mig 177). Restoring
-- '2024-01-01' widens each interval back to where it already was when 177's
-- own precondition passed, so it cannot introduce an overlap that did not
-- exist before.
--
-- Re-runnable: the IF NOT EXISTS keeps a second run a harmless no-op rather
-- than "no such table: migration_180_snapshot".
PRAGMA busy_timeout = 10000;

BEGIN;

CREATE TABLE IF NOT EXISTS migration_180_snapshot (
  promotion_id   INTEGER PRIMARY KEY,
  old_date_start TEXT
);

UPDATE promotions
   SET date_start = (SELECT s.old_date_start
                       FROM migration_180_snapshot s
                      WHERE s.promotion_id = promotions.id)
 WHERE id IN (SELECT promotion_id FROM migration_180_snapshot);

DROP TABLE migration_180_snapshot;

COMMIT;
