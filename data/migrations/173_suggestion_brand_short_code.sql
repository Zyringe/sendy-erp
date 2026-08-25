-- 173: carry a NEW brand's short_code + Thai name across stage -> approve.
--
-- Card B lets you type a brand that does not exist yet. Before this, the only
-- thing that survived staging was `brand_other_name`, so `brands.short_code`
-- was left NULL on every brand born that way -- and short_code is a SEGMENT OF
-- sku_code, so those products silently lost their brand segment
-- (`CHM-500ml` instead of `CHM-LIQ-SONAX-500ml`). SONAX was the only one of 76
-- brands with a NULL short_code; it got there through exactly this path.
--
-- `brand_other_name_th` exists because the old inline INSERT copied `name` into
-- `name_th`, which rendered the picker label as "SONAX / SONAX". The fix stops
-- copying and lets the operator type a real Thai name when there is one.
PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions ADD COLUMN brand_other_short_code TEXT;
ALTER TABLE pending_product_suggestions ADD COLUMN brand_other_name_th TEXT;

COMMIT;
