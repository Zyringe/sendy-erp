-- Rollback 189: salesperson 03 back to Tier C (the 0% placeholder), with the note
-- mig 014 seeded.
--
-- ⚠ This does NOT undo the post-deploy script
-- (scripts/2026_09_19_589_commission_03_backrecord.py). If that script ran, 03 holds
-- commission_payouts rows, and the September one owns a locked cashbook row. On
-- Tier C every 03 invoice has commission_due = 0, so those payouts read as paid
-- against nothing. To undo them as well, void each 03 payout on /commission/payouts
-- (delete_payout also removes the September cashbook row) and re-key cashbook row
-- 845 by hand; the script's output lists what it wrote.
--
-- Run manually; the migration runner does not auto-rollback.
PRAGMA busy_timeout = 10000;

BEGIN;

UPDATE commission_assignments
   SET tier_id    = (SELECT id FROM commission_tiers WHERE code = 'C'),
       note       = 'ท /03 — TBD',
       updated_at = datetime('now', 'localtime')
 WHERE salesperson_code = '03';

DELETE FROM applied_migrations WHERE filename = '189_commission_03_tier_a.sql';

COMMIT;
