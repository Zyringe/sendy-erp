-- ============================================================================
-- Rollback for 146 — สหภัณฑ์ค้าไม้ reverts to its stamped rep code.
--
-- Safe with the application code deployed: removes a ROW, not the table.
-- Attribution reverts to whatever Express stamps on the receipt.
--
-- ⚠ NOT reverted: `customers.salesperson`, which goes back to '31' (หนุ่ม)
-- only if you do it by hand — the pre-146 value is not stored:
--     UPDATE customers SET salesperson = '31' WHERE code = '62ส008';
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

DELETE FROM commission_customer_reassign
 WHERE customer_code = '62ส008'
   AND effective_from = '2026-08-01'
   AND to_salesperson = '00';

COMMIT;
