-- 056_cashbook_is_transfer.sql
-- Adds is_transfer flag to cashbook_accounts.
--
-- Rationale: the Overview sheet excludes pass-through / transfer accounts from
-- its P&L totals (income ≈ expense → net contribution ≈ 0, e.g. account 904).
-- The importer detects these heuristically (Σincome ≈ Σexpense AND Σincome > 0)
-- and sets is_transfer=1 so that reporting layers can replicate the same
-- exclusion without hard-coding account codes.
--
-- Clobber-guard: if the heuristic would flip an already-1 account back to 0
-- (meaning the current file happens to show a non-zero net balance for an
-- account that was previously detected as a transfer), the importer DOES NOT
-- overwrite the existing 1 — it leaves it and emits a warning.  This protects
-- against a one-off partial-month snapshot reversing a known classification.
-- A manual override to is_transfer=1 is likewise preserved by the importer.
--
-- Apply:    via database.py::run_pending_migrations (automatic on boot)
-- Rollback: 056_cashbook_is_transfer.rollback.sql
--
-- NOTE: do NOT self-insert into applied_migrations here.

BEGIN;

ALTER TABLE cashbook_accounts
    ADD COLUMN is_transfer INTEGER NOT NULL DEFAULT 0
    CHECK(is_transfer IN (0,1));

COMMIT;
