-- ============================================================================
-- Migration 083 — collapse seed-stock duplicates
--                 (BullTech ตลับเมตร 5m/7.5m/10m + SOMIC ลูกกลิ้งสีน้ำ 10in)
--
-- Background
--   4 dormant product IDs each hold a single "ยอดต้นปี (back-solved จาก
--   current stock − ประวัติ BSN)" ADJUST row from 2024-01-03, while all BSN
--   traffic since has flowed through canonical pids. Result: stock split
--   between two pids per SKU, neither showing the full count.
--
--   Identified by Put 2026-05-25 manual review. Pattern is identical to
--   mig 081's pid 771 case, but here the fix is simpler: re-point the
--   ADJUST row's product_id to the canonical pid (mig 080 triggers handle
--   stock reconciliation automatically) — no scale conversion needed.
--
--     dormant pid (collapse)                       → canonical pid (keep)
--     ────────────────────────────────────────────────────────────────────
--     649  ตลับเมตรหุ้มยาง BullTech 5m     stock +22  →  1325 (Bull Tech 5m,   has BSN 561ต1060)
--     650  ตลับเมตรหุ้มยาง BullTech 7.5m   stock +11  →  1327 (Bull Tech 7.5m, has BSN 561ต1061)
--     651  ตลับเมตรหุ้มยาง BullTech 10m    stock +36  →  1323 (Bull Tech 10m,  has BSN 561ต1062)
--     787  ลูกกลิ้ง SOMIC 10in            stock +100 →  1527 (ลูกกลิ้งสีน้ำ SOMIC 10in, has BSN 556ล1010)
--
-- What this migration does NOT do (honest framing)
--   Post-mig stock is NOT verified to match physical count. It equals
--   (2024 seed baseline) + (2-yr BSN flow). Closer to truth than the split
--   state, but errors in 2-yr BSN data still propagate. Physical stock-take
--   is needed for ground-truth verification.
--
--   pid 1527 will still read -180 after this migration (= -280 + 100). Its
--   primary -1345 seed ADJUST is NOT recomputed here — that's deferred to
--   a separate migration once a physical count is available.
--
-- Why soft-delete instead of DELETE for dormant pids
--   FK reality check (2026-05-25): product_locations (4 rows) and
--   product_cost_ledger (4 rows) reference these pids with NO ACTION
--   semantics. Hard DELETE would error. is_active=0 preserves history
--   and avoids cascade surprises; Sendy UI / queries filter on is_active.
--
-- Trigger-aware (mig 080)
--   `after_transaction_update` fires when product_id changes:
--     stock_levels(OLD_pid) -= OLD.quantity_change   → drops to 0
--     stock_levels(NEW_pid) += NEW.quantity_change   → absorbs the seed
--   Net stock conserved across the operation. No manual stock_levels math
--   needed in this migration (per sendy_erp/CLAUDE.md merge-product rule).
--
-- Stock_levels final state
--     pid 649  → row dropped (was 0)
--     pid 650  → row dropped (was 0)
--     pid 651  → row dropped (was 0)
--     pid 787  → row dropped (was 0)
--     pid 1325 → -1 + 22  = +21
--     pid 1327 →  0 + 11  = +11
--     pid 1323 →  0 + 36  = +36
--     pid 1527 → -280 + 100 = -180  (still negative; awaits physical count)
--
-- Rollback
--   083_collapse_seed_duplicates.rollback.sql restores product_id + note
--   from migration_083_snapshot. Triggers fire in reverse to reconcile
--   stock_levels back to the pre-mig split. A defensive recompute of
--   stock_levels (drift-recovery flow) covers any trigger-state edge case.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot table for rollback ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_083_snapshot (
    txn_id           INTEGER PRIMARY KEY,
    orig_product_id  INTEGER NOT NULL,
    orig_note        TEXT
);

-- ── 2. Snapshot the 4 seed ADJUST rows ─────────────────────────────────────
-- Identified by (dormant pid + ADJUST + 2024-01-03 seed timestamp). On a
-- re-run the rows are no longer attached to the dormant pids (product_id
-- moved), so the SELECT yields zero rows → INSERT OR IGNORE no-op.
INSERT OR IGNORE INTO migration_083_snapshot (txn_id, orig_product_id, orig_note)
SELECT id, product_id, note
  FROM transactions
 WHERE product_id IN (649, 650, 651, 787)
   AND txn_type   = 'ADJUST'
   AND created_at = '2024-01-03 00:00:00';

-- ── 3. Re-point each ADJUST row to its canonical pid ───────────────────────
-- One UPDATE per pair for clarity. mig 080's `after_transaction_update`
-- handles stock_levels reconciliation. Each statement also has an
-- idempotency guard (`AND product_id = OLD_pid`) so a second forward run
-- finds nothing left to move and skips cleanly (no double-append of note).
UPDATE transactions
   SET product_id = 1325,
       note = note || ' [mig 083 merged → pid 1325 BullTech 5m]'
 WHERE id IN (SELECT txn_id FROM migration_083_snapshot WHERE orig_product_id = 649)
   AND product_id = 649;

UPDATE transactions
   SET product_id = 1327,
       note = note || ' [mig 083 merged → pid 1327 BullTech 7.5m]'
 WHERE id IN (SELECT txn_id FROM migration_083_snapshot WHERE orig_product_id = 650)
   AND product_id = 650;

UPDATE transactions
   SET product_id = 1323,
       note = note || ' [mig 083 merged → pid 1323 BullTech 10m]'
 WHERE id IN (SELECT txn_id FROM migration_083_snapshot WHERE orig_product_id = 651)
   AND product_id = 651;

UPDATE transactions
   SET product_id = 1527,
       note = note || ' [mig 083 merged → pid 1527 SOMIC ลูกกลิ้งสีน้ำ 10in]'
 WHERE id IN (SELECT txn_id FROM migration_083_snapshot WHERE orig_product_id = 787)
   AND product_id = 787;

-- ── 4. Drop zero stock_levels rows for the now-vacated dormant pids ────────
-- Triggers left them at 0; this keeps the table tidy.
DELETE FROM stock_levels
 WHERE product_id IN (649, 650, 651, 787)
   AND quantity = 0;

-- ── 5. Soft-delete the dormant duplicates ──────────────────────────────────
UPDATE products
   SET is_active = 0
 WHERE id IN (649, 650, 651, 787)
   AND is_active = 1;

COMMIT;
