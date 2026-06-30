-- data/migrations/121_marketplace_listing_status.sql
-- Persist marketplace listing status (live/delisted) per listing, BOTH platforms.
-- Grain = (platform, product_id_str) = one marketplace listing (NOT per variation;
-- a listing's variations share its status). Consumers join platform_skus on
-- (platform, product_id_str) for the internal pids.
--   Lazada: derived from platform_skus.raw_json $.status (delisted iff ALL variations inactive).
--   Shopee: live except the 75 product ids from the 2026-06-29 Seller-Center
--           "not shown" basic-info export (the Shopee export carries no status field).
-- NOTE: do NOT self-insert into applied_migrations.
-- On a fresh DB this migration is bootstrap-stamped (not replayed); the empty table
-- comes from schema.sql and there is no platform_skus data to backfill.
BEGIN;
CREATE TABLE IF NOT EXISTS marketplace_listing_status (
    platform        TEXT NOT NULL,
    product_id_str  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('live','delisted')),
    as_of           TEXT,
    source_file     TEXT,
    PRIMARY KEY (platform, product_id_str)
);

-- Lazada: delisted iff every variation of the listing is inactive
INSERT OR REPLACE INTO marketplace_listing_status (platform, product_id_str, status, as_of, source_file)
SELECT 'lazada', product_id_str,
       CASE WHEN SUM(CASE WHEN lower(COALESCE(json_extract(raw_json,'$.status'),'active'))='inactive'
                          THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'delisted' ELSE 'live' END,
       '2026-06-30', 'platform_skus.raw_json'
FROM platform_skus
WHERE platform='lazada' AND product_id_str IS NOT NULL AND product_id_str<>''
GROUP BY product_id_str;

-- Shopee: all listings live except the not-shown ids from the export
INSERT OR REPLACE INTO marketplace_listing_status (platform, product_id_str, status, as_of, source_file)
SELECT 'shopee', product_id_str,
       CASE WHEN product_id_str IN ('10167703206','11740262298','11905520519','12093008975','1245849975','1245894435','1250095392','1301155266','1437542320','1438054701','1540787540','1560379583','15718626403','1587472488','1587473687','1587644893','1587955436','1597589647','1597630313','1597817779','1598102256','1603237231','1617096003','1617146455','1617496869','16696561912','1724299836','1728039499','17387547719','18375572965','18765903620','19209461864','2044025726','2059447452','20991262572','2125514680','2326928544','2380830315','23927823065','24276476341','25313575648','25413224691','25512667619','25913236594','2705113037','27311939217','2765443303','28376144199','29418369064','3418754297','3906672582','4438130102','4804398549','4945134017','5147675505','5245321227','5504396528','5935400286','5935405466','6004183326','6038391052','6041755357','6715088528','6806615974','6951298850','7004197206','7102346731','7233310047','7351303978','7604199661','7848071002','7858583181','7942591839','8745795667','9345831178') THEN 'delisted' ELSE 'live' END,
       '2026-06-29', 'mass_update_basic_info_74562936_20260629173114.xlsx'
FROM (SELECT DISTINCT product_id_str FROM platform_skus
      WHERE platform='shopee' AND product_id_str IS NOT NULL AND product_id_str<>'');
COMMIT;
