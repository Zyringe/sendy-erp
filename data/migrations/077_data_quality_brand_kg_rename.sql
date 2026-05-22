-- ============================================================================
-- Migration 077 — data-quality fixes from tracker XLSX (PR-A scope only)
--
-- Source: Put's reviewed decisions in
--   data_quality_tracker_2026-05-21.xlsx (sheets D5_NoBrand + D3_KG)
--
-- This migration is the SAFE half of the tracker (D5 brand assignments +
-- D3 simple kg-rename). The retroactive half (D1 SWAP_UNIT_TYPE / WRONG_MAP /
-- NEW_PRODUCT_ID + D3 complex → แพ็ค + stock recompute) is deferred to a
-- follow-up PR because it requires:
--   (1) a platform-stock-safe variant of _sync_bsn_to_stock — the current
--       resync path double-deducts shopee_stock / lazada_stock /
--       platform_skus.stock (deductions live on the products row, not in the
--       transactions table, so they don't get restored by nuke+resync)
--   (2) product_code_mapping retarget for WRONG_MAP — mig 061 made the
--       mapping table unit-aware; a UC-only fix re-introduces bad data on
--       the next BSN import
--   (3) snapshot/restore design for retroactive transactions
--
-- This PR-A:
--   • Adds 3 new third-party brands (FOUR STARS, BEYOND, ALTECO).
--   • Sets brand_id on 98 products that were ไม่ระบุแบรนด์
--     (the ฿1.95M revenue bucket from D5_NoBrand). Allocation:
--       28 → Sendai (id=3)
--        2 → Golden Lion (id=1)
--        1 → BullTech (id=52, already in brands table)
--        2 → FOUR STARS (new)
--        2 → BEYOND (new)
--        1 → ALTECO (new)
--       62 → Other (id=13, third_party catch-all). The 62 are exported
--            separately to
--            Operations/05_analysis-reports/d5_pending_brand_reclassification_2026-05-22.csv
--            so Put can reclassify them later. Per Put's decision
--            2026-05-22: "ใส่ Other ไปก่อน".
--   • Renames unit_type 'กก.' → 'กิโลกรัม' on 17 products. Verified the live
--     BSN ledger already uses 'กิโลกรัม' on these pids (zero rows with
--     unit='กก.' in sales_transactions), so the rename is purely cosmetic
--     for existing transactions. The identity unit_conversions row
--     (pid, 'กิโลกรัม', 1.0) becomes redundant after the rename but is
--     left in place — _get_base_qty short-circuits when bsn_unit==unit_type
--     anyway, so the row is harmless. Less change to roll back.
--
-- Rollback strategy:
--   Snapshot prior (brand_id, unit_type) per affected pid into
--   migration_077_snapshot at the start. Rollback file restores from it.
--   The 3 new brands are dropped on rollback by code (AUTOINCREMENT id
--   means we can't hardcode the new IDs).
--
-- No stock impact. No transactions touched. No platform_skus touched.
-- ============================================================================

BEGIN;

-- ── 1. Snapshot prior state for rollback ────────────────────────────────────
-- IF NOT EXISTS + OR IGNORE so a manual re-run doesn't overwrite the snapshot
-- captured on the first run (the migration runner is filename-keyed and
-- shouldn't re-run on its own, but defend against manual replay).
CREATE TABLE IF NOT EXISTS migration_077_snapshot (
    id              INTEGER PRIMARY KEY,
    prior_brand_id  INTEGER,
    prior_unit_type TEXT
);

INSERT OR IGNORE INTO migration_077_snapshot (id, prior_brand_id, prior_unit_type)
SELECT id, brand_id, unit_type
FROM products
WHERE id IN (
    -- D5 brand-only pids (98)
    -- SENDAI (28)
    401,191,189,190,402,414,411,214,215,221,219,400,216,220,56,410,403,53,218,52,
    59,54,62,211,60,217,418,212,
    -- GOLDEN LION (2)
    621,576,
    -- BullTech (1, brand already exists id=52)
    1033,
    -- FOUR STARS (2, new brand)
    406,407,
    -- BEYOND (2, new brand)
    776,775,
    -- ALTECO (1, new brand)
    872,
    -- OTHER / catch-all (62) — see external CSV for reclassification backlog
    660,988,855,853,588,764,727,648,834,548,405,573,999,768,1691,713,572,1363,412,1003,
    698,763,704,729,1651,702,1330,587,659,575,1303,686,1353,1347,1647,577,578,1362,1646,586,
    1821,1517,1687,1862,1709,549,1202,1364,658,554,556,1176,557,1639,1200,1565,618,1883,1358,
    1025,1102,1195,
    -- D3 simple kg-rename pids (17 — overlaps pid 414 + 686 with the D5 list above)
    415,416,472,473,474,475,476,477,681,682,683,684,685,687,912
);

-- ── 2. New 3rd-party brands ─────────────────────────────────────────────────
INSERT OR IGNORE INTO brands (code, name, is_own_brand, sort_order) VALUES
    ('four_stars', 'FOUR STARS', 0, 200),
    ('beyond',     'BEYOND',     0, 200),
    ('alteco',     'ALTECO',     0, 200);

-- ── 3. D5 — Brand assignments (98 pids) ─────────────────────────────────────

-- SENDAI (28)
UPDATE products SET brand_id = 3 WHERE id IN (
    401,191,189,190,402,414,411,214,215,221,219,400,216,220,56,410,403,53,218,52,
    59,54,62,211,60,217,418,212
);

-- GOLDEN LION (2)
UPDATE products SET brand_id = 1 WHERE id IN (621, 576);

-- BullTech (1)
UPDATE products SET brand_id = 52 WHERE id = 1033;

-- FOUR STARS (2)
UPDATE products SET brand_id = (SELECT id FROM brands WHERE code='four_stars')
WHERE id IN (406, 407);

-- BEYOND (2)
UPDATE products SET brand_id = (SELECT id FROM brands WHERE code='beyond')
WHERE id IN (776, 775);

-- ALTECO (1)
UPDATE products SET brand_id = (SELECT id FROM brands WHERE code='alteco')
WHERE id = 872;

-- OTHER / ทั่วไป (62)
UPDATE products SET brand_id = 13 WHERE id IN (
    660,988,855,853,588,764,727,648,834,548,405,573,999,768,1691,713,572,1363,412,1003,
    698,763,704,729,1651,702,1330,587,659,575,1303,686,1353,1347,1647,577,578,1362,1646,586,
    1821,1517,1687,1862,1709,549,1202,1364,658,554,556,1176,557,1639,1200,1565,618,1883,1358,
    1025,1102,1195
);

-- ── 4. D3 simple — unit_type 'กก.' → 'กิโลกรัม' (17 pids) ─────────────────────
-- Defensive: only flip rows still saying 'กก.'. Re-apply safe.
UPDATE products SET unit_type = 'กิโลกรัม'
WHERE id IN (414,415,416,472,473,474,475,476,477,681,682,683,684,685,686,687,912)
  AND unit_type = 'กก.';

COMMIT;
