-- Rollback 125: revert the 3 customers to หนุ่ม (31), remove house salesperson.
--
-- All three customers were salesperson '31' before mig 125 (verified
-- 2026-07-03), so the revert target is a literal '31'. DELETE of '00' is safe:
-- no commission_assignments row references it, and at rollback time no
-- customer still points at it (reverted just above). If future '00'-tagged
-- receipts exist by rollback time they are left as-is in received_payments
-- (raw Express truth) — they would simply stop appearing on the commission
-- dashboard again, which is the pre-125 behaviour.
--
-- Run manually; the migration runner does not auto-rollback.

PRAGMA foreign_keys=OFF;
BEGIN;

UPDATE customers SET salesperson = '31'
WHERE code IN ('47ท002', '62ห007', '58บ001');

DELETE FROM salespersons WHERE code = '00';

DELETE FROM applied_migrations WHERE filename = '125_add_house_salesperson_00.sql';

COMMIT;
PRAGMA foreign_keys=ON;
