-- 180 — promotions.date_start stops lying about when a promo started.
--
-- WHAT WAS WRONG (issue #500, measured on PROD 2026-09-11)
-- All 570 promotions carried date_start = '2024-01-01' and date_end = NULL,
-- while created_at spanned three import batches: 2026-06-01 (565 rows),
-- 2026-08-11 (1) and 2026-08-28 (4). The placeholder was not a code default —
-- scripts/import_catalog_pricing.py REQUIRES --batch-date and writes it
-- straight to date_start. Every row's promo_name still reads
-- 'catalog 2024-01-01 (...)', which is that flag's own echo: the operator
-- passed 2024-01-01 on all three runs.
--
-- WHY IT MATTERS
-- price_lookup.py::_epoch_candidates uses the CURRENT price-slot promo's
-- date_start as the `promo_start` epoch source, and _window clamps the
-- evidence window with max(epoch, today - 365). A 2024-01-01 epoch always
-- loses that max(), so a promotion could never register as a price-regime
-- change. Concretely on prod: 4 กิ๊ปรัด ORBIT products (pid 1111/1112/1114/1116)
-- have NO base_sell_price history at all, so their 2026-08-28 promo is their
-- ONLY price event — and it was invisible. /quote-customer could answer with a
-- last-paid bill struck before that promo and call it the current price.
--
-- WHAT PUT DECIDED (2026-09-11)
-- The 565 rows from the 2026 catalogue are STANDING discounts that were
-- already being given before they were imported; the import recorded prices
-- that already existed, it did not change them. They therefore get
-- date_start = NULL, which both _epoch_candidates and the one-per-slot
-- triggers already read as "since forever / never moved the regime".
-- The 5 later rows ARE real price events (pid 1902's base went 0.00 -> 19.00
-- on the same day; 116 base_sell_price rows changed on 2026-08-28) and get
-- their true date.
--
-- BLAST RADIUS, measured on prod with the app's own _epoch_candidates before
-- writing this: of 520 products holding a price-slot promo, 501 already had
-- base_changed inside the window, so their epoch never depended on the promo.
-- Under the rejected alternative (date_start = created_at for all 570) the
-- window floor moved for 355 products — 336 of them by 1-7 days — and only
-- 13 bills across 12 products left the evidence population. Under Put's
-- ruling the 565 keep their current answers exactly and only the 5 real
-- events clamp.
--
-- POPULATION
-- `date_start = '2024-01-01' AND source = 'catalog-import'` is a CLOSED
-- historical set: nothing writes that placeholder any more, and a manually
-- created promo (source = 'manual') is never touched. On a DB that does not
-- hold these rows this migration is a no-op, which is correct.
--
-- ROLLBACK is exact, not derived: migration_180_snapshot records each
-- touched row's id and its old date_start before anything is written.
-- Do NOT try to recover by pattern-matching date(created_at) — a row whose
-- true start legitimately equals its created_at date is indistinguishable.

PRAGMA busy_timeout = 10000;
BEGIN;

-- ── precondition: ABORT if any product already holds two overlapping active
-- promos in one slot ────────────────────────────────────────────────────────
-- Setting date_start = NULL widens a row's interval to "since forever", and
-- promotions_one_per_slot_upd (mig 177) is a PLAIN BEFORE UPDATE trigger with
-- no `OF` column list, so it fires on the UPDATEs below. Measured 0 such pairs
-- on prod 2026-09-11, but prod can drift between the rehearsal and the deploy,
-- and the trigger's own abort message ("one current price promo and one
-- current qty promo per product") would not say which migration hit it.
--
-- Mechanism: BEFORE DELETE fires once per row, so DELETE on an EMPTY precheck
-- table is a no-op and on a NON-empty one aborts. database.py's runner catches,
-- rolls back, and does NOT stamp applied_migrations — a failed precondition
-- leaves the DB untouched.
--
-- RECOVERY: list the offending pairs with the RECOVERY query in migration 177's
-- header, close the earlier row at (later row's date_start - 1 day), re-run.
DROP TABLE IF EXISTS temp._mig180_precheck;
CREATE TEMP TABLE _mig180_precheck AS
SELECT DISTINCT p.product_id
  FROM promotions p JOIN promotions q
    ON q.product_id = p.product_id AND q.id <> p.id
   AND p.is_active = 1 AND q.is_active = 1
   AND COALESCE(p.date_start,'0000-01-01') <= COALESCE(q.date_end,'9999-12-31')
   AND COALESCE(q.date_start,'0000-01-01') <= COALESCE(p.date_end,'9999-12-31')
   AND ((((p.promo_type IN ('percent','fixed') OR (p.promo_type = 'mixed' AND p.discount_value IS NOT NULL))) AND ((q.promo_type IN ('percent','fixed') OR (q.promo_type = 'mixed' AND q.discount_value IS NOT NULL)))) OR (((p.promo_type IN ('bundle','gift') OR (p.promo_type = 'mixed' AND (p.bundle_buy IS NOT NULL OR p.gift_desc IS NOT NULL)))) AND ((q.promo_type IN ('bundle','gift') OR (q.promo_type = 'mixed' AND (q.bundle_buy IS NOT NULL OR q.gift_desc IS NOT NULL))))));

CREATE TEMP TRIGGER _mig180_precondition_guard
BEFORE DELETE ON _mig180_precheck
BEGIN
  SELECT RAISE(ABORT, 'mig 180 precondition FAILED: a product already holds two overlapping active promos in one slot, so nulling date_start would trip promotions_one_per_slot_upd. See the RECOVERY note in this migration header.');
END;

DELETE FROM _mig180_precheck;
DROP TRIGGER _mig180_precondition_guard;
DROP TABLE _mig180_precheck;

-- ── forensic snapshot (rollback reads from here, never from a pattern) ──────
CREATE TABLE IF NOT EXISTS migration_180_snapshot (
  promotion_id   INTEGER PRIMARY KEY,
  old_date_start TEXT
);
DELETE FROM migration_180_snapshot;
INSERT INTO migration_180_snapshot (promotion_id, old_date_start)
SELECT id, date_start
  FROM promotions
 WHERE date_start = '2024-01-01' AND source = 'catalog-import';

-- ── transform 1: the 2026 catalogue carry-over = standing discount ──────────
UPDATE promotions
   SET date_start = NULL
 WHERE date_start = '2024-01-01'
   AND source = 'catalog-import'
   AND date(created_at) <= '2026-06-01';

-- ── transform 2: promos imported AFTER that batch are real price events ─────
UPDATE promotions
   SET date_start = date(created_at)
 WHERE date_start = '2024-01-01'
   AND source = 'catalog-import'
   AND date(created_at) > '2026-06-01';

-- ── postcondition: no placeholder may survive ───────────────────────────────
-- Stated as an invariant, not a row count, so it holds on prod and on any dev
-- clone regardless of how many of these rows that DB happens to carry.
DROP TABLE IF EXISTS temp._mig180_postcheck;
CREATE TEMP TABLE _mig180_postcheck AS
SELECT id FROM promotions
 WHERE date_start = '2024-01-01' AND source = 'catalog-import';

CREATE TEMP TRIGGER _mig180_postcondition_guard
BEFORE DELETE ON _mig180_postcheck
BEGIN
  SELECT RAISE(ABORT, 'mig 180 postcondition FAILED: a catalog-import promo still carries the 2024-01-01 placeholder after both UPDATEs.');
END;

DELETE FROM _mig180_postcheck;
DROP TRIGGER _mig180_postcondition_guard;
DROP TABLE _mig180_postcheck;

COMMIT;
