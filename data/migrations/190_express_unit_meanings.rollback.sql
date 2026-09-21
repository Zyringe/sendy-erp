-- Rollback of 190_express_unit_meanings.sql (#599).
--
-- Puts the three BSN5657 map rows back to the words Sendy's hand-built map
-- produced (ตัว / ถุง / แผง) and deletes exactly the conversions migration 190
-- inserted — by id, from its own snapshot, so a `กุรุส` row a human (or #603)
-- created afterwards at a different ratio is left alone.
--
-- ⛔ RUN ONLY through the migration runner or `sqlite3 -bail`: the guard below
-- is a RAISE(ABORT) and the plain CLI would carry on past it.
--
-- The map rows are restored from migration_190_snapshot rather than
-- hard-coded, so a rollback after a later re-name still returns the value this
-- migration actually overwrote. The snapshot tables themselves are KEPT: they
-- are the forensic record, and dropping them would make a second forward run
-- unable to tell what it had already done.
--
-- Re-runnable: both statements are no-ops once they have run (the ids are
-- gone, the words already match).

PRAGMA busy_timeout = 10000;

BEGIN;

-- ── Guard: refuse if a row 190 inserted has since been given a DIFFERENT
--    ratio. Deleting it would then silently discard someone's correction.
DROP TABLE IF EXISTS temp._mig190_rb_changed;
CREATE TEMP TABLE _mig190_rb_changed AS
SELECT u.id FROM unit_conversions u
  JOIN migration_190_uc_inserted s ON s.id = u.id
 WHERE u.product_id IS NOT s.product_id OR u.bsn_unit IS NOT s.bsn_unit
    OR u.ratio IS NOT s.ratio;

CREATE TEMP TRIGGER _mig190_rb_guard BEFORE DELETE ON _mig190_rb_changed
BEGIN SELECT RAISE(ABORT, 'mig 190 rollback REFUSED: a conversion row it inserted has been edited since. Reconcile it by hand, then delete the row from migration_190_uc_inserted.'); END;
DELETE FROM _mig190_rb_changed;
DROP TABLE _mig190_rb_changed;

DELETE FROM unit_conversions WHERE id IN (SELECT id FROM migration_190_uc_inserted);

UPDATE unit_map
   SET word = (SELECT s.old_value FROM migration_190_snapshot s
                WHERE s.table_name = 'unit_map' AND s.column_name = 'word'
                  AND s.row_id = unit_map.id)
 WHERE id IN (SELECT row_id FROM migration_190_snapshot
               WHERE table_name = 'unit_map' AND column_name = 'word');

COMMIT;
