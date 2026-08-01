-- ============================================================================
-- 147 — conversion_cost_log.run_token: the replay key for a conversion run.
--
-- PR #345 shipped the replay guard by letting the per-form nonce STAND IN for
-- a blank เลขที่เอกสาร, storing it in reference_no. That has a hole: when the
-- operator types a เลขที่เอกสาร the nonce has nowhere to live, so the guard
-- turned itself off and a re-submitted POST converted twice (Codex review of
-- PR #345, 2026-08-01).
--
-- The two are different things and need different homes:
--   reference_no — the operator's business document number. May legitimately
--                  repeat across separate runs. Customer/ops facing.
--   run_token    — one page render. Never repeats. Machine-only.
--
-- Additive column, so historical rows keep NULL. The unique index is PARTIAL
-- (WHERE run_token IS NOT NULL) because SQLite treats NULLs as distinct but
-- a partial index states the intent plainly: at most one run per token, and
-- the pre-147 rows are exempt rather than accidentally allowed.
--
-- The index is also the lookup path for the guard's SELECT, so this is not
-- redundant with the app-level check — it is the constraint that makes the
-- check true no matter which code path writes the row.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE conversion_cost_log ADD COLUMN run_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_cost_log_run_token
    ON conversion_cost_log(run_token)
 WHERE run_token IS NOT NULL;

COMMIT;
