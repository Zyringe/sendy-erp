-- ============================================================================
-- Migration 085 — SOMIC ลูกกลิ้ง 10in frankenpid split + mig 083 reversal
--
-- Background
--   Investigation 2026-05-25 (post-mig 083) revealed that pid 1527
--   "ลูกกลิ้งสีน้ำ SOMIC 10in" is not a simple 10in duplicate of pid 787 —
--   it's a 4-year frankenpid that accumulated BOTH 9in and 10in BSN
--   history due to bad routing in product_code_mapping prior to 2026-02.
--
--   Family pattern (BNS catalogue):
--     pid 784 (sku 817) ลูกกลิ้ง SOMIC 4in    — BSN 556ล1004, family canonical
--     pid 785 (sku 818) ลูกกลิ้ง SOMIC 7in    — BSN 556ล1007, family canonical
--     pid 786 (sku 819) ลูกกลิ้ง SOMIC 9in    — BSN 556ล1009 (mapped 2026-02), ghost (seed only)
--     pid 787 (sku 820) ลูกกลิ้ง SOMIC 10in   — sku 820 = intended 10in family member
--                                              (soft-deleted by mig 083 — to be revived)
--     pid 1527 (sku 1564) ลูกกลิ้งสีน้ำ SOMIC 10in — frankenpid, holds:
--                                                  - 9in BSN traffic (28 S + 20 P, 2024-01 to 2026-02)
--                                                  - 10in BSN traffic (96 S + 60 P, 2024-01 to 2026-05)
--                                                  - frankenpid -1345 opening ADJUST seed (2024-01-03)
--                                                  - +100 ADJUST seed moved here wrongly by mig 083
--
--   Mig 083 made it worse: assumed pid 1527 was the canonical 10in and
--   collapsed pid 787 into it. Wrong direction — pid 787 (sku 820) is
--   the intended family member.
--
-- What this migration does
--   1. REVERSES mig 083's SOMIC piece:
--      - Move +100 ADJUST row from pid 1527 back to pid 787
--      - Re-activate pid 787 (is_active=1)
--   2. Re-maps BSN code 556ล1010 (= 10in) from pid 1527 → pid 787 in
--      product_code_mapping (future BSN imports route correctly)
--   3. Updates sales_transactions/purchase_transactions.product_id for
--      all rows with bsn_code 556ล1009 (→ pid 786) and 556ล1010 (→ pid 787)
--      so doc-detail pages route correctly
--   4. Deletes ALL remaining transactions on pid 1527 (BSN ledger + -1345
--      frankenpid seed). Triggers reconcile stock_levels(1527) to 0.
--   5. Inserts ONE summary ADJUST per canonical pid representing the
--      consolidated net BSN flow:
--      - pid 786 += (Σ P 9in × ratio) − (Σ S 9in × ratio) = +1280 − 1199 = +81
--      - pid 787 += (Σ P 10in × ratio) − (Σ S 10in อัน × 1.0 + Σ S 10in โหล × 12)
--                = 4160 − 4032 − 24 = +104
--      Net stocks per pid (verified pre-mig math):
--        pid 786: existing +80 seed + +81 net = **+161**
--        pid 787: +100 returned seed + +104 net = **+204**
--   6. Ensures unit_conversions on pid 786/787 cover the units BSN uses
--      (อัน=1.0, โหล=12.0) so future syncs route correctly.
--   7. Soft-deletes pid 1527, removes its zero stock_levels row.
--
-- Drops by design (not lost — audit trail in P_t/S_t)
--   - frankenpid -1345 opening ADJUST (was wrong attribution against
--     mishmashed 9in+10in history — has no business meaning on a single
--     SKU)
--   - +880 history_import phantom IN sum (audit padding rows — net=0
--     on real stock effect)
--   Delta: +1345 (drop bad seed) − 880 (drop phantom IN) = +465 stock
--   gain, distributed back to 786/787 as documented per-row above.
--
-- What this migration does NOT do (honest framing)
--   - Does NOT verify post-mig stock against physical count. Numbers
--     reflect (2024 seed baseline) + (4yr BSN flow), trusting that the
--     +80 seed on pid 786 and the +100 seed (returned to pid 787) were
--     computed from correct physical counts at 2024-01-03. Stock-take
--     still recommended.
--   - Does NOT re-create per-doc transactions on pid 786/787. The 4-year
--     BSN history is consolidated into a single ADJUST per pid. Doc-level
--     traceability remains in sales_transactions / purchase_transactions
--     (with corrected product_id after step 3).
--
-- Trigger-aware (mig 080)
--   All steps rely on after_transaction_insert/update/delete triggers
--   to reconcile stock_levels automatically. No manual stock math.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot tables for rollback ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_085_snapshot_txn (
    txn_id              INTEGER PRIMARY KEY,
    orig_product_id     INTEGER,
    orig_quantity_change REAL,
    orig_txn_type       TEXT,
    orig_unit_mode      TEXT,
    orig_reference_no   TEXT,
    orig_note           TEXT,
    orig_created_at     TEXT
);

