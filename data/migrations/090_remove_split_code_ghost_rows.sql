-- ============================================================================
-- Migration 090 — remove split-code ghost rows (B1 fix)
--
-- Apply:    drop this file + 090_remove_split_code_ghost_rows.rollback.sql into
--           data/migrations/ then restart sendy (runner auto-applies on init_db).
-- Rollback: 090_remove_split_code_ghost_rows.rollback.sql (run manually, then
--           DELETE the applied_migrations row — the rollback does both).
--
-- Why
--   Three invoices (IV6900394-7, IV6900391-2, IV6900392-1) were imported in
--   batch 1 and again in batch 18. The batch-1 lines created ghost
--   `sales_transactions` rows with different BSN codes than the canonical
--   batch-18 rows. Express ขายเงินเชื่อ confirms each invoice has exactly ONE
--   physical line item per product — the batch-18 rows are the real ones.
--
--   The ghost rows inflate revenue by ฿1,248 and produced spurious OUT
--   transactions that overcounted stock decrements for products 128, 815, 436.
--
--   Audit trail (from ar_reconcile_express_vs_db_2026-05-29.csv):
--     Ghost ST id 73  — IV6900394-7 / bsn_code=041ม2761 / product_id=128 / ฿1,122
--     Ghost ST id 295 — IV6900391-2 / bsn_code=556ห7000 / product_id=815 /   ฿25
--     Ghost ST id 313 — IV6900392-1 / bsn_code=999อ1501 / product_id=436 /  ฿101
--   Canonical rows (batch 18): ST ids 20597, 37476, 38290.
--
-- What
--   1. DELETE the three ghost transactions rows (stock_levels auto-corrects
--      via the `after_transaction_delete` trigger added in mig 080 — do NOT
--      manually UPDATE stock_levels).
--   2. DELETE the three ghost sales_transactions rows.
--
--   NOTE: The scoping report (track_b_plan_2026-05-29.md) proposed a manual
--   stock_levels recalculate in step 2. That is superseded here by mig 080's
--   trigger which handles stock atomically on each DELETE — manual recalc
--   would double-count.
--
-- Predicates used (STABLE, not raw autoincrement ids)
--   transactions: (reference_no, product_id, txn_type)
--     — for product 436 the ghost is qty=-5 and the canonical is qty=-30;
--       we also gate on quantity_change to avoid touching the canonical row.
--   sales_transactions: (doc_no, bsn_code)
--
-- Idempotency
--   Each DELETE is guarded by EXISTS on the full predicate. A second run on a
--   DB where the rows are already gone is a no-op.
--
-- Tables touched
--   transactions    — 3 rows deleted
--   sales_transactions — 3 rows deleted
--   stock_levels    — auto-updated by after_transaction_delete trigger (mig 080)
--
-- Stock impact (each DELETE reverses the ghost OUT, so quantity INCREASES)
--   product 128 (มือจับ #760 ตัว):      stock += 24  (was -24)
--   product 815 (เหล็กโป้วด้ามดำ 1.5in): stock +=  1  (was -1)
--   product 436 (ลูกกลิ้ง 1in):          stock +=  5  (was -5)
-- ============================================================================

PRAGMA foreign_keys = OFF;

BEGIN;

-- ── Step 1: delete ghost transactions rows ───────────────────────────────────
-- The after_transaction_delete trigger fires for each DELETE and updates
-- stock_levels automatically. Do NOT add any UPDATE/recalc on stock_levels here.

-- Ghost txn for IV6900394-7 (product 128, qty=-24, batch 1)
DELETE FROM transactions
WHERE reference_no = 'IV6900394-7'
  AND product_id   = 128
  AND txn_type     = 'OUT'
  AND EXISTS (
      SELECT 1 FROM sales_transactions
      WHERE doc_no = 'IV6900394-7' AND bsn_code = '041ม2761'
  );

-- Ghost txn for IV6900391-2 (product 815, qty=-1, batch 1)
DELETE FROM transactions
WHERE reference_no = 'IV6900391-2'
  AND product_id   = 815
  AND txn_type     = 'OUT'
  AND EXISTS (
      SELECT 1 FROM sales_transactions
      WHERE doc_no = 'IV6900391-2' AND bsn_code = '556ห7000'
  );

-- Ghost txn for IV6900392-1 (product 436, qty=-5, batch -2)
-- NOTE: canonical row 70947 is also product_id=436 / reference_no=IV6900392-1 but
-- has quantity_change=-30. The quantity_change guard prevents touching it.
DELETE FROM transactions
WHERE reference_no   = 'IV6900392-1'
  AND product_id     = 436
  AND txn_type       = 'OUT'
  AND quantity_change = -5
  AND EXISTS (
      SELECT 1 FROM sales_transactions
      WHERE doc_no = 'IV6900392-1' AND bsn_code = '999อ1501'
  );

-- ── Step 2: delete ghost sales_transactions rows ─────────────────────────────

DELETE FROM sales_transactions
WHERE doc_no = 'IV6900394-7' AND bsn_code = '041ม2761';

DELETE FROM sales_transactions
WHERE doc_no = 'IV6900391-2' AND bsn_code = '556ห7000';

DELETE FROM sales_transactions
WHERE doc_no = 'IV6900392-1' AND bsn_code = '999อ1501';

COMMIT;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO applied_migrations(filename)
    VALUES ('090_remove_split_code_ghost_rows.sql');
