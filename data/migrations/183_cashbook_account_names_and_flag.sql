-- 183 — cashbook accounts: readable names + "รายรับบันทึกที่อื่น" flag.
--
-- Issue #534. `cashbook_accounts.display_name` has existed since migration 055
-- but nothing has ever written to it (verified 2026-09-15: all 7 accounts on
-- both prod and the local dev DB carry '' /NULL) — the admin page never
-- exposed the field. This migration seeds it and adds the one new column the
-- issue needs: a boolean marking an account whose INCOME is tracked outside
-- the cashbook entirely (`ชฎามาศ` receives all No-VAT sales money, none of
-- which is keyed here — Put, 2026-09-15), so its all-history balance
-- (−฿1,743,429.31 on prod 2026-09-14) means nothing and must stop being
-- summed into the dashboard's คงเหลือ headline or shown on its own account
-- page. Its EXPENSES stay fully counted everywhere (รายจ่าย, category
-- summary, P&L) — only the INCOME-shaped "balance" reading is suppressed;
-- see blueprints/cashbook.py's dashboard() and templates/cashbook/*.html.
--
-- Column semantics: `income_recorded_elsewhere` (NOT `is_income_elsewhere`,
-- to read as a fact about the account rather than a state) mirrors the
-- existing `is_transfer` CHECK(IN (0,1)) shape but is a DIFFERENT axis —
-- `904` is is_transfer=1 and income_recorded_elsewhere=0; `ชฎามาศ` is the
-- reverse. An account could in principle be both; nothing here forbids it.
--
-- `392` is SCB (ไทยพาณิชย์), not กสิกร (Put, 2026-09-15, correcting the
-- issue table's own text) — `bank_name` is untouched by this migration
-- either way; only `display_name` carries the bank's short name for
-- at-a-glance reading on the dashboard.
--
-- Seed is idempotent by DESIGN, not just by the runner's filename bookkeeping:
-- every display_name UPDATE is guarded `WHERE display_name IS NULL OR
-- display_name = ''`, so a hand re-run after Put has renamed an account via
-- the new admin field leaves his name alone (issue's explicit requirement).
-- `904` gets neither a name nor the flag — "leave it" per the issue table.
--
-- ⚠ NOT re-runnable on its own via a second `sqlite3 < file` apply: `ALTER
-- TABLE ... ADD COLUMN` has no IF NOT EXISTS form, so re-applying dies at
-- `duplicate column name: income_recorded_elsewhere` — same documented
-- shape as 159/176/178. The runner never repeats an applied migration (keyed
-- by filename in applied_migrations), so this only matters for a hand
-- rehearsal: run 183_cashbook_account_names_and_flag.rollback.sql first.
--
-- No audit triggers exist on cashbook_accounts (checked: 0 hits for the
-- table in data/schema.sql's trigger blocks) — nothing to recreate.
--
-- Rehearsed forward + rollback + forward on a `sqlite3 .backup` copy of
-- ~/sendy-prod-backups/pre_conversion_533.db before commit (never `cp` —
-- sendy holds a WAL); results pasted into the PR body. sqlite3.sqlite_version
-- on this machine: 3.51.0. Prod runs 3.46.1.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

ALTER TABLE cashbook_accounts ADD COLUMN income_recorded_elsewhere INTEGER
    NOT NULL DEFAULT 0 CHECK (income_recorded_elsewhere IN (0,1));

UPDATE cashbook_accounts SET display_name = 'ไทยพาณิชย์ 392'
 WHERE code = '392' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET display_name = 'กสิกร รับเงิน Lazada'
 WHERE code = 'LEX' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET display_name = 'กสิกร รับเงิน Shopee'
 WHERE code = 'SPX' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET display_name = 'บัญชีส่วนตัว ชฎามาศ'
 WHERE code = 'ชฎามาศ' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET display_name = 'เงินสำรอง เซียง'
 WHERE code = 'กิติยา' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET display_name = 'เงินสดลิ้นชัก'
 WHERE code = 'Put-Cash' AND (display_name IS NULL OR display_name = '');

UPDATE cashbook_accounts SET income_recorded_elsewhere = 1
 WHERE code = 'ชฎามาศ';

-- Self-record INSIDE this transaction — same reasoning as 178/159/176: this
-- file is not re-runnable, so if the runner crashed between executescript()
-- committing and its own separate applied_migrations INSERT, the next boot
-- would retry it and die on the duplicate column. INSERT OR IGNORE makes
-- this safe whether or not the runner's own bookkeeping INSERT also fires.
INSERT OR IGNORE INTO applied_migrations (filename, applied_by)
VALUES ('183_cashbook_account_names_and_flag.sql', 'auto');

COMMIT;