CREATE TABLE IF NOT EXISTS migration_085_snapshot_st (
    st_id            INTEGER PRIMARY KEY,
    orig_product_id  INTEGER
);

CREATE TABLE IF NOT EXISTS migration_085_snapshot_pt (
    pt_id            INTEGER PRIMARY KEY,
    orig_product_id  INTEGER
);

CREATE TABLE IF NOT EXISTS migration_085_snapshot_misc (
    field      TEXT PRIMARY KEY,
    orig_value TEXT
);

-- ── 2. Snapshot mutating data ──────────────────────────────────────────────
-- Snapshot every transactions row on pid 1527 (so rollback can restore them all)
INSERT OR IGNORE INTO migration_085_snapshot_txn
    (txn_id, orig_product_id, orig_quantity_change, orig_txn_type,
     orig_unit_mode, orig_reference_no, orig_note, orig_created_at)
SELECT id, product_id, quantity_change, txn_type,
       unit_mode, reference_no, note, created_at
  FROM transactions WHERE product_id = 1527;

-- Snapshot P/S rows we're going to re-point
INSERT OR IGNORE INTO migration_085_snapshot_st (st_id, orig_product_id)
SELECT id, product_id FROM sales_transactions
 WHERE product_id = 1527 AND bsn_code IN ('556ล1009', '556ล1010');

INSERT OR IGNORE INTO migration_085_snapshot_pt (pt_id, orig_product_id)
SELECT id, product_id FROM purchase_transactions
 WHERE product_id = 1527 AND bsn_code IN ('556ล1009', '556ล1010');

-- Snapshot misc state (pid 787 is_active, mapping for 556ล1010)
INSERT OR IGNORE INTO migration_085_snapshot_misc (field, orig_value) VALUES
  ('pid_787_is_active',
   (SELECT CAST(is_active AS TEXT) FROM products WHERE id = 787)),
  ('pid_1527_is_active',
   (SELECT CAST(is_active AS TEXT) FROM products WHERE id = 1527)),
  ('mapping_556_1010_pid',
   (SELECT CAST(product_id AS TEXT) FROM product_code_mapping
     WHERE bsn_code = '556ล1010' AND bsn_unit = ''));

-- ── 3. REVERSE mig 083's SOMIC piece ───────────────────────────────────────
-- Move +100 ADJUST seed from pid 1527 back to pid 787. mig 080's
-- after_transaction_update reconciles stock_levels automatically.
-- Idempotency: AND product_id = 1527 ensures re-run skips already-moved row.
UPDATE transactions
   SET product_id = 787,
       note = note || ' [mig 085 reversed mig 083 wrong-direction merge → back to pid 787 (sku 820 family canonical)]'
 WHERE quantity_change = 100
   AND txn_type = 'ADJUST'
   AND created_at = '2024-01-03 00:00:00'
   AND product_id = 1527;

-- Re-activate pid 787
UPDATE products SET is_active = 1 WHERE id = 787 AND is_active = 0;

-- ── 4. Update product_code_mapping: 556ล1010 → pid 787 ─────────────────────
UPDATE product_code_mapping
   SET product_id = 787
 WHERE bsn_code = '556ล1010' AND bsn_unit = '' AND product_id = 1527;

-- ── 5. Update sales_transactions.product_id for both bsn_codes ────────────
UPDATE sales_transactions SET product_id = 786
 WHERE bsn_code = '556ล1009' AND product_id = 1527;
UPDATE sales_transactions SET product_id = 787
 WHERE bsn_code = '556ล1010' AND product_id = 1527;

-- ── 6. Update purchase_transactions.product_id for both bsn_codes ─────────
UPDATE purchase_transactions SET product_id = 786
 WHERE bsn_code = '556ล1009' AND product_id = 1527;
