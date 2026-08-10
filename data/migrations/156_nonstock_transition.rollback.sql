-- 156_nonstock_transition.rollback.sql
-- Restores the pre-156 state from the three migration_156_* snapshot tables,
-- then drops them. Run manually; the migration runner never auto-rolls-back.
--
-- stock_levels is again left entirely to the mig-080 triggers:
--   step 1 DELETEs the correcting ADJUST  → stock_levels −= SUM(legs)
--   step 2 re-INSERTs the legs at their original ids → stock_levels += SUM(legs)
-- Net zero, so stock_levels lands back exactly where it was before 156.
--
-- The legs come back at their ORIGINAL `id` values, which matters:
-- reconcile._ledger_check and _delete_verified_txns key on transactions.id,
-- and audit_log rows already reference those ids.
--
-- Re-runnable: DELETE is a no-op the second time, INSERT OR IGNORE cannot
-- duplicate, the UPDATEs are idempotent, and the DROPs use IF EXISTS.

PRAGMA busy_timeout = 10000;

BEGIN IMMEDIATE;

-- ── 1. Remove the correcting ADJUSTs (trigger subtracts them back out) ─────
DELETE FROM transactions
 WHERE product_id IN (1211, 1623)
   AND txn_type = 'ADJUST'
   AND note = 'คืนยอดสต็อกหลังถอน ledger ของบรรทัดไม่นับสต็อก (migration 156) — ค่าขนส่ง/ส่วนลดพิเศษ ไม่ใช่สินค้าจริง';

-- ── 2. Restore the deleted ledger legs at their original ids ───────────────
INSERT OR IGNORE INTO transactions
    (id, product_id, txn_type, quantity_change, unit_mode, reference_no,
     note, created_at, source_bsn_code, source_line_seq)
SELECT id, product_id, txn_type, quantity_change, unit_mode, reference_no,
       note, created_at, source_bsn_code, source_line_seq
  FROM migration_156_deleted_ledger;

-- ── 3. Restore synced_to_stock = 1 on EXACTLY the rows that had it ─────────
UPDATE sales_transactions SET synced_to_stock = 1
 WHERE id IN (SELECT row_id FROM migration_156_synced_sources
               WHERE src_table = 'sales_transactions');
UPDATE purchase_transactions SET synced_to_stock = 1
 WHERE id IN (SELECT row_id FROM migration_156_synced_sources
               WHERE src_table = 'purchase_transactions');

-- ── 4. Restore the derived cost-ledger rows ────────────────────────────────
INSERT OR IGNORE INTO product_cost_ledger
    (id, product_id, event_type, event_date, qty_change, unit_cost,
     stock_after, wacc_after, reference_no, note, created_at)
SELECT id, product_id, event_type, event_date, qty_change, unit_cost,
       stock_after, wacc_after, reference_no, note, created_at
  FROM migration_156_deleted_cost_ledger;

-- ── 5. Drop the snapshots (a re-run of the forward migration rebuilds them) ─
DROP TABLE IF EXISTS migration_156_deleted_ledger;
DROP TABLE IF EXISTS migration_156_synced_sources;
DROP TABLE IF EXISTS migration_156_deleted_cost_ledger;

DELETE FROM applied_migrations WHERE filename = '156_nonstock_transition.sql';

COMMIT;
