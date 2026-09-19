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
-- ── Precondition: ABORT unless 904 exists and is inactive ─────────────────
-- An ACTIVE 904 with is_transfer = 0 is an ordinary pay-from account. That is
-- the one state this file must never produce, so it refuses rather than
-- flipping. It also refuses a DB with no 904 at all: on prod that would mean
-- the account was renamed, and silently doing nothing there would leave the
-- ADR effect un-applied with nothing saying so. (A fresh DB never runs this:
-- it is built from schema.sql and every migration is stamped.)
-- Mechanism is 177's: BEFORE DELETE fires once per row, so DELETE on an empty
-- precheck table is a no-op and on a non-empty one aborts. The runner
-- (database.py::run_pending_migrations) rolls back, re-raises and does NOT
-- stamp applied_migrations, so a refused run leaves the DB untouched and the
-- next boot tries again.
--
-- RECOVERY when it aborts: look at the row first —
--     SELECT id, code, is_active, is_transfer FROM cashbook_accounts
--      WHERE code = '904';
-- No row: find what 904 was renamed to and ask Put before editing this file.
-- is_active = 1: someone re-activated it. Ask Put whether it should be
-- active; if not, set is_active = 0 on the admin accounts page and reboot.
-- ⚠ The shared LOCAL dev DB held 904 active on 2026-09-19 (it predates Put's
-- 09-15 deactivation on prod): refresh it from a prod snapshot rather than
-- hand-editing it.
--
-- Re-runnable: a second apply finds 904 already `is_transfer = 0` and
-- inactive, passes the precondition, and the UPDATE changes nothing.
-- `updated_at` is deliberately left alone so the rollback restores the row
-- byte-identically; applied_migrations is the record of this change.
--
-- Rehearsed forward + rollback + forward on a `sqlite3 .backup` copy of
-- ~/sendy-prod-backups/prod-2026-09-19T1536Z-pre-592.db before commit.

PRAGMA busy_timeout = 10000;

BEGIN;

DROP TABLE IF EXISTS temp._mig188_precheck;
CREATE TEMP TABLE _mig188_precheck AS
SELECT 'no account 904' AS why
 WHERE NOT EXISTS (SELECT 1 FROM cashbook_accounts WHERE code = '904')
UNION ALL
SELECT '904 is active'
  FROM cashbook_accounts
 WHERE code = '904' AND is_active <> 0;

CREATE TEMP TRIGGER _mig188_precondition_guard
BEFORE DELETE ON _mig188_precheck
BEGIN
  SELECT RAISE(ABORT, 'mig 188 precondition FAILED: account 904 must exist and be inactive (is_active = 0) before is_transfer comes off. See the RECOVERY note in this migration header.');
END;

DELETE FROM _mig188_precheck;
DROP TRIGGER _mig188_precondition_guard;
DROP TABLE _mig188_precheck;

UPDATE cashbook_accounts
   SET is_transfer = 0
 WHERE code = '904'
   AND is_transfer = 1
   AND is_active = 0;

COMMIT;
