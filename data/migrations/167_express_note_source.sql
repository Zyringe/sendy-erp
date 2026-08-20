-- ============================================================================
-- 167 — express_payments_out.note_source / express_credit_notes.note_source.
--
-- WHY. `note` has two sources with different fidelity. The printed Express
-- report captured several lines; the DBF carries one short APTRN.YOUREF. So a
-- daily DBF refresh (mig-less, but see 43f0459's replace_existing) has to
-- decide what to do when the incoming YOUREF differs from the stored note, and
-- with only those two strings it CANNOT tell a correction from a truncation:
--
--   stored '711 โอน 37,635 / VAT 26,613 / อานี 37,635', incoming '712 โอน 37,635'
--
-- is either "Express fixed 711 to 712" or "YOUREF only ever held the first
-- line". A prefix test answers the second and silently deletes the VAT and
-- อานี lines in the first — the merge blocker Codex raised on 2026-08-20.
--
-- This column records the YOUREF the row's note was last written with, which
-- makes the split exact rather than inferred: the YOUREF-derived part of the
-- note is replaced, the Sendy-only remainder is carried across.
--
-- NULL means "this note did not come from YOUREF" — a printed-report import,
-- or any row predating this column. Those get a one-time preserve on their
-- first DBF contact, which is what protects the 40 rows measured on 2026-08-20
-- (24 with a blank YOUREF behind a stored note, 16 whose note is longer than
-- YOUREF). Measured on all 281 shared documents, the stored note splits on the
-- YOUREF prefix cleanly in 100% of cases.
--
-- Additive and nullable: no backfill, no rewrite of existing rows. Sendy has
-- no UI that writes `note` (ap.html renders it read-only and no UPDATE touches
-- it outside the importers), so an import is the ONLY way a wrong note can
-- ever be corrected — which is why freezing the field instead was rejected.
-- ============================================================================
PRAGMA busy_timeout = 10000;
BEGIN IMMEDIATE;
ALTER TABLE express_payments_out ADD COLUMN note_source TEXT;
ALTER TABLE express_credit_notes ADD COLUMN note_source TEXT;
COMMIT;
