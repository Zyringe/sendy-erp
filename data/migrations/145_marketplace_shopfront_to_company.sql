-- ============================================================================
-- Migration 145 — marketplace shopfronts belong to the company
--
-- Apply:    restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 145_marketplace_shopfront_to_company.rollback.sql
--
-- Why
--   Lazada /97, NET /99 and BRR /98 are not people. They are CHANNEL codes,
--   mapping one-to-one onto pseudo-customers the team invented so marketplace
--   revenue could be keyed into Express in bulk (thousands of end buyers, one
--   line per shop):
--
--       Lหน้าร้าน  "Laz หน้าร้านL"  <- Lazada          1,692 invoices
--       Zหน้าร้าน  "สด หน้าร้านS"   <- Shopee (main)   2,564 invoices
--       Bหน้าร้าน  "BRR หน้าร้านB"  <- Shopee บุญเรืองเรือง (closed 2026-01-29)
--
--   Express itself already treats the newest one this way: `Tหน้าร้าน`
--   ("Tik หน้าร้านT", TikTok) carries salesperson '00'. The other three simply
--   predate that convention. This makes them consistent (Put, 2026-07-31).
--
--   Put's instruction was explicit that the CUSTOMER records stay as they are —
--   four separate shopfronts, not merged, not renamed. Only the attribution
--   moves, which is exactly what a reassignment rule does.
--
-- ⚠ NO MONEY MOVES. 97/98/99 are all on Tier C ("รอตัดสินใจ", 0%), so they earn
--   ฿0 either way. ~฿1.14M of marketplace revenue moves from showing under a
--   channel code to showing under บุญสวัสดิ์ นำชัย /00. No commission_payouts
--   exist for 97 or 98; 99 has a single ฿4.20 row from 2024-11 which is left
--   untouched (Put, 2026-07-31: "ถ้าจ่ายคอมแล้วก็ตามนั้น").
--
-- effective_from = 2000-01-01: marketplace sales were never a person's, so the
-- rule covers all history rather than a cut-over date. This differs from
-- migrations 143/144, where a real human sold the earlier orders and keeps them.
--
-- Depends on the customer master containing the shopfront codes. They were
-- absent until the full Express ARMAS import (2026-07-31): 2,665 customers vs
-- the 1,477 loaded in April, which is why this could not be done earlier —
-- `create_customer_reassignment` rejects a customer that does not exist. Guarded
-- below so a database without them applies cleanly instead of failing.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

INSERT INTO commission_customer_reassign
       (customer_code, to_salesperson, effective_from, note)
SELECT c.code, '00', '2000-01-01',
       'ช่องทางขาย marketplace ไม่ใช่เซลส์ — ยอดเป็นของบริษัท เหมือน Tหน้าร้าน (TikTok) ที่ Express ตั้ง 00 อยู่แล้ว (Put 2026-07-31)'
  FROM customers c
 WHERE c.code IN ('Lหน้าร้าน', 'Zหน้าร้าน', 'Bหน้าร้าน')
   AND EXISTS (SELECT 1 FROM salespersons s WHERE s.code = '00')
   AND NOT EXISTS (SELECT 1 FROM commission_customer_reassign r
                    WHERE r.customer_code = c.code);

-- Keep the customer master in step, as the reassignment UI does on every save.
UPDATE customers SET salesperson = '00'
 WHERE code IN ('Lหน้าร้าน', 'Zหน้าร้าน', 'Bหน้าร้าน');

COMMIT;
