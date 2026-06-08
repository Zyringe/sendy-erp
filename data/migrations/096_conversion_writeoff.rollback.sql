-- Rollback 096 — drop conversion_cost_log.writeoff_qty.
--
-- SQLite supports ALTER TABLE DROP COLUMN since 3.35.0 (env runs 3.51); the
-- column has no index / trigger / view / generated-column dependency, so the
-- drop is unconditional (same convention as mig 094's rollback). Recorded
-- writeoff counts are lost on rollback — acceptable, they are an audit field.

BEGIN;

ALTER TABLE conversion_cost_log DROP COLUMN writeoff_qty;

COMMIT;
