-- ============================================================================
-- Migration 081 — pid 771 ledger rewrite (โหล → อัน scale conversion)
--
-- Background
--   Mig 078 (2026-05-23) changed pid 771's unit_type 'โหล' → 'อัน' and
--   unit_conversions ratio (โหล) 1.0 → 12.0. The forward mig added a
--   reconciliation row `MIG_078` (+44 ADJUST) so stock_levels(771) jumped
--   from 4 (โหล base) to 48 (อัน base) immediately.
--
--   But the underlying ledger was left in mixed scale:
--     - 14 OUT rows synced from sales_transactions(unit='โหล') still hold
--       raw qty in the OLD โหล base (sum -94, real meaning -1128 อัน)
--     - 5 IN rows synced from purchase_transactions(unit='โหล') likewise
--       (sum +98, real meaning +1176 อัน)
--     - 3 OUT rows synced from sales_transactions(unit='อัน') already correct
--     - Opening balance ADJUST (+36, id 75733) made the pre-mig-078 SUM
--       arithmetically equal 4 = stock_levels(โหล base) by happening to
--       offset the also-wrong-scale อัน sales
--
--   Mig 078's rollback notes flag this as "deliberately deferred". Mig 080's
--   docstring also explicitly says recompute is out of scope. Mig 081 closes
--   the loop: rewrite the 19 affected rows × 12 and drop the patch ADJUST.
--
-- Why now
--   Future "recalc stock from transactions" recipes (`sendy_erp/CLAUDE.md`
--   drift-recovery flow) would currently corrupt pid 771's stock_levels by
--   summing the mixed-scale ledger. Closing the gap removes that landmine.
--
-- What this migration does
--   1. Snapshot the 19 row IDs + their prior quantity_change values into
--      migration_081_snapshot (for rollback)
--   2. Snapshot the full MIG_078 ADJUST row into migration_081_deleted_rows
--      (for rollback re-insertion)
--   3. UPDATE quantity_change *= 12 on the 19 snapshotted rows
--   4. DELETE the MIG_078 ADJUST row
--
-- Stock_levels math (post-mig 080: triggers auto-reconcile)
--   - Pre-state: stock_levels(771) = 48, ledger SUM = 48
--   - UPDATE delta from mig 080's `after_transaction_update`:
--       sum(NEW - OLD) = sum(11 * OLD) = 11 * (98 - 94) = +44
--   - DELETE delta from mig 080's `after_transaction_delete`: -44
--   - Net: 0 → stock_levels stays at 48 throughout
--   - Post-state ledger SUM: 36 + (98*12) + (-94*12) + (-36) = 48 ✓
--
--   If mig 080 is NOT yet applied (e.g., partial deploy), stock_levels is
--   simply untouched and the ledger SUM still equals 48 by construction.
--   This migration is trigger-agnostic.
--
-- Rollback
--   The .rollback.sql file restores from the two snapshot tables:
--     - Restores quantity_change from snapshot on the 19 affected rows
--     - Re-INSERTs the MIG_078 ADJUST row from migration_081_deleted_rows
--     - Recomputes stock_levels(771) from the restored ledger SUM as the
--       final step (trigger-agnostic safety net — covers both the mig-080-
--       present and mig-080-absent cases)
-- ============================================================================

BEGIN;

-- ── 1. Snapshot tables for rollback ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_081_snapshot (
    id                INTEGER PRIMARY KEY,
    prior_qty_change  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_081_deleted_rows (
    id              INTEGER PRIMARY KEY,
    product_id      INTEGER NOT NULL,
    txn_type        TEXT    NOT NULL,
    quantity_change INTEGER NOT NULL,
    unit_mode       TEXT    NOT NULL,
    reference_no    TEXT,
    note            TEXT,
    created_at      TEXT    NOT NULL
);

-- ── 2. Snapshot the 14 OUT rows (sales unit='โหล') ─────────────────────────
-- The EXISTS guard makes this re-run safe: if the MIG_078 row is gone, the
-- mig already ran → snapshot stays empty → UPDATE/DELETE below are no-ops.
-- This protects against a manual re-run after fresh BSN imports added more
-- โหล sales rows (those would already be in อัน scale post-mig-078; without
-- the guard, a re-run would multiply them × 12 a second time).
INSERT OR IGNORE INTO migration_081_snapshot (id, prior_qty_change)
SELECT id, quantity_change
FROM transactions
WHERE product_id = 771
  AND txn_type   = 'OUT'
  AND reference_no IN (
      SELECT doc_no FROM sales_transactions
      WHERE product_id = 771 AND unit = 'โหล'
  )
  AND EXISTS (
      SELECT 1 FROM transactions
      WHERE product_id = 771 AND reference_no = 'MIG_078'
  );

-- ── 3. Snapshot the 5 IN rows (purchases unit='โหล') ───────────────────────
-- Same EXISTS guard for re-run safety (see step 2).
INSERT OR IGNORE INTO migration_081_snapshot (id, prior_qty_change)
SELECT id, quantity_change
FROM transactions
WHERE product_id = 771
  AND txn_type   = 'IN'
  AND reference_no IN (
      SELECT doc_no FROM purchase_transactions
      WHERE product_id = 771 AND unit = 'โหล'
  )
  AND EXISTS (
      SELECT 1 FROM transactions
      WHERE product_id = 771 AND reference_no = 'MIG_078'
  );

-- ── 4. Snapshot the MIG_078 ADJUST row for re-insertion on rollback ────────
INSERT OR IGNORE INTO migration_081_deleted_rows
       (id, product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
SELECT  id, product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at
FROM transactions
WHERE product_id = 771
  AND reference_no = 'MIG_078'
  AND txn_type = 'ADJUST';

-- ── 5. Multiply the 19 snapshotted ledger rows × 12 ────────────────────────
-- Mig 080's `after_transaction_update` trigger (if applied) auto-reconciles
-- stock_levels for each UPDATE; net delta across all 19 rows is +44 (see
-- migration header). Without mig 080, stock_levels is untouched (also fine —
-- net ledger SUM still resolves to 48).
--
-- EXISTS guard for re-run safety: snapshot table persists across runs (it's
-- needed by rollback), so on a second forward-mig run the snapshot still
-- holds the 19 IDs. Without this guard, UPDATE would multiply × 12 a second
-- time. Gating by the MIG_078 row's presence means: if mig already ran (row
-- gone), this is a no-op.
UPDATE transactions
   SET quantity_change = quantity_change * 12
 WHERE id IN (SELECT id FROM migration_081_snapshot)
   AND EXISTS (
       SELECT 1 FROM transactions
       WHERE product_id = 771 AND reference_no = 'MIG_078'
   );

-- ── 6. Drop the MIG_078 patch ADJUST row ───────────────────────────────────
-- Mig 080's `after_transaction_delete` trigger (if applied) auto-decrements
-- stock_levels by OLD.quantity_change (44), restoring net delta to 0.
DELETE FROM transactions
 WHERE product_id   = 771
   AND reference_no = 'MIG_078'
   AND txn_type     = 'ADJUST';

COMMIT;
