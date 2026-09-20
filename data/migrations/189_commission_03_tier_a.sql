-- 189 — ทวีเกียรติ (salesperson 03) moves onto the commission engine at Tier A (#589).
--
-- Put's ruling 2026-09-19: 03 is paid through /commission from now on, at Tier A
-- (10% own brands / 5% third party, no threshold), the same tier as ต๋อ /06.
-- Until now 03 sat on Tier C (the 0% placeholder seeded by mig 014), so the
-- engine never owed anything and every payment was keyed by hand in the cashbook.
--
-- What this moves, measured on the prod snapshot of 2026-09-19 15:36Z (mig 187)
-- with the engine's own functions:
--   * receipts collected up to 2026-04-30 stay settled (commission.SETTLED_THROUGH
--     and SOLD_SETTLED_BEFORE force remaining = 0), so no false backlog appears
--     before May 2026;
--   * May-Sep 2026 each hold ONE 03 invoice, all own brand: IV6900744 ฿87.40 (May),
--     IV6900441 ฿874.80 (Jun), IV6900531 ฿117.00 (Jul), IV6901059 ฿250.00 (Sep,
--     a ฿5/แผ่น override on pid 396). Those were paid by hand; the post-deploy
--     script scripts/2026_09_19_589_commission_03_backrecord.py records them.
--     Between this migration and that script, /commission shows them as owed.
--
-- effective_from is deliberately NOT changed: commission._load_tiers ignores it,
-- so writing a start date here would state a rule the engine does not apply.
-- The settlement constants above are what stop history from reading as owed.
--
-- Re-runnable: the UPDATE is idempotent, and the mig-014 audit trigger only logs
-- a row when a value really changes.
--
-- Precondition: ABORT if 03's assignment row or the Tier A row is missing.
-- The runner (database.py::run_pending_migrations) rolls back and does NOT stamp
-- applied_migrations, so a failed precondition leaves the DB untouched.
-- RECOVERY: both rows come from mig 014; a DB without them is not a Sendy DB
-- this migration was written for. Inspect with
--     SELECT * FROM commission_assignments WHERE salesperson_code = '03';
--     SELECT * FROM commission_tiers WHERE code = 'A';
--
-- Rollback: 189_commission_03_tier_a.rollback.sql
PRAGMA busy_timeout = 10000;

BEGIN;

DROP TABLE IF EXISTS temp._mig189_precheck;
CREATE TEMP TABLE _mig189_precheck AS
SELECT 'missing' AS problem
 WHERE NOT EXISTS (SELECT 1 FROM commission_assignments WHERE salesperson_code = '03')
    OR NOT EXISTS (SELECT 1 FROM commission_tiers WHERE code = 'A');

CREATE TEMP TRIGGER _mig189_precondition_guard
BEFORE DELETE ON _mig189_precheck
BEGIN
  SELECT RAISE(ABORT, 'mig 189 precondition FAILED: commission_assignments has no row for salesperson 03, or commission_tiers has no Tier A. See the RECOVERY note in this migration header.');
END;

DELETE FROM _mig189_precheck;
DROP TRIGGER _mig189_precondition_guard;
DROP TABLE _mig189_precheck;

UPDATE commission_assignments
   SET tier_id    = (SELECT id FROM commission_tiers WHERE code = 'A'),
       note       = 'ท /03 — ทวีเกียรติ (Tier A, Put 2026-09-19, #589)',
       updated_at = datetime('now', 'localtime')
 WHERE salesperson_code = '03'
   AND (tier_id IS NOT (SELECT id FROM commission_tiers WHERE code = 'A')
        OR note IS NOT 'ท /03 — ทวีเกียรติ (Tier A, Put 2026-09-19, #589)');

COMMIT;
