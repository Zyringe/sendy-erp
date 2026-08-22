-- 170 — pending_product_suggestions gains clone_source_pid.
--
-- WHY. mapping-suggest-clone PR3 lets Card B's "ส่งสร้าง SKU ใหม่" be
-- overwrite-prefilled from an EXISTING product (decision Q1), and the plan
-- (Q11) requires that provenance to survive into the created product's
-- `products.created_via = 'smart_mapping_clone_<source_pid>'` — but the
-- stage and the approve can be arbitrarily far apart in time ("ส่งให้
-- manager review" now, reviewed on Tab 2 hours/days later by someone else),
-- so the source product id has to be carried on the STAGED row itself, not
-- just passed through a single request's payload (create_now's one-request
-- path also reads it back off this same column, for symmetry — see
-- models/suggestions.py::approve_pending_suggestion).
--
-- No CHECK/trigger/index on this table touches this column (only
-- idx_pps_bsn_code and idx_pps_status exist, same as migration 169), so a
-- plain ALTER TABLE ADD COLUMN is enough; no table rebuild needed.

PRAGMA busy_timeout = 10000;

BEGIN;

ALTER TABLE pending_product_suggestions ADD COLUMN clone_source_pid INTEGER REFERENCES products(id);

COMMIT;
