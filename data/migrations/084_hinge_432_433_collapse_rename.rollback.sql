-- 084_hinge_432_433_collapse_rename.rollback.sql
--
-- Restores pre-mig-084 state from migration_084_snapshot, then drops it.
-- Run manually; the migration runner does not auto-rollback.
--
-- Stock invariant
--   Step 1's product_id rollback fires mig 080's after_transaction_update,
--   which reverses the forward stock_levels movement. Step 3 then recomputes
--   stock_levels from the ledger as a trigger-agnostic safety net (the
--   documented drift-recovery flow from sendy_erp/CLAUDE.md).

BEGIN;

-- ── 1. Restore pid 85's ADJUST row product_id + note ──────────────────────
UPDATE transactions
   SET product_id = (SELECT CAST(orig_value AS INTEGER)
                       FROM migration_084_snapshot
                      WHERE kind = 'txn' AND row_key = transactions.id
                        AND field = 'product_id'),
       note       = (SELECT orig_value
                       FROM migration_084_snapshot
                      WHERE kind = 'txn' AND row_key = transactions.id
                        AND field = 'note')
 WHERE id IN (SELECT row_key FROM migration_084_snapshot WHERE kind = 'txn');

-- ── 2. Restore pid 1393 + 1395 column values ──────────────────────────────
UPDATE products
   SET product_name = (SELECT orig_value FROM migration_084_snapshot
                        WHERE kind = 'pcol' AND row_key = products.id
                          AND field = 'product_name'),
       unit_type    = (SELECT orig_value FROM migration_084_snapshot
                        WHERE kind = 'pcol' AND row_key = products.id
                          AND field = 'unit_type'),
       size         = (SELECT orig_value FROM migration_084_snapshot
                        WHERE kind = 'pcol' AND row_key = products.id
                          AND field = 'size')
 WHERE id IN (1393, 1395);

-- ── 3. Re-activate pid 85 ─────────────────────────────────────────────────
UPDATE products
   SET is_active = COALESCE(
         (SELECT CAST(orig_value AS INTEGER) FROM migration_084_snapshot
           WHERE kind = 'pcol' AND row_key = 85 AND field = 'is_active'),
         1)
 WHERE id = 85;

-- ── 4. Recompute stock_levels from ledger (drift-recovery safety net) ─────
DELETE FROM stock_levels WHERE product_id IN (85, 1393);
INSERT INTO stock_levels (product_id, quantity)
SELECT product_id, COALESCE(SUM(quantity_change), 0)
  FROM transactions
 WHERE product_id IN (85, 1393)
 GROUP BY product_id;

-- ── 5. Drop snapshot table ────────────────────────────────────────────────
DROP TABLE IF EXISTS migration_084_snapshot;

COMMIT;
