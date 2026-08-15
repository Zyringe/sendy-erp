-- ============================================================================
-- 158 — conversion_formula_inputs.role + one active [แพ็ค] per output.
--
-- See docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5 Phase 0 for the
-- full design (bundle conversions like "1 แผง = 1 อัน + 1 การ์ด", pid
-- 268/269/270/271/869). This migration lands ONLY the schema; the bundle
-- data and the form that writes `role` are separate, later work.
--
-- Two independent pieces:
--
--   1. `conversion_formula_inputs.role` — labels a multi-input formula's rows
--      so a reader can tell which input is the real component vs. the
--      packaging, without relying on ROW ORDER (verified: no ORDER BY on any
--      of the 3 readers that walk this table, and no index existed on it at
--      all before this migration — row order is not a contract this
--      codebase can rely on going forward). Nullable, no backfill: the 122
--      existing formulas are all single-input, and NULL keeps meaning "the
--      sole input is the partner" for them — rewriting 122 rows of a
--      money-adjacent table for no functional gain is not this migration's
--      job.
--
--   2. `ux_conv_active_pack_per_output` — a partial unique index enforcing
--      "at most one ACTIVE [แพ็ค] formula per output product".
--      `models/conversions.py::get_buildable` SUMS over every active formula
--      for an output, so two active [แพ็ค] formulas for the same output
--      would double-count "แปลงได้ตอนนี้". Verified against prod 2026-08-15:
--      0 violations today — 6 outputs DO have 2 active formulas, but every
--      one of those is a [แกะ] half feeding a loose product reachable from
--      several packs, and this index deliberately does not touch that shape
--      (it only matches the literal `[แพ็ค]` name prefix — `[` is not a
--      metacharacter in SQLite LIKE, so it is not treated as a char class).
--
-- ⚠ NOT re-runnable on its own: `ALTER TABLE ... ADD COLUMN` has no IF NOT
-- EXISTS form, so a second apply fails with `duplicate column name: role`.
-- To re-apply, run 158_conversion_input_role.rollback.sql FIRST — same shape
-- as migration 157's `pre157_conn` test fixture.
--
-- Rehearsed forward + rollback on a throwaway schema-only clone before
-- commit (never mutated a real DB directly — see verification-discipline.md).
-- sqlite3.sqlite_version on this machine: 3.51.0.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE conversion_formula_inputs ADD COLUMN role TEXT
    CHECK (role IS NULL OR role IN ('component', 'packaging'));

DROP INDEX IF EXISTS ux_conv_active_pack_per_output;
CREATE UNIQUE INDEX ux_conv_active_pack_per_output
    ON conversion_formulas(output_product_id)
    WHERE is_active = 1 AND name LIKE '[แพ็ค]%';

INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('158_conversion_input_role.sql', 'auto');

COMMIT;
