-- Rollback 191 — remove every book='xp5' row this migration added.
-- Pure additive forward migration (no existing row was touched, no BSN5657
-- row was read anywhere but the SELECT source), so undo is a plain DELETE:
-- this migration is the only writer of book='xp5' rows to date.
--
-- After running this: DELETE FROM applied_migrations
--   WHERE filename = '191_unit_map_xp5_meanings.sql';

BEGIN;

DELETE FROM unit_map WHERE book = 'xp5';

COMMIT;
