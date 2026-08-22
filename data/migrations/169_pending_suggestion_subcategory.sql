-- 169 — pending_product_suggestions gains sub_category, sub_category_short_code,
-- category_id.
--
-- WHY. `parse_name` emits a fine-grained "category" (a `products.sub_category`
-- shape, 501 distinct live values — e.g. 'แปรงทาสี', 'ลูกบิด') but
-- `mapping.html`'s Card B feeds that text into the Category combo, which is
-- bound to the 39-row `categories` master. `comboCommit` is pick-only there,
-- so an unmatched value resolves to nothing: the text is discarded and
-- `category_id` never gets set. Verified live on the one product ever created
-- this way (pid 2044, `created_via='smart_mapping'`):
--   product_name " แปรงทาสี Sendai #111-2in"  <- leading space, first
--                                                 name segment empty
--   sku_code     "SD-111-2in"                  <- starts at the brand;
--                                                 both prefix segments missing
--   sub_category NULL  sub_category_short_code NULL  category_id NULL
-- (products.sub_category feeds the name's first segment via
-- name_builder.rebuild_product_name, and sku_code_utils.build_sku_code needs
-- BOTH categories.short_code (cat_short_code, via category_id) and
-- sub_category_short_code to build the two sku_code prefix segments.)
--
-- This migration only adds the staging columns `pending_product_suggestions`
-- needs so the app-layer fix (PR2, projects/mapping-suggest-clone/plan.md)
-- has somewhere correct to write both axes — the taxonomy master
-- (`category_id`) and the free-text sub-category
-- (`sub_category`/`sub_category_short_code`, which has no derivation table,
-- same as products.sub_category_short_code — Put types it by hand).
--
-- No CHECK/trigger/index on this table touches these columns (verified
-- against data/schema.sql — only idx_pps_bsn_code and idx_pps_status exist),
-- so a plain ALTER TABLE ADD COLUMN is enough; no table rebuild needed.

PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions ADD COLUMN sub_category TEXT;
ALTER TABLE pending_product_suggestions ADD COLUMN sub_category_short_code TEXT;
ALTER TABLE pending_product_suggestions ADD COLUMN category_id INTEGER REFERENCES categories(id);

COMMIT;
