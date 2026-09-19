-- Rollback 185.
--
-- Un-stamp ONLY the rows this migration stamped — the same explicitly named
-- (txn_date, amount, description) set, and only while they still carry
-- exactly the value 185 wrote ('2025'). A row someone has since re-stamped
-- to a different period is newer than this migration and survives, same
-- precedent as 183's rollback un-naming only its own exact strings and 176's
-- un-stamping only its own (source, date_start) pair.
--
-- The UPDATE is not strictly required — DROP COLUMN erases the values
-- regardless, exactly as 183's rollback notes for its flag. It is here so
-- the intermediate state is correct if someone stops after it, and so the
-- rollback says out loud which rows this migration owned.
--
-- DROP COLUMN restores `cashbook_transactions`'s table SQL byte-identically
-- (verified on both sqlite 3.51.0 and a prod-derived copy) — a clean
-- rollback means sqlite_master comes back the same, not merely that the
-- column is gone.
--
-- Run manually; the migration runner does not auto-rollback (matches 182's
-- and 183's rollback headers).

PRAGMA busy_timeout = 10000;

BEGIN;

UPDATE cashbook_transactions
   SET belongs_to_period = NULL
 WHERE belongs_to_period = '2025'
   AND direction = 'expense'
   AND category  = 'จ่ายค่าโบนัส'
   AND (txn_date, amount, description) IN (
                   SELECT '2026-01-31',  11000.0, 'โบนัสพี่ลี'
        UNION ALL  SELECT '2026-01-31',  12000.0, 'โบนัสพี่ต๋อง'
        UNION ALL  SELECT '2026-01-31',  18000.0, 'โบนัสพี่หนุ่ม'
        UNION ALL  SELECT '2026-01-31',   7500.0, 'โบนัสพี่ติม'
        UNION ALL  SELECT '2026-01-31',  19000.0, 'โบนัสพี่แต'
        UNION ALL  SELECT '2026-01-31', 100000.0, 'โบนัสพี่ต๋อ'
        UNION ALL  SELECT '2026-03-09', 200000.0, 'โบนัสปี 68'
   );

ALTER TABLE cashbook_transactions DROP COLUMN belongs_to_period;

DELETE FROM applied_migrations WHERE filename = '185_prior_period_expense.sql';

COMMIT;
