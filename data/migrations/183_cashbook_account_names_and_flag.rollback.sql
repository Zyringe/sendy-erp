-- Rollback 183.
--
-- Un-name ONLY the rows this migration named (display_name still equals the
-- exact string it seeded) — an account Put has since renamed via the admin
-- page no longer matches and survives, same precedent as 176's rollback
-- un-stamping only rows matching its own (source, date_start) pair. The flag
-- doesn't need an explicit reset first: DROP COLUMN erases it regardless.
--
-- Run manually; the migration runner does not auto-rollback (matches 182's
-- rollback header).

PRAGMA busy_timeout = 10000;

BEGIN;

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = '392' AND display_name = 'กสิกร 392';

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = 'LEX' AND display_name = 'กสิกร รับเงิน Lazada';

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = 'SPX' AND display_name = 'กสิกร รับเงิน Shopee';

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = 'ชฎามาศ' AND display_name = 'บัญชีส่วนตัว ชฎามาศ';

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = 'กิติยา' AND display_name = 'เงินสำรอง เซียง';

UPDATE cashbook_accounts SET display_name = NULL
 WHERE code = 'Put-Cash' AND display_name = 'เงินสดลิ้นชัก';

ALTER TABLE cashbook_accounts DROP COLUMN income_recorded_elsewhere;

DELETE FROM applied_migrations WHERE filename = '183_cashbook_account_names_and_flag.sql';

COMMIT;
