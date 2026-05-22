-- ============================================================================
-- Migration 078 — data-quality mapping + UC cleanup (PR-B scope, schema only)
--
-- Source: Put's reviewed decisions in
--   Operations/05_analysis-reports/mig078_pr_b3_decision_sheet_2026-05-22.csv
--
-- This is the SCHEMA-CLEANUP half of the deferred PR-B work. NO historical
-- stock recompute, NO retargeting of historical sales_transactions rows,
-- NO platform_skus changes. Stock_levels stay exactly where they are.
--
-- Three coordinated changes per affected pid:
--   1. product_code_mapping: convert legacy rows (bsn_unit='') to unit-aware
--      rows by filling in the real bsn_unit. Where Put's filled bsn_unit
--      collides with an existing unit-aware row, DELETE the legacy row
--      instead of UPDATE-ing (UNIQUE(bsn_code, bsn_unit) would otherwise
--      block). The unit-aware row already does the right thing.
--   2. unit_conversions: DELETE rows Put marked "delete this mapp" in the
--      CSV. These were synonyms/conversions that came from legacy data and
--      no longer fit the cleaner mapping shape Put wants going forward.
--   3. products: UPDATE unit_type to match the bsn_unit Put filled in for
--      each pid's real mapping row.
--
-- Per-pid overrides (Put's explicit decisions 2026-05-22):
--   • pid 771: ut โหล → อัน, UC (โหล, 12.0) — per the ORIGINAL tracker
--     ratio=12 note, NOT the CSV's bsn_unit='ตัว' (Put confirmed the
--     tracker decision over the CSV value for this pid)
--   • pid 1142 / 1143: ut stays 'ตัว' (per CSV), NOT 'แผง' as the tracker
--     originally suggested
--   • Historical sales: stay attached to current pid; no retarget
--   • Stock_levels: not touched
--
-- Scope: 25 source pids + 4 retarget targets (which get mapping cleanup but
-- no other changes) = 29 pids total.
--
-- Rollback strategy: snapshot the prior state of every touched row into 3
-- migration_078_snapshot_* tables at the start. Rollback restores from them.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot prior state for rollback ────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_078_snapshot_products (
    id              INTEGER PRIMARY KEY,
    prior_unit_type TEXT
);
CREATE TABLE IF NOT EXISTS migration_078_snapshot_uc (
    product_id   INTEGER,
    bsn_unit     TEXT,
    prior_ratio  REAL,
    PRIMARY KEY (product_id, bsn_unit)
);
CREATE TABLE IF NOT EXISTS migration_078_snapshot_mapping (
    id                  INTEGER PRIMARY KEY,
    prior_bsn_unit      TEXT,
    prior_product_id    INTEGER,
    prior_bsn_code      TEXT,
    prior_bsn_name      TEXT
);

-- Snapshot products (only ones whose unit_type will change)
INSERT OR IGNORE INTO migration_078_snapshot_products (id, prior_unit_type)
SELECT id, unit_type FROM products
WHERE id IN (142, 771, 992, 1357, 1369, 1370, 1371, 1372, 1373, 1374,
             1492, 1493, 1495, 1503, 1505, 1506, 1511, 2003);

-- Snapshot UCs (every UC we delete or update)
INSERT OR IGNORE INTO migration_078_snapshot_uc (product_id, bsn_unit, prior_ratio)
SELECT product_id, bsn_unit, ratio FROM unit_conversions
WHERE (product_id, bsn_unit) IN (
    -- pid 142
    (142, 'ตัว'), (142, 'แผง'),
    -- pid 149
    (149, 'แผง'),
    -- pid 150
    (150, 'ตัว'), (150, 'แผง'),
    -- pid 162
    (162, 'ตัว'),
    -- pid 771
    (771, 'โหล'), (771, 'อัน'),
    -- pid 991 (phantom orphans)
    (991, 'ตัว'), (991, 'แผง'),
    -- pid 1142, 1143
    (1142, 'ชุด'), (1142, 'แผง'),
    (1143, 'ชุด'), (1143, 'แผง'),
    -- pid 1357
    (1357, 'กิโลกรัม'),
    -- pid 1369-1374
    (1369, 'กิโลกรัม'), (1369, 'แพ็ค'),
    (1370, 'กิโลกรัม'), (1370, 'แพ็ค'),
    (1371, 'กิโลกรัม'), (1371, 'แพ็ค'),
    (1372, 'กิโลกรัม'), (1372, 'แพ็ค'),
    (1373, 'กิโลกรัม'), (1373, 'แพ็ค'),
    (1374, 'กิโลกรัม'), (1374, 'แพ็ค'),
    -- pid 1492-1511
    (1492, 'ตัว'),
    (1493, 'ตัว'),
    (1495, 'ตัว'), (1495, 'แผง'),
    (1503, 'แผง'),
    (1505, 'แผง'),
    (1506, 'แผง'),
    (1511, 'แผง'),
    (2003, 'แผง')
);

