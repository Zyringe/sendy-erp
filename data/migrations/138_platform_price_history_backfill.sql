-- 138_platform_price_history_backfill.sql
-- Best-effort SEED of platform_price_history with the marketplace price changes
-- that were applied via the Claude-assisted growth loop and diff-verified:
--   Shopee 2026-07-16 (6) + Lazada 2026-07-17 (15).
-- Source: Operations/05_analysis-reports/product/marketplace_price_change_history_2026-07-21.md
-- (Shopee CHANGELOG + Lazada worklist reprice_R "ใน upload?"=YES).
--
-- These predate any /ecommerce import after 2026-07-12, so the trigger (mig 137)
-- would not have captured them. This seeds the product-page timeline so it is not
-- empty on day 1. changed_at = the real change date; variation_id left NULL
-- (Shopee changelog is pid-only; a pid can span >1 variation) — best-effort, keyed
-- on internal_product_id which is all the product-page view needs.
--
-- PROD-SAFE: INNER JOIN products drops any pid not present, so a master-data
-- mismatch on prod skips that row instead of FK-failing boot.
--
-- variation_id is RESOLVED by matching the recorded old_value against the
-- pre-reprice platform_skus snapshot (special_price for special rows, price for
-- price rows). This disambiguates products with several variations — e.g. pid
-- 133 lazada has a 1-ตัว listing (special 9→10) and a 12-ตัว แผง listing
-- (special 95→119); matching old 9 vs 95 pins each to its variation. Unresolved
-- rows keep NULL (best-effort).
--
-- Rollback: 138_platform_price_history_backfill.rollback.sql (deletes by source tag)

BEGIN;

INSERT INTO platform_price_history
    (platform, variation_id, internal_product_id, field_name, old_value, new_value, changed_at, source)
SELECT b.platform,
       (SELECT ps.variation_id FROM platform_skus ps
         WHERE ps.platform = b.platform
           AND ps.internal_product_id = b.pid
           AND ( (b.field = 'special_price' AND ps.special_price = b.old_v)
              OR (b.field = 'price'         AND ps.price         = b.old_v) )
         LIMIT 1),
       b.pid, b.field, b.old_v, b.new_v, b.changed_at,
       'backfill:campaign-2026-07 (best-effort)'
FROM (
    -- Shopee 2026-07-16 (main price)
              SELECT 'shopee' AS platform, 133 AS pid, 'price' AS field, 100 AS old_v, 117 AS new_v, '2026-07-16' AS changed_at
    UNION ALL SELECT 'shopee', 149, 'price',  69,  71, '2026-07-16'
    UNION ALL SELECT 'shopee', 506, 'price',  80,  84, '2026-07-16'
    UNION ALL SELECT 'shopee',  91, 'price', 110, 113, '2026-07-16'
    UNION ALL SELECT 'shopee', 566, 'price',  35,  36, '2026-07-16'
    UNION ALL SELECT 'shopee', 569, 'price',  30,  33, '2026-07-16'
    -- Lazada 2026-07-17 (price + special_price)
    UNION ALL SELECT 'lazada', 129, 'price',         100, 104, '2026-07-17'
    UNION ALL SELECT 'lazada', 129, 'special_price',  70, 103, '2026-07-17'
    UNION ALL SELECT 'lazada', 133, 'special_price',   9,  10, '2026-07-17'
    UNION ALL SELECT 'lazada', 133, 'special_price',  95, 119, '2026-07-17'
    UNION ALL SELECT 'lazada',  91, 'price',         110, 114, '2026-07-17'
    UNION ALL SELECT 'lazada', 744, 'special_price',  20,  27, '2026-07-17'
    UNION ALL SELECT 'lazada', 643, 'special_price',  60,  85, '2026-07-17'
    UNION ALL SELECT 'lazada', 644, 'price',          95, 106, '2026-07-17'
    UNION ALL SELECT 'lazada', 644, 'special_price',  60, 105, '2026-07-17'
    UNION ALL SELECT 'lazada', 645, 'price',         115, 122, '2026-07-17'
    UNION ALL SELECT 'lazada', 645, 'special_price',  80, 121, '2026-07-17'
    UNION ALL SELECT 'lazada', 646, 'price',         145, 170, '2026-07-17'
    UNION ALL SELECT 'lazada', 646, 'special_price', 100, 169, '2026-07-17'
    UNION ALL SELECT 'lazada', 647, 'price',         125, 147, '2026-07-17'
    UNION ALL SELECT 'lazada', 647, 'special_price',  90, 146, '2026-07-17'
) b
JOIN products p ON p.id = b.pid;

COMMIT;
