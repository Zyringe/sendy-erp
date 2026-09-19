-- 188 — account 904 stops being a transfer account; it stays deactivated.
--
-- ADR 0017 / issue #594. `is_transfer` means exactly one thing: the account
-- is a conduit whose movements are not operating activity. 904 is the
-- pre-cashbook history bucket (ธ.ค. 68 ถึง มี.ค. 69), and the flag was doing a
-- second job on it: hiding that era. That hid ฿205,278.91 of real operating
-- expense from every report. Its genuine transfers stay excluded by the
-- `เงินทุน/เงินโอน` category, exactly as every other account's are, so this
-- cannot leak transfers anywhere (every reader filters the category in the
-- same WHERE — census in tests/test_is_transfer_population_coverage.py).
--
-- `is_active = 0` is NOT touched. It is Put's own guard from 2026-09-15
-- (decisions/log.md) and after this migration it is the ONLY thing keeping
-- 904 out of the pay-from pickers and the salary / commission posting paths,
-- which all require `is_active = 1` AND not a transfer account
-- (tests/test_594_account_populations.py pins each of them).
--
-- Measured effect on /accounting (prod snapshot 2026-09-19 15:36Z, mig 187,
-- through get_accounting_summary itself): 2026-02 ค่าใช้จ่ายดำเนินงาน
-- +29,678.91, 2026-03 +8,100.00, 2026-01 unchanged with its ฿167,500.00 of
-- FY2568 bonuses landing on ค่าใช้จ่ายของงวดก่อน (stamped by 187).
--
-- ── Why the flip is CONDITIONAL, and never an abort ──────────────────────
-- An ACTIVE 904 with is_transfer = 0 is an ordinary pay-from account, so the
-- flip happens only where 904 is inactive. Anywhere else (904 still active,
-- or no 904 at all) this file is a stamped no-op.
-- It deliberately does NOT abort on those states. A migration that raises
-- crashes boot: database.run_pending_migrations re-raises out of init_db(),
-- which runs at `import app`, and under `gunicorn --preload` the master then
-- exits before it ever listens (measured 2026-09-19 against this file's first
-- version on a snapshot copy with 904 active: rc 1, no "Listening at").
-- The shared local dev DB, and every worktree seeded from it, still had 904
-- active on 2026-09-19 — an abort would have stopped all of them booting.
-- The cost of that choice: if 904 were re-activated on prod before this
-- deploys, the ADR effect would silently not land. That is what the
-- post-deploy check is for:
--     SELECT is_active, is_transfer FROM cashbook_accounts WHERE code = '904';
--         -- expect 0, 0
--     SELECT filename FROM applied_migrations
--      WHERE filename = '188_unflag_904_is_transfer.sql';   -- expect 1 row
-- A stamped 188 with 904 still `is_transfer = 1` means 904 was active at
-- deploy time: ask Put whether it should be, and if not, deactivate it and
-- un-flag it by hand with this file's UPDATE.
--
-- Re-runnable: a second apply changes nothing. `updated_at` is deliberately
-- left alone so the rollback restores the row byte-identically;
-- applied_migrations is the record of this change.
--
-- Rehearsed forward + rollback + forward on a `sqlite3 .backup` copy of
-- ~/sendy-prod-backups/prod-2026-09-19T1536Z-pre-592.db before commit.

PRAGMA busy_timeout = 10000;

BEGIN;

UPDATE cashbook_accounts
   SET is_transfer = 0
 WHERE code = '904'
   AND is_transfer = 1
   AND is_active = 0;

COMMIT;
