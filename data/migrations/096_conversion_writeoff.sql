-- Migration 096 — record output write-off (yield loss / ของเสีย) on a conversion run.
--
-- A conversion like 10 แผง → 20 ตัว can yield a broken ตัว: the operator writes
-- off the broken unit(s) so only the GOOD units enter stock. This adds one
-- column to conversion_cost_log recording how many output units were scrapped
-- on each run, for an audit trail.
--
-- The cost treatment (total input cost spread over the GOOD output only, so
-- scrap correctly raises good-unit cost) lives in models.run_conversion — it
-- needs no schema change. The broken units never enter stock_levels (they were
-- never sellable), so there is no scrap stock movement to record here.
--
-- Purely ADDITIVE: one INTEGER column, NOT NULL DEFAULT 0, no data transform.

BEGIN;

ALTER TABLE conversion_cost_log ADD COLUMN writeoff_qty INTEGER NOT NULL DEFAULT 0;

COMMIT;
