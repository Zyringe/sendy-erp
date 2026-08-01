-- ============================================================================
-- Migration 146 — สหภัณฑ์ค้าไม้ (62ส008) belongs to the company
--
-- Apply:    restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 146_sahaphan_kamai_to_company.rollback.sql
--
-- Why
--   Put, 2026-08-01: "It is now the customer of company."
--
--   This one was deliberately left out of migration 144. That migration derived
--   its rules from `customers.salesperson`, and this customer is registered to
--   หนุ่ม /31 — who has NOT left — while its receipts carry ภ /33, who has. It
--   surfaced in the follow-up sweep for customers the register-based scope
--   missed, and was held back precisely because moving a customer away from an
--   active rep needed Put's word rather than an inferred rule.
--
--   Its receipts already show two different codes (31 twice, 33 once), so it
--   had changed hands before; this records that it now sits with the company.
--
-- ⚠ NO MONEY MOVES, on any date. All four invoices are settled:
--     2025-04  ภ /33   IV6800098/485/486  due ฿0.00 (Tier C, 0%)
--     2025-11  หนุ่ม /31 IV6801706          due ฿90.85, remaining ฿0.00
--   (sold before SOLD_SETTLED_BEFORE = 2026-02-01, so closed by business rule)
--
-- effective_from = 2026-08-01, the day Put said so. Deliberately NOT
-- backdated: หนุ่ม is an active Tier-B rep who genuinely sold IV6801706, and
-- the rule this project runs on is "he sold it, he earns it" — an order written
-- before the cut stays with the seller. The customer's last invoice was
-- 2025-06-28, so this moves nothing that exists today; it routes future orders
-- to the company instead of silently reviving a departed rep's code.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

INSERT INTO commission_customer_reassign
       (customer_code, to_salesperson, effective_from, note)
SELECT '62ส008', '00', '2026-08-01',
       'สหภัณฑ์ค้าไม้ — เป็นลูกค้าของบริษัทแล้ว (Put 2026-08-01). ใบเสร็จเคยตีทั้ง 31 และ 33; บิลเก่าทั้งหมดปิดยอดแล้ว ไม่กระทบเงิน'
 WHERE EXISTS (SELECT 1 FROM customers     c WHERE c.code = '62ส008')
   AND EXISTS (SELECT 1 FROM salespersons  s WHERE s.code = '00')
   AND NOT EXISTS (SELECT 1 FROM commission_customer_reassign r
                    WHERE r.customer_code = '62ส008');

-- Keep the customer master in step, as the reassignment UI does on every save.
UPDATE customers SET salesperson = '00' WHERE code = '62ส008';

COMMIT;