-- Snapshot mapping rows that will change
INSERT OR IGNORE INTO migration_078_snapshot_mapping
    (id, prior_bsn_unit, prior_product_id, prior_bsn_code, prior_bsn_name)
SELECT id, bsn_unit, product_id, bsn_code, bsn_name FROM product_code_mapping
WHERE id IN (
    59, 60, 61, 150, 163, 399, 1680, 1681, 1728, 1763,
    1766, 1767, 1739, 1489, 1682, 1488, 852, 1487, 972,
    1738, 1729, 846, 1557, 1509, 476, 1017, 537
);

-- ── 2. product_code_mapping changes ─────────────────────────────────────────
-- DELETEs first (free up UNIQUE(bsn_code, bsn_unit) slot before any UPDATE)

-- pid 128: id 59 (041ม2760, '', pid=128) — would collide with id 2090
-- (041ม2760, 'แผง', pid=148) after fill-in + retarget. DEL legacy row;
-- id 2090 already routes 'แผง' to pid 148 (Put's intent).
DELETE FROM product_code_mapping WHERE id = 59;

-- pid 162: id 150 (041ม3350, '', pid=162) — would collide with id 2092
-- (041ม3350, 'แผง', pid=162). DEL legacy.
DELETE FROM product_code_mapping WHERE id = 150;

-- UPDATEs (fill in bsn_unit on legacy rows)

-- pid 128: 041ม2761 → ตัว
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 399;
-- pid 142: 041ม3370 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 163;
-- pid 149: 041ม2850 → แผง (id 2091 already has bsn_unit='ตัว' for same code)
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 60;
-- pid 150: 041ม2858 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 61;
-- pid 162: 041ม3315 → ตัว
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 1680;
-- pid 992: 041ม2625 → ตัว, 041ม2629 → แผง
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 1728;
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1763;
-- pid 994 (retarget target): 041ม3320 → ตัว
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 1681;
-- pid 1142: 045ก0641-2 → ตัว
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 1766;
-- pid 1143: 045ก0643-1 → ตัว
UPDATE product_code_mapping SET bsn_unit = 'ตัว' WHERE id = 1767;
-- pid 1357: 167ถ0060 → แพ็ค
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 1739;
-- pid 1369-1374: all → แพ็ค
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 1489;  -- 1369
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 1682;  -- 1370
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 1488;  -- 1371
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 852;   -- 1372
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 1487;  -- 1373
UPDATE product_code_mapping SET bsn_unit = 'แพ็ค' WHERE id = 972;   -- 1374
-- pid 1492: 141ม1786 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1738;
-- pid 1493: 041ม5550 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1729;
-- pid 1495: 041ม5560 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 846;
-- pid 1503: 041ม2630 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1557;
-- pid 1505: 041ม2918 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1509;
-- pid 1506: 041ม2710 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 476;
-- pid 1511: 041ม3900 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 1017;
-- pid 2003: 041ม2855 → แผง
UPDATE product_code_mapping SET bsn_unit = 'แผง' WHERE id = 537;

-- (pid 771 mapping id 579: leave bsn_unit='' as legacy fallback. Future BSN
-- imports of 999ม9995 with units 'โหล'/'อัน' will fall back to it. Tracker
-- decision was ut → อัน with UC (โหล, 12.0), so the legacy fallback +
-- unit_conversions table cover both units cleanly.)

-- ── 3. unit_conversions changes ─────────────────────────────────────────────
-- Policy (Put 2026-05-22): KEEP every non-identity 1.0 UC so future BSN imports
-- that still send the old unit string sync as 1:1 relabel — no manual review.
-- Only DELETE: (a) identity UCs that are redundant after the ut change,
-- (b) phantom orphans on pid 991, (c) the one non-1.0 UC Put marked deletion
-- on in the CSV (pid 162 'ตัว' 0.5 → UPDATE to 1.0 for the new relabel policy),
-- and (d) UC ratio update for pid 771 per the original tracker decision.

-- pid 162: UPD UC (ตัว) ratio 0.5 → 1.0 (1:1 relabel, replaces the
-- "delete this mapp" intent now that Put wants the legacy unit to stay synced)
UPDATE unit_conversions SET ratio = 1.0
WHERE product_id = 162 AND bsn_unit = 'ตัว' AND ratio = 0.5;

-- pid 771: UPD UC (โหล) ratio 1.0 → 12.0 per ORIGINAL tracker (1 BSN โหล = 12 อัน)
UPDATE unit_conversions SET ratio = 12.0
WHERE product_id = 771 AND bsn_unit = 'โหล';

-- DEL identity UCs that become redundant after their ut change:
--   pid 142 (แผง=ut), pid 149 (แผง=ut), pid 150 (แผง=ut), pid 771 (อัน=ut),
--   pid 1369-1374 (แพ็ค=ut), pid 1495 (แผง=ut), pid 1503/1505/1506/1511/2003 (แผง=ut)
DELETE FROM unit_conversions WHERE product_id = 142  AND bsn_unit = 'แผง' AND ratio = 1.0;
DELETE FROM unit_conversions WHERE product_id = 149  AND bsn_unit = 'แผง' AND ratio = 1.0;
DELETE FROM unit_conversions WHERE product_id = 150  AND bsn_unit = 'แผง' AND ratio = 1.0;
DELETE FROM unit_conversions WHERE product_id = 771  AND bsn_unit = 'อัน' AND ratio = 1.0;
DELETE FROM unit_conversions WHERE product_id IN (1369, 1370, 1371, 1372, 1373, 1374)
    AND bsn_unit = 'แพ็ค';
DELETE FROM unit_conversions WHERE product_id = 1495 AND bsn_unit = 'แผง' AND ratio = 1.0;
DELETE FROM unit_conversions WHERE product_id IN (1503, 1505, 1506, 1511, 2003)
    AND bsn_unit = 'แผง';

-- DEL phantom orphan UCs on pid 991 (no mapping or sales — no future BSN data
-- will ever reach pid 991, so the relabel-keep policy doesn't apply here)
DELETE FROM unit_conversions WHERE product_id = 991 AND bsn_unit IN ('ตัว', 'แผง');

-- KEPT (relabel ratio=1.0 for future BSN imports of these legacy units):
--   (142, ตัว, 1.0)         — 1 BSN ตัว = 1 แผง of pid 142 after ut→แผง
--   (149, ตัว, 0.5)         — non-1.0 ratio, untouched per Put's CSV
--   (150, ตัว, 1.0)
--   (162, ตัว, 1.0)         — updated from 0.5 above
--   (1142, ชุด, 1.0), (1142, แผง, 1.0)   — pid 1142 stays ut='ตัว'; both relabel
--   (1143, ชุด, 1.0), (1143, แผง, 1.0)   — same
--   (1357, กิโลกรัม, 1.0)   — pid 1357 ut→แพ็ค; relabel
--   (1369-1374, กิโลกรัม, 1.0) — pid ut→แพ็ค; relabel
--   (1492, ตัว, 1.0), (1493, ตัว, 1.0), (1495, ตัว, 1.0)  — pid ut→แผง; relabel
--   (992, ตัว, 1.0)         — already keep (Put did not mark delete in CSV)
--   (128, ตัว, 1.0), (994, ตัว, 1.0)     — identity, harmless

-- ── 4. products.unit_type changes ───────────────────────────────────────────
UPDATE products SET unit_type = 'แผง'
WHERE id IN (142, 992, 1492, 1493, 1495, 1503, 1505, 1506, 1511, 2003)
  AND unit_type = 'ตัว';

UPDATE products SET unit_type = 'แพ็ค'
WHERE id IN (1357, 1370, 1371, 1373) AND unit_type = 'ตัว';

UPDATE products SET unit_type = 'แพ็ค'
WHERE id IN (1369, 1372, 1374) AND unit_type = 'กิโลกรัม';

UPDATE products SET unit_type = 'อัน'
WHERE id = 771 AND unit_type = 'โหล';

COMMIT;
