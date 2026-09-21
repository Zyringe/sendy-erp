-- 191 — GH #601 (#595 · 6/7): the VAT book (xp5) reads its OWN unit list.
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
-- SEEDING RULE for this migration (stated so a later reader does not have to
-- re-derive it): for every xp5 code whose Express meaning agrees with
-- BSN5657's, COPY the word BSN5657's row ALREADY HOLDS in `unit_map` at the
-- moment this migration runs — not a hand-typed literal. This is
-- deliberately a live SELECT, not a copy-pasted value list, so this
-- migration is correct regardless of merge order against #599 (mig 190,
-- which corrects BSN5657's `กร`/`ถง`/`บล`): if #599 has already applied on
-- a given database, xp5 inherits the CORRECTED word; if it has not yet, xp5
-- inherits today's word and is unaffected either way — #599 only ever
-- touches BSN5657's rows, never xp5's. `บล` has no xp5 counterpart at all
-- (xp5's own ISTAB does not list it), so it is correctly absent from the
-- copy list below.
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
