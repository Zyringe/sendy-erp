-- 083_collapse_seed_duplicates.rollback.sql
--
-- Restores pre-mig-083 state from migration_083_snapshot, then drops it.
-- Run manually; the migration runner does not auto-rollback.
--
-- Stock invariant
--   Step 1's UPDATE fires mig 080's `after_transaction_update` trigger,
--   which reverses the forward stock_levels movement (decrement canonical
--   pid, re-increment dormant pid). Step 3 then recomputes stock_levels
--   from the ledger as a trigger-agnostic safety net — converges to the
--   same values the triggers should already have produced.

BEGIN;

-- ── 1. Restore product_id and note on the 4 ADJUST rows ───────────────────
-- Snapshot lookup (not arithmetic), so restored values match byte-for-byte
-- what was there pre-mig-083 even if anyone manually tweaked rows after.
UPDATE transactions
   SET product_id = (SELECT orig_product_id FROM migration_083_snapshot
                       WHERE txn_id = transactions.id),
       note       = (SELECT orig_note       FROM migration_083_snapshot
                       WHERE txn_id = transactions.id)
 WHERE id IN (SELECT txn_id FROM migration_083_snapshot);

-- ── 2. Re-activate the dormant pids ───────────────────────────────────────
UPDATE products
   SET is_active = 1
 WHERE id IN (649, 650, 651, 787);

-- ── 3. Recompute stock_levels from ledger (drift-recovery safety net) ─────
-- Documented flow in sendy_erp/CLAUDE.md: DELETE + INSERT FROM SUM. Covers
-- both mig-080-present (no-op) and mig-080-absent (clamps to ledger SUM)
-- environments. Restricted to the 8 affected pids; nothing else touched.
DELETE FROM stock_levels
 WHERE product_id IN (649, 650, 651, 787, 1325, 1327, 1323, 1527);

INSERT INTO stock_levels (product_id, quantity)
SELECT product_id, COALESCE(SUM(quantity_change), 0)
  FROM transactions
 WHERE product_id IN (649, 650, 651, 787, 1325, 1327, 1323, 1527)
 GROUP BY product_id;

-- ── 4. Drop snapshot table ────────────────────────────────────────────────
DROP TABLE IF EXISTS migration_083_snapshot;

COMMIT;
