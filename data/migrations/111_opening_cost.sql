-- 111_opening_cost.sql
-- Decouple the WACC seed from its output.
--
-- cost_price BECOMES the live WACC output: recalculate_product_wacc writes it and
-- every margin / COGS / quote reader already consumes it. opening_cost is the NEW
-- immutable cost BASIS that seeds the ledger's INITIAL ("ยอดยกมา") entry. Keeping the
-- two apart stops the feedback loop where writing WACC back onto the seed re-blended
-- past purchases on every recompute and drifted the cost upward.
--
-- This migration only sets a SAFE placeholder (opening_cost = current cost_price) so
-- recompute never seeds 0-for-all on a fresh deploy. A one-time data op then corrects
-- opening_cost to the true PRE-2026-06-17-bulk-sync cost (from the pre-sync backup)
-- for the products whose cost_price was bulk-synced that day.
--
-- Column-additive only: products_full uses an explicit column list (not p.*) so the
-- view is unaffected, and audit_products_update is WHEN-gated on tracked columns so
-- the placeholder UPDATE does NOT touch any of them and fires no audit rows.
PRAGMA busy_timeout=10000;
BEGIN IMMEDIATE;

ALTER TABLE products ADD COLUMN opening_cost REAL NOT NULL DEFAULT 0.0;
UPDATE products SET opening_cost = cost_price;

COMMIT;
