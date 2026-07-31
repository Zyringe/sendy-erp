-- ============================================================================
-- Migration 144 — move five departed reps' customers to the company
--
-- Apply:    restart the server (database.py::init_db() auto-applies on boot)
-- Rollback: 144_reassign_departed_reps_to_company.rollback.sql
--
-- Why
--   Put, 2026-07-31: น้อย /02 stopped selling on 1 Feb 2026, and ส /01,
--   Kน /07, วิชัย /13, ภ /33 have left. Express keeps stamping their route
--   codes on receipts regardless — น้อย's code was still on invoices through
--   25 Jul 2026 — so their customers must be routed to the company.
--
--   Same mechanism as migration 143 (see `commission_attribution.py` for the
--   invoice-date semantics: an order written BEFORE the cut stays with the rep
--   who sold it; the latest rule at or before the invoice date wins).
--
-- ⚠ NO MONEY MOVES. All five are on Tier C ("รอตัดสินใจ", 0%), so they earn
--   ฿0 either way — verified across every 2026 cycle before and after. This
--   changes ATTRIBUTION only: ฿1,175,377 of น้อย's post-Feb sales now show
--   under บุญสวัสดิ์ นำชัย /00 instead of under him.
--
-- Cut dates — น้อย's is Put's own; the rest are the day after each rep's LAST
-- invoice, so nothing they actually sold is taken from them. For those four
-- that moves zero existing bills (nothing exists after their last sale); the
-- rules exist so a returning customer routes to the company rather than
-- silently reviving a departed rep.
--
--     02  น้อย    2026-02-01   (Put: stopped selling 1 Feb 2026)
--     13  วิชัย    2025-11-22   (last invoice 2025-11-21)
--     01  ส       2025-10-03   (last invoice 2025-10-02)
--     07  Kน      2025-03-23   (last invoice 2025-03-22)
--     33  ภ       2024-03-07   (last invoice 2024-03-06)
--
-- ⚠ Reaches into two cycles already paid to น้อย (2026-03 ฿80.03, 2026-04
--   ฿348.95), which will therefore read as overpaid. Put ruled 2026-07-31:
--   "ถ้าจ่ายคอมแล้วก็ตามนั้น" — no clawback. Recorded here so the negative
--   figure is explainable later rather than looking like a bug.
--
-- Derived from `customers.salesperson` rather than a hard-coded id list so the
-- same file reproduces the same result on any database. Re-runnable: rows that
-- already exist are skipped, so applying to a DB where the work was done by
-- hand is a no-op.
--
-- NOT covered here (blocked, tracked separately): Lazada /97, NET /99 and
-- BRR /98 are marketplace CHANNELS mapping to customer codes `Lหน้าร้าน`,
-- `Zหน้าร้าน`, `Bหน้าร้าน` — none of which exist in `customers`. 34 such
-- orphan codes carry 4,718 invoices worth ฿3.1M (mostly Lao export accounts).
-- They need adding to the customer master before any rule can reference them.
-- ============================================================================

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

INSERT INTO commission_customer_reassign
       (customer_code, to_salesperson, effective_from, note)
SELECT c.code, '00', v.eff, v.note
  FROM customers c
  JOIN (SELECT '02' AS sp, '2026-02-01' AS eff,
               'น้อย /02 หยุดขาย 1 ก.พ. 2026 (Put 2026-07-31) — ลูกค้าเป็นของบริษัท' AS note
        UNION ALL SELECT '13', '2025-11-22', 'วิชัย /13 ออกแล้ว — ลูกค้าเป็นของบริษัท (Put 2026-07-31)'
        UNION ALL SELECT '01', '2025-10-03', 'ส /01 ออกแล้ว — ลูกค้าเป็นของบริษัท (Put 2026-07-31)'
        UNION ALL SELECT '07', '2025-03-23', 'Kน /07 ออกแล้ว — ลูกค้าเป็นของบริษัท (Put 2026-07-31)'
        UNION ALL SELECT '33', '2024-03-07', 'ภ /33 ออกแล้ว — ลูกค้าเป็นของบริษัท (Put 2026-07-31)'
       ) v ON v.sp = c.salesperson
 WHERE EXISTS (SELECT 1 FROM salespersons s WHERE s.code = '00')
   AND NOT EXISTS (SELECT 1 FROM commission_customer_reassign r
                    WHERE r.customer_code = c.code
                      AND r.effective_from = v.eff);

-- Keep the customer master in step with the rule, exactly as the reassignment
-- UI does on every save (models.commission.sync_customer_master_salesperson).
UPDATE customers SET salesperson = '00'
 WHERE salesperson IN ('02', '13', '01', '07', '33');

COMMIT;
