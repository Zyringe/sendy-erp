-- ============================================================================
-- Rollback for 145 — marketplace shopfronts revert to their channel codes.
--
-- Safe with the application code deployed: this removes ROWS, not the table, so
-- every query keeps working. Attribution reverts to the code Express stamps on
-- the receipt (97 Lazada / 99 NET / 98 BRR).
--
-- Matched on the exact effective_from 145 wrote ('2000-01-01'), so any rule a
-- human later created for these shopfronts through /commission/reassign is left
-- alone.
--
-- ⚠ NOT reverted: `customers.salesperson`. Restoring 97/98/99 would need the
-- pre-145 values, which are not stored — and Express itself already treats the
-- newest shopfront (Tหน้าร้าน) as '00', so '00' is defensible regardless. To
-- undo by hand:
--     UPDATE customers SET salesperson='97' WHERE code='Lหน้าร้าน';
--     UPDATE customers SET salesperson='99' WHERE code='Zหน้าร้าน';
--     UPDATE customers SET salesperson='98' WHERE code='Bหน้าร้าน';
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DELETE FROM commission_customer_reassign
 WHERE customer_code IN ('Lหน้าร้าน', 'Zหน้าร้าน', 'Bหน้าร้าน')
   AND effective_from = '2000-01-01'
   AND to_salesperson = '00';

COMMIT;
