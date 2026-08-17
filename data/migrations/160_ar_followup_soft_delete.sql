-- ============================================================================
-- 160 — ar_followup_log soft delete (deleted_at / deleted_by) + active index.
--
-- See docs/superpowers/plans/2026-08-15-customers-ar-review-fixes.md Task 4.
-- Deleting an AR outreach row today is a hard DELETE, so the collection
-- history it belonged to disappears with it and nothing records who removed
-- it. This migration lands ONLY the schema; the reader filters, the
-- `delete_outreach()` model function and the route attribution are separate
-- work in the same task.
--
-- Shape copied from the existing call-log soft delete (`customer_call_log`
-- .deleted_at / .deleted_by, consumed by inventory_app/call_card.py) so AR
-- outreach behaves the same way the team already knows: the physical row
-- stays, `deleted_at` stamps `datetime('now','localtime')`, `deleted_by`
-- stamps the actor's username, and every reader adds `deleted_at IS NULL`.
--
-- Three pieces:
--
--   1. `deleted_at TEXT` — NULL means "live". Nullable with no backfill and
--      no DEFAULT: every one of the existing rows is live by definition, and
--      NULL is the value the readers' `deleted_at IS NULL` filter needs.
--
--   2. `deleted_by TEXT` — the actor's `session['username']`, not display
--      text. Nullable and deliberately NOT a FK to `users`: the codebase
--      stores usernames as free text in the sibling audit columns
--      (`ar_followup_log.created_by`, `customer_call_log.deleted_by`) and a
--      FK here would break a rename/removal of a user account, which must
--      never destroy collection history.
--
--   3. `idx_ar_followup_active_customer` — partial index over the LIVE rows
--      only, matching the shape every follow-up reader uses after this task:
--      resolve one customer (`customer_code` when known, `customer` name as
--      the fallback identity — see ar_followup.py::_resolve_target) and read
--      that customer's log newest-first. `WHERE deleted_at IS NULL` keeps
--      the index the size of the live set and lets the planner satisfy the
--      filter, the lookup and the ORDER BY from the index alone.
--      Non-unique on purpose: a customer legitimately has many log rows.
--
-- Invariants this migration does NOT change: no trigger, view or foreign key
-- references ar_followup_log (verified against the live schema — the only
-- sqlite_master objects on that table are itself and its three indexes), so
-- nothing downstream needs rebuilding here, and the three existing indexes
-- (idx_ar_followup_customer, idx_ar_followup_log_date,
-- idx_ar_followup_next_action) are untouched by both directions.
--
-- Apply: the runner applies it (database.py::run_pending_migrations) on the
-- next `init_db()` — i.e. restart the app. To apply by hand against a
-- disposable clone: `sqlite3 <clone.db> ".read data/migrations/160_ar_followup_soft_delete.sql"`
-- Rollback file: `160_ar_followup_soft_delete.rollback.sql`.
--
-- ⚠ NOT re-runnable on its own: `ALTER TABLE ... ADD COLUMN` has no IF NOT
-- EXISTS form, so a second apply fails with `duplicate column name:
-- deleted_at`. The index half IS drop-first, and to re-apply the whole file
-- run 160_ar_followup_soft_delete.rollback.sql FIRST — same shape as
-- migrations 157/158 and their `pre157_conn` / `pre158_conn` test fixtures.
--
-- Rehearsed forward + rollback on a `sqlite3 .backup` copy of a disposable
-- clone before commit, diffing sqlite_master before/after (never mutated a
-- real DB directly — see verification-discipline.md).
-- sqlite3.sqlite_version on this machine: 3.51.0.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE ar_followup_log ADD COLUMN deleted_at TEXT;
ALTER TABLE ar_followup_log ADD COLUMN deleted_by TEXT;

DROP INDEX IF EXISTS idx_ar_followup_active_customer;
CREATE INDEX idx_ar_followup_active_customer
    ON ar_followup_log(customer_code, customer, log_date DESC)
    WHERE deleted_at IS NULL;

-- NOTE: this file does NOT stamp its own applied_migrations row. The runner
-- (database.py::run_pending_migrations) inserts it with `applied_by='auto'`,
-- `sha256` and `duration_ms`; a self-INSERT here would win the filename
-- PRIMARY KEY and make the runner's `INSERT OR IGNORE` a no-op, so the row
-- would land with sha256 NULL. Matches 155/156/158 (only 157 self-stamps).
-- The rollback still DELETEs the row — that half matches 155/156/157/158.

COMMIT;
