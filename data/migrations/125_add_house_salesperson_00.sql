-- Migration 125: add house salesperson '00' (บุญสวัสดิ์ นำชัย) + move 3 customers
--
-- Business context (Put, 2026-07-03): 3 customers previously serviced by
-- หนุ่ม (31) become company/house accounts from **June 2026 forward** — the
-- company keeps the margin, no rep commission. Put already created the
-- salesperson in Express as code '00'; future Express files stamp '00' on
-- these customers' receipts.
--   47ท002  หจก. ไทยทวีกิจ
--   62ห007  หนองแสงอลูมินั่ม
--   58บ001  บัญชารถเกี่ยว
--
-- Commission is RECEIPT-driven (received_payments.salesperson, copied from the
-- Express file at import — commission.py _BASE_QUERY). This migration does NOT
-- and cannot move any commission by itself. It only:
--   1. makes Sendy RECOGNIZE code '00' — the commission dashboard lists ONLY
--      codes present in salespersons (app.py ~L2265); a receipt code absent
--      from this table is silently dropped from the page. Registering '00'
--      makes the future June+ '00' receipts show as their own house line.
--   2. aligns the Sendy customer master with Express (cosmetic; no commission
--      effect — the fallback in get_invoices_for_salesperson only bites when a
--      receipt is missing, which is not the case here).
--
-- 0% by construction: '00' gets NO commission_assignments row, so the engine
-- resolves tier=None → 0 commission. Nobody ever earns on a house account.
--
-- June-forward is satisfied automatically: verified 2026-07-03 there are ZERO
-- receipts for these 3 customers dated >= 2026-05-01 (latest is 2026-04-20),
-- all of which are already settled (<= SETTLED_THROUGH 2026-04-30). The 29
-- existing '31'-tagged receipts are LEFT UNTOUCHED (historical truth).
--
-- Idempotent: INSERT guarded by WHERE NOT EXISTS; the UPDATE is a no-op on
-- rerun once the 3 rows already read '00'.

PRAGMA foreign_keys=OFF;
BEGIN;

-- 1. Register the house salesperson. Name follows the "<name> /NN" convention
--    of the seeded rows ('หนุ่ม /31', 'น้อย /02'). is_active=1 so it is also
--    a valid target for /customers/bulk-reassign.
INSERT INTO salespersons (code, name, is_active)
SELECT '00', 'บุญสวัสดิ์ นำชัย /00', 1
WHERE NOT EXISTS (SELECT 1 FROM salespersons WHERE code = '00');

-- 2. Align the customer master with Express (no commission effect).
UPDATE customers SET salesperson = '00'
WHERE code IN ('47ท002', '62ห007', '58บ001');

COMMIT;
PRAGMA foreign_keys=ON;
