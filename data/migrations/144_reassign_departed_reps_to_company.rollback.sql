-- ============================================================================
-- Rollback for 144 — remove the five departed reps' reassignment rules.
--
-- Safe to run with the application code deployed: unlike 143's rollback this
-- drops ROWS, not the table, so every query keeps working. Attribution simply
-- reverts to the rep code Express stamps on the receipt.
--
-- Matched on the exact (to_salesperson, effective_from) pairs 144 wrote, so
-- rules created by hand through /commission/reassign — including the four
-- customers seeded by migration 143, which use different dates — are left
-- alone.
--
-- ⚠ NOT reverted: `customers.salesperson`. The pre-144 owner is not stored
-- anywhere, so restoring it would mean guessing, and a wrong guess overwrites
-- a correct value with a stale one (same reasoning as
-- models.commission.sync_customer_master_salesperson). The reps are gone, so
-- '00' stays right regardless. To genuinely undo it you need the pre-migration
-- values — take them from a backup:
--     SELECT code, salesperson FROM customers WHERE salesperson IN ('02','13','01','07','33');
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DELETE FROM commission_customer_reassign
 WHERE to_salesperson = '00'
   AND effective_from IN ('2026-02-01', '2025-11-22', '2025-10-03',
                          '2025-03-23', '2024-03-07');

COMMIT;
