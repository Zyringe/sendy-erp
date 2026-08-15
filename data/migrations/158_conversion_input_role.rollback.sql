-- Rollback for 158_conversion_input_role.sql.
--
-- Restores conversion_formula_inputs to its pre-158 shape (drops `role`) and
-- drops the partial unique index on conversion_formulas. Nothing in Phase 0
-- writes or transforms data — no backfill ran forward, so there is no
-- reverse-backfill here either. This rollback is purely structural, which
-- means it is automatically safe for the "preserve rows inserted after the
-- forward migration ran" rule: `ALTER TABLE ... DROP COLUMN` operates on the
-- CURRENT table in place (verified on sqlite3.sqlite_version 3.51.0 — a row
-- inserted after the forward migration, with data in every other column,
-- survives the drop untouched; only the dropped column's values are lost,
-- and this migration never wrote any).
--
-- `role` carries no FK, no index of its own, and is referenced only by its
-- own CHECK constraint (which does not block DROP COLUMN — verified). The
-- unique index lives on a different table (conversion_formulas) and has no
-- dependency ordering against dropping `role`; both drops are included for
-- a clean rollback regardless of order.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS ux_conv_active_pack_per_output;

ALTER TABLE conversion_formula_inputs DROP COLUMN role;

DELETE FROM applied_migrations WHERE filename='158_conversion_input_role.sql';

COMMIT;
