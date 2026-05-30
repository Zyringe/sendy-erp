-- ============================================================================
-- Migration 091 — formalize purchase_transactions.line_seq
--
-- Apply:    drop this file + 091_purchase_transactions_line_seq.rollback.sql into
--           data/migrations/ then restart sendy (runner auto-applies on init_db).
-- Rollback: 091_purchase_transactions_line_seq.rollback.sql (run manually — it
--           rebuilds purchase_transactions without line_seq and removes the
--           applied_migrations row).
--
-- Why
--   Sales doc_nos carry a line suffix ("IV6900503-  1", "...-  2") so each
--   physical line item is uniquely identifiable by (doc_no). Purchase doc_nos
--   do NOT carry that suffix — a single purchase doc_no can legitimately repeat
--   the same bsn_code across multiple physical lines. As a result 261 purchase
--   lines share a non-unique (doc_no, bsn_code) key. A loader needs a stable
--   per-line discriminator to give those rows unique identity; `line_seq`
--   (1, 2, 3, ... within a doc) provides it.
--
--   sales_transactions does NOT get line_seq — the asymmetry is justified by the
--   doc_no suffix above. This migration touches purchase_transactions ONLY.
--
-- What
--   ADD COLUMN purchase_transactions.line_seq INTEGER NOT NULL DEFAULT 1.
--   On fresh/prod DBs the DEFAULT backfills every existing row to 1; the loader
--   then renumbers true duplicates. No backfill UPDATE is needed here.
--
-- How / idempotency  (READ THIS — non-standard)
--   A loader script already ran this exact ALTER directly against the DEV DB
--   (an un-migrated schema change), so on dev the column is ALREADY present.
--   SQLite `ADD COLUMN` has no `IF NOT EXISTS`, and the migration runner
--   (database.py::run_pending_migrations) executes the whole .sql via
--   `executescript` — plain SQL only, no procedural/PRAGMA-gated branching —
--   and a raised error aborts boot. So a re-run of the ALTER on dev would throw
--   "duplicate column name: line_seq" and break startup.
--
--   Resolution (path b, the house-approved one for "already-present on some
--   envs" where the runner can't branch):
--     * This forward file is the CANONICAL ALTER. Fresh/prod DBs (column
--       absent) run it cleanly and self-record below.
--     * The DEV DB, where the column already exists, has its applied_migrations
--       row inserted MANUALLY (out-of-band, alongside shipping this migration)
--       so the runner sees 091 as already-applied and never re-runs the ALTER.
--   Net effect: every environment converges on the same schema + the same
--   applied_migrations bookkeeping, without the runner ever hitting the
--   duplicate-column error.
--
-- Tables touched
--   purchase_transactions — 1 column added (line_seq)
--   No triggers or views reference purchase_transactions (verified 2026-05-30:
--   sqlite_master has zero trigger/view rows mentioning it), so ADD COLUMN
--   cannot drift any dependent object.
--
-- FK hazard: PRAGMA foreign_keys = OFF before BEGIN, matching recent migs. This
-- mig adds no FK and rebuilds no table; the pragma keeps the ceremony
-- consistent and harmless.
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

ALTER TABLE purchase_transactions
    ADD COLUMN line_seq INTEGER NOT NULL DEFAULT 1;

COMMIT;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO applied_migrations(filename)
    VALUES ('091_purchase_transactions_line_seq.sql');
