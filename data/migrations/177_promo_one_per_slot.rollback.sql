-- Rollback 177 — drop both stacking-guard triggers. Pure trigger add/remove,
-- no data touched, so rollback is a clean drop (no snapshot needed).
PRAGMA busy_timeout = 10000;

BEGIN;

DROP TRIGGER IF EXISTS promotions_one_per_slot_ins;
DROP TRIGGER IF EXISTS promotions_one_per_slot_upd;

COMMIT;
