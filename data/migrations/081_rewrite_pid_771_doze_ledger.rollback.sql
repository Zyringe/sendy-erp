-- 081_rewrite_pid_771_doze_ledger.rollback.sql
-- Restores pre-mig-081 state from snapshot tables, then drops them.
-- Run manually; migration runner does not auto-rollback.
--
-- Stock invariant — rollback is trigger-agnostic via step 3 recompute:
--
-- Case A: mig 080 (`after_transaction_update` + `_delete`) active
--   - Step 1 UPDATEs fire `after_transaction_update`, net delta to
--     stock_levels(771) = sum(NEW - OLD) = -44 (reverse of mig 081's +44)
--   - Step 2 INSERT fires the canonical `after_transaction_insert`, delta +44
--   - stock_levels(771) lands at 48 organically
--
-- Case B: mig 080 absent (parallel-deploy scenario)
--   - Step 1 UPDATEs don't fire any business trigger → stock_levels unchanged
--   - Step 2 INSERT still fires canonical `after_transaction_insert` (defined
--     in database.py schema, NOT mig 080) → stock_levels += 44 → drift to 92
--   - Step 3 recompute clamps stock_levels back to SUM(quantity_change) = 48
--
-- Either way: stock_levels(771) = 48 after this rollback completes.

BEGIN;

-- ── 1. Restore quantity_change from snapshot on the 19 affected rows ───────
-- Using snapshot lookup (not arithmetic divide) so the restored values match
-- byte-for-byte what was there pre-mig-081, even if anyone manually tweaked
-- rows post-mig-081.
UPDATE transactions
   SET quantity_change = (
       SELECT prior_qty_change
       FROM migration_081_snapshot
       WHERE migration_081_snapshot.id = transactions.id
   )
 WHERE id IN (SELECT id FROM migration_081_snapshot);

-- ── 2. Re-INSERT the MIG_078 ADJUST row from snapshot ─────────────────────
-- The canonical `after_transaction_insert` trigger (database.py schema, NOT
-- mig 080) auto-increments stock_levels by +44 — but see step 3 below for
-- the safety-net reconcile that makes this rollback trigger-agnostic.
INSERT OR IGNORE INTO transactions
       (id, product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
SELECT  id, product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at
FROM migration_081_deleted_rows;

-- ── 3. Recompute stock_levels(771) from the restored ledger ────────────────
-- Trigger-agnostic safety net. With mig 080 active this is a no-op (the
-- update/delete/insert triggers already landed stock_levels at 48). Without
-- mig 080, step 2's canonical insert trigger leaves stock_levels at 92; this
-- recompute clamps it back to the authoritative ledger SUM (= 48).
--
-- This is the documented drift-recovery flow from `sendy_erp/CLAUDE.md`:
-- `DELETE FROM stock_levels WHERE product_id=?; INSERT FROM SUM(quantity_change)`.
-- Safe alongside mig 080's triggers because it converges to the same value
-- the triggers already produced.
DELETE FROM stock_levels WHERE product_id = 771;
INSERT INTO stock_levels (product_id, quantity)
SELECT 771, COALESCE(SUM(quantity_change), 0)
  FROM transactions
 WHERE product_id = 771;

-- ── 4. Drop snapshot tables ────────────────────────────────────────────────
DROP TABLE IF EXISTS migration_081_snapshot;
DROP TABLE IF EXISTS migration_081_deleted_rows;

COMMIT;
