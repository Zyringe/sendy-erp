-- 192 — GH #601 (#595 · 6/7): the VAT book (xp5) reads its OWN unit list.
--
-- ADR 0018: a code's meaning comes from the Express book it came from.
-- #596 (mig 185) seeded `unit_map` with only BSN5657's 44 entries — every
-- xp5 code fell through to BSN5657's meaning, wrongly, for the one code the
-- two books disagree on: xp5's `หอ` is หลอด (sealant/glue tube), not ห่อ.
-- xp5 also has one code BSN5657 lacks entirely: `ดว` = ดวง.
--
-- Verified against both books' own ISTAB (TABTYP '20'), read live off the
-- DBF snapshots at projects/express-integration/data/{BSN5657,xp5}, 2026-09-21:
--   xp5 has 34 codes. Every one of them, EXCEPT `หอ`, carries the identical
--   Express meaning (TYPDES) BSN5657's ISTAB shows for the same code —
--   `หอ` is the only disagreement (ห่อ vs หลอด). `ดว` (ดวง) exists only in
--   xp5's list; `หด` (BSN5657's spelling of หลอด) does not exist in xp5's.
--
-- ⛔ DEPENDS ON MIG 190 (#599, "กร/ถง/บล take Express's meaning") HAVING
-- ALREADY APPLIED. This migration's SEEDING RULE copies whatever word
-- BSN5657's row holds in `unit_map` AT THE MOMENT it runs — a live
-- `INSERT ... SELECT`, not a hand-typed literal, so it does not duplicate
-- 190's own word list. But it is a one-time COPY, not a live link: 190's
-- own UPDATE is scoped to `WHERE book = 'BSN5657'` and never touches
-- book='xp5' rows, so if this migration ran BEFORE 190, xp5 would freeze
-- at กร=ตัว / ถง=ถุง forever (190 never revisits a row this migration
-- already wrote, and this migration itself never re-runs once applied).
-- The runner applies migrations in filename order, and 190 < 192, so on
-- every real environment (fresh install or existing DB) 190 is guaranteed
-- to apply first — this is NOT "correct regardless of order"; it is
-- correct BECAUSE the filename ordering enforces the one order that works.
-- Do not lower this migration's number below 190's for any reason.
-- (An earlier draft of this file, numbered 191 before #625 claimed that
-- number for an unrelated migration, mis-stated this as self-healing
-- "regardless of merge order" — reviewer-caught, corrected here.)
--
-- Verified live 2026-09-22 (this branch rebased onto #628, mig 190 applied
-- first): xp5's `กร` and `ถง` copy through as **กุรุส** / **ถัง** (190's
-- corrected words, matching xp5's own ISTAB meaning for those same codes —
-- see test_migration_192_unit_map_xp5.py's dedicated pin).
--
-- `บล` has no xp5 counterpart at all (xp5's own ISTAB does not list it), so
-- it is correctly absent from the copy list below.
-- Five xp5 codes (ขว, คร, ตล, ทน, ใบ) have no existing BSN5657 row to copy —
-- same as BSN5657's own gaps (ขว, เม, ครึ่งโล, ...) — and are left for #610
-- ("Unit map learns the approved vocabulary"), not invented here.
--
-- Re-runnable WITHOUT losing data: INSERT OR IGNORE, so a second run (e.g. a
-- DB whose applied_migrations lost this row) never overwrites a code Put has
-- since named on /unit-conversions under book='xp5'.

BEGIN;

-- Every xp5 code whose Express meaning matches BSN5657's (all of xp5's 34
-- codes except หอ, which disagrees, and ดว, which BSN5657 doesn't have).
INSERT OR IGNORE INTO unit_map (book, spelling, word)
SELECT 'xp5', spelling, word FROM unit_map
WHERE book = 'BSN5657' AND spelling IN (
  'กก','กน','กป','กร','กล','ขด','คน','คู','ชด','ชน','ซง','ดก','ตว','ถง',
  'ทง','ปน','ผง','ผน','มน','ลก','ลง','สน','หค','หล','อน','แก','แพ'
);

-- xp5-specific: the one disagreement (หอ) and the one xp5-only code (ดว).
INSERT OR IGNORE INTO unit_map (book, spelling, word) VALUES
  ('xp5', 'หอ', 'หลอด'),
  ('xp5', 'ดว', 'ดวง');

COMMIT;
