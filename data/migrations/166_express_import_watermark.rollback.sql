-- Rollback for 166_express_import_watermark.sql — a new table, so it just goes.
-- The route falls back to the snapshot-derived watermark when the row is absent,
-- which is the pre-166 behaviour.
BEGIN;
DROP TABLE IF EXISTS express_import_watermark;
COMMIT;
