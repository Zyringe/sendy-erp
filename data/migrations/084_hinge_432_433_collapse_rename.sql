-- ============================================================================
-- Migration 084 — บานพับ Sendai #432/#433 dup-collapse + long-format rename
--
-- Background
--   Two related cleanups identified by Put 2026-05-25 in the
--   /products review:
--
--   (A) Duplicate split between pid 85 (long-format seed) and pid 1393
--       (canonical BSN-receiving). Same pattern as mig 083's Bull Tech /
--       SOMIC cases — 2024-01-03 "ยอดต้นปี (back-solved)" seed landed on
--       a dormant pid while BSN traffic flows through a sibling pid.
--
--     dormant pid (collapse)                                           → canonical pid (keep, rename)
--     ─────────────────────────────────────────────────────────────────────────────────────────────────
--     85   บานพับแหวนท.ล Sendai #432-4in สีสเปรย์ (PAC) (แผง)   stock +117  →  1393  (BSN 030บ2032, 37 txns)
--
--   (B) Naming/structure cleanup on pid 1395 — the sibling #433 SN
--       product. No merge; only renames to match the standardized long
--       format and fixes unit_type + size columns.
--
-- What this migration does
--   1. Re-point pid 85's lone ADJUST row to pid 1393 (mig 080 triggers
--      reconcile stock_levels). Net stock(1393) = -12 + 117 = +105.
--   2. Rename pid 1393:
--        product_name → "บานพับแหวนท.ล Sendai #432-4in สีสเปรย์ (PAC) (แผง)"
--        unit_type    → "แผง"  (was "ตัว" — UC ratios already 1.0 for both,
--                               so this is a relabel, NOT a scale change)
--        size         → "4in"  (was empty)
--   3. Rename pid 1395 to the same long format with #433 SN substitutions:
--        product_name → "บานพับแหวนท.ล Sendai #433-4in สีเงินด้าน (SN) (แผง)"
--        unit_type    → "แผง"  (was "ตัว" — UC ratio for (1395, แผง)=1.0, OK)
--        size         → "4in"  (was empty)
--   4. Cleanup zero stock_levels row for pid 85, soft-delete pid 85.
--
-- What this migration does NOT do
--   - Does NOT touch pid 86 (#433 รมดำ AC) or pid 1394 (#433 JSN). Put
--     explicitly scoped this round to #432 PAC merge + #433 SN rename.
--   - Does NOT change any quantity_change or unit_mode in the ledger.
--     unit_type rename is purely a label change because UC ratios for
--     both pid 1393 (ตัว=1.0, แผง=1.0) and pid 1395 (แผง=1.0) are 1.0.
--   - Does NOT recompute stock to match physical count. As with mig 083,
--     post-mig stock = 2024-baseline + 2yr BSN flow (closer to truth, not
--     verified correct). Physical stock-take still needed.
--
-- Why soft-delete pid 85
--   FK reality check identical to mig 083: product_locations and
--   product_cost_ledger likely hold rows with NO ACTION semantics. Soft
--   delete preserves history; Sendy UI filters by is_active.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot table for rollback ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_084_snapshot (
    kind       TEXT    NOT NULL,        -- 'txn' or 'pcol'
    row_key    INTEGER NOT NULL,        -- txn_id or product_id
    field      TEXT    NOT NULL,        -- column name being snapshotted
    orig_value TEXT,                    -- original value (NULL ok)
    PRIMARY KEY (kind, row_key, field)
);

-- ── 2. Snapshot pid 85's seed ADJUST row (for the merge step) ──────────────
-- Identified by (pid + ADJUST + 2024-01-03 seed timestamp) — uniquely
-- matches mig-083-style seed rows. On a re-run the row is no longer at
-- product_id=85, so the SELECT yields zero rows → INSERT no-op.
INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'txn', id, 'product_id', CAST(product_id AS TEXT)
  FROM transactions
 WHERE product_id = 85 AND txn_type = 'ADJUST'
   AND created_at = '2024-01-03 00:00:00';

INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'txn', id, 'note', note
  FROM transactions
 WHERE product_id = 85 AND txn_type = 'ADJUST'
   AND created_at = '2024-01-03 00:00:00';

-- ── 3. Snapshot pid 1393 + 1395 columns being renamed ──────────────────────
INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'pcol', id, 'product_name', product_name FROM products WHERE id IN (1393, 1395);
INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'pcol', id, 'unit_type',    unit_type    FROM products WHERE id IN (1393, 1395);
INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'pcol', id, 'size',         size         FROM products WHERE id IN (1393, 1395);
INSERT OR IGNORE INTO migration_084_snapshot (kind, row_key, field, orig_value)
SELECT 'pcol', id, 'is_active',    CAST(is_active AS TEXT) FROM products WHERE id = 85;

-- ── 4. Merge: re-point pid 85's ADJUST to pid 1393 ────────────────────────
-- mig 080's after_transaction_update fires: stock_levels(85) -= 117 (→ 0),
-- stock_levels(1393) += 117 (-12 → +105). Idempotency: AND product_id = 85
-- ensures no double-move on re-run.
UPDATE transactions
   SET product_id = 1393,
       note = note || ' [mig 084 merged → pid 1393 บานพับแหวนท.ล Sendai #432-4in PAC]'
 WHERE id IN (SELECT row_key FROM migration_084_snapshot WHERE kind = 'txn')
   AND product_id = 85;

-- ── 5. Rename canonical pid 1393 to standardized long format ──────────────
-- unit_type change ตัว→แผง: relabel only (UC ratios for both = 1.0).
UPDATE products
   SET product_name = 'บานพับแหวนท.ล Sendai #432-4in สีสเปรย์ (PAC) (แผง)',
       unit_type    = 'แผง',
       size         = '4in'
 WHERE id = 1393
   AND product_name = 'บานพับ Sendai #432 สีสเปรย์ (PAC) (แผง)';

-- ── 6. Rename pid 1395 to same long format with #433 SN substitutions ─────
UPDATE products
   SET product_name = 'บานพับแหวนท.ล Sendai #433-4in สีเงินด้าน (SN) (แผง)',
       unit_type    = 'แผง',
       size         = '4in'
 WHERE id = 1395
   AND product_name = 'บานพับ Sendai #433 สีเงินด้าน (SN) (แผง)';

-- ── 7. Drop zero stock_levels row for pid 85 ──────────────────────────────
DELETE FROM stock_levels WHERE product_id = 85 AND quantity = 0;

-- ── 8. Soft-delete pid 85 (dormant duplicate) ─────────────────────────────
UPDATE products
   SET is_active = 0
 WHERE id = 85 AND is_active = 1;

COMMIT;
