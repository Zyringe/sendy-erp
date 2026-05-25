-- 085_somic_frankenpid_split.rollback.sql
--
-- Restores pre-mig-085 state from migration_085_snapshot_* tables, then
-- drops them. Run manually; the migration runner does not auto-rollback.
--
-- Stock invariant (trigger-aware via mig 080)
--   - Step 1: re-INSERT all snapshot transactions onto pid 1527.
--     after_transaction_insert fires, restoring pid 1527's ledger sum.
--   - Step 2: DELETE the MIG_085 consolidated ADJUSTs on pid 786/787.
--     after_transaction_delete fires, removing their contributions.
--   - Step 3: undo P_t/S_t.product_id changes from snapshot.
--   - Step 4: undo mapping change from snapshot.
--   - Step 5: re-soft-delete pid 787 (back to mig-083-post state) +
--     re-activate pid 1527.
--   - Step 6: recompute stock_levels from ledger (drift-recovery safety net).

BEGIN;

-- ── 1. Re-INSERT all snapshotted transactions on pid 1527 ─────────────────
-- INSERT OR IGNORE in case some rows already exist (defensive).
INSERT OR IGNORE INTO transactions
    (id, product_id, txn_type, quantity_change, unit_mode,
     reference_no, note, created_at)
SELECT txn_id, orig_product_id, orig_quantity_change, orig_txn_type,
       orig_unit_mode, orig_reference_no, orig_note, orig_created_at
  FROM migration_085_snapshot_txn;

-- ── 2. DELETE the consolidated MIG_085 ADJUSTs on pid 786/787 ─────────────
DELETE FROM transactions
 WHERE reference_no IN ('MIG_085_NET_9IN', 'MIG_085_NET_10IN');

-- ── 3. Restore P_t / S_t product_id from snapshot ─────────────────────────
UPDATE sales_transactions
   SET product_id = (SELECT orig_product_id FROM migration_085_snapshot_st
                      WHERE st_id = sales_transactions.id)
 WHERE id IN (SELECT st_id FROM migration_085_snapshot_st);

UPDATE purchase_transactions
   SET product_id = (SELECT orig_product_id FROM migration_085_snapshot_pt
                      WHERE pt_id = purchase_transactions.id)
 WHERE id IN (SELECT pt_id FROM migration_085_snapshot_pt);

-- ── 4. Restore product_code_mapping for 556ล1010 ──────────────────────────
UPDATE product_code_mapping
   SET product_id = (SELECT CAST(orig_value AS INTEGER)
                       FROM migration_085_snapshot_misc
                      WHERE field = 'mapping_556_1010_pid')
 WHERE bsn_code = '556ล1010' AND bsn_unit = '';

-- ── 5. Restore is_active for pid 787 and pid 1527 ─────────────────────────
UPDATE products
   SET is_active = (SELECT CAST(orig_value AS INTEGER)
                      FROM migration_085_snapshot_misc
                     WHERE field = 'pid_787_is_active')
 WHERE id = 787;

UPDATE products
   SET is_active = (SELECT CAST(orig_value AS INTEGER)
                      FROM migration_085_snapshot_misc
                     WHERE field = 'pid_1527_is_active')
 WHERE id = 1527;

-- ── 6. Recompute stock_levels from ledger (drift-recovery safety net) ─────
DELETE FROM stock_levels WHERE product_id IN (786, 787, 1527);
INSERT INTO stock_levels (product_id, quantity)
SELECT product_id, COALESCE(SUM(quantity_change), 0)
  FROM transactions
 WHERE product_id IN (786, 787, 1527)
 GROUP BY product_id;

-- ── 7. Drop snapshot tables ───────────────────────────────────────────────
DROP TABLE IF EXISTS migration_085_snapshot_txn;
DROP TABLE IF EXISTS migration_085_snapshot_st;
DROP TABLE IF EXISTS migration_085_snapshot_pt;
DROP TABLE IF EXISTS migration_085_snapshot_misc;

COMMIT;