UPDATE purchase_transactions SET product_id = 787
 WHERE bsn_code = '556ล1010' AND product_id = 1527;

-- ── 7. Ensure unit_conversions for pid 786/787 cover BSN units ────────────
-- pid 786 already has UC entries from sync; INSERT OR IGNORE protects.
-- pid 787 currently has nothing (was soft-deleted with no traffic).
INSERT OR IGNORE INTO unit_conversions (product_id, bsn_unit, ratio) VALUES
  (786, 'อัน', 1.0),
  (786, 'โหล', 12.0),
  (787, 'อัน', 1.0),
  (787, 'โหล', 12.0);

-- ── 8. DELETE all remaining transactions on pid 1527 ──────────────────────
-- After step 3 these rows are: 116 BSN IN + 124 BSN OUT + 1 frankenpid ADJUST
-- (-1345). Triggers fire on each delete, decrementing stock_levels(1527).
-- Final stock_levels(1527) = 0.
--
-- The +880 history_import phantom IN sum is implicitly dropped here. Doesn't
-- affect real stock (those rows were net=0 audit padding).
DELETE FROM transactions WHERE product_id = 1527;

-- ── 9. Insert consolidated ADJUST per canonical pid ───────────────────────
-- pid 786 ← net 9in BSN flow (+81 = 1280 P − 1199 S, all อัน × 1.0)
-- pid 787 ← net 10in BSN flow (+104 = 4160 P − 4032 S(อัน) − 2 × 12 S(โหล))
--
-- Compute dynamically from now-updated P_t/S_t to be re-run robust and
-- handle any edge cases (e.g., if a unit other than อัน/โหล appeared).
INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
SELECT 786, 'ADJUST',
       CAST(COALESCE(
         (SELECT SUM(pt.qty * COALESCE(uc.ratio, 1.0))
            FROM purchase_transactions pt
            LEFT JOIN unit_conversions uc
              ON uc.product_id = 786 AND uc.bsn_unit = pt.unit
           WHERE pt.bsn_code = '556ล1009' AND pt.product_id = 786), 0)
       - COALESCE(
         (SELECT SUM(st.qty * COALESCE(uc.ratio, 1.0))
            FROM sales_transactions st
            LEFT JOIN unit_conversions uc
              ON uc.product_id = 786 AND uc.bsn_unit = st.unit
           WHERE st.bsn_code = '556ล1009' AND st.product_id = 786), 0)
       AS INTEGER),
       'unit', 'MIG_085_NET_9IN',
       '[mig 085] consolidated 9in BSN flow (was on frankenpid 1527 — see migration_085_snapshot_* tables)',
       '2024-01-03 00:00:01'
 WHERE NOT EXISTS (
       SELECT 1 FROM transactions
        WHERE product_id = 786 AND reference_no = 'MIG_085_NET_9IN'
 );

INSERT INTO transactions
    (product_id, txn_type, quantity_change, unit_mode, reference_no, note, created_at)
SELECT 787, 'ADJUST',
       CAST(COALESCE(
         (SELECT SUM(pt.qty * COALESCE(uc.ratio, 1.0))
            FROM purchase_transactions pt
            LEFT JOIN unit_conversions uc
              ON uc.product_id = 787 AND uc.bsn_unit = pt.unit
           WHERE pt.bsn_code = '556ล1010' AND pt.product_id = 787), 0)
       - COALESCE(
         (SELECT SUM(st.qty * COALESCE(uc.ratio, 1.0))
            FROM sales_transactions st
            LEFT JOIN unit_conversions uc
              ON uc.product_id = 787 AND uc.bsn_unit = st.unit
           WHERE st.bsn_code = '556ล1010' AND st.product_id = 787), 0)
       AS INTEGER),
       'unit', 'MIG_085_NET_10IN',
       '[mig 085] consolidated 10in BSN flow (was on frankenpid 1527 — see migration_085_snapshot_* tables)',
       '2024-01-03 00:00:01'
 WHERE NOT EXISTS (
       SELECT 1 FROM transactions
        WHERE product_id = 787 AND reference_no = 'MIG_085_NET_10IN'
 );

-- ── 10. Cleanup stock_levels for pid 1527 + soft-delete the frankenpid ────
DELETE FROM stock_levels WHERE product_id = 1527;
UPDATE products SET is_active = 0 WHERE id = 1527 AND is_active = 1;

COMMIT;
