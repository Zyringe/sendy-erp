-- 177 — promo stacking guard at the DB: one CURRENT promo per (product,
-- slot), judged by DATE OVERLAP, not by counting active rows.
--
-- "Slot" = price (percent/fixed, or mixed-with-discount_value) or qty
-- (bundle/gift, or mixed-with-bundle_buy/gift_desc) — see
-- models/promotions.py::promo_slot_sql, which renders the SAME expression
-- text embedded in these trigger bodies below (byte-for-byte — a test
-- renders promo_slot_sql('p') / promo_slot_sql('NEW') and asserts the
-- trigger body in sqlite_master contains each string, so the trigger and
-- the resolver/replace_promotion cannot silently drift apart).
--
-- Two triggers, same body:
--   promotions_one_per_slot_ins — BEFORE INSERT
--   promotions_one_per_slot_upd — plain BEFORE UPDATE (NO `OF` column list).
--     `UPDATE OF col1, col2` only fires when one of those columns is in the
--     SET list, so a filtered trigger would miss an UPDATE that moves a
--     promo's product_id, or edits only date_start/date_end — exactly the
--     shapes 2a's replace_promotion and 2c's importer perform (closing the
--     OLD row is an UPDATE of date_end alone).
--
-- Overlap test (COALESCE to open-ended sentinels so a NULL date_start/
-- date_end reads as "since forever" / "until forever"):
--   p.date_start <= NEW.date_end  AND  NEW.date_start <= p.date_end
-- This is what lets 2a/2c leave the OLD row `is_active = 1` with
-- `date_end = new_start - 1 day` sitting right next to a future-dated NEW
-- row — their windows are adjacent, not overlapping, so no ABORT.
--
-- `p.id <> NEW.id` is required for the UPDATE trigger: without it, a
-- no-op `UPDATE promotions SET is_active = 1 WHERE id = <row's own id>`
-- would see itself as "another row in the same slot" and always ABORT
-- (pid 445's genuine 2-promo state depends on every no-op survive).
PRAGMA busy_timeout = 10000;

BEGIN;

DROP TRIGGER IF EXISTS promotions_one_per_slot_ins;
CREATE TRIGGER promotions_one_per_slot_ins
BEFORE INSERT ON promotions
WHEN NEW.is_active = 1 AND EXISTS (
  SELECT 1 FROM promotions p
   WHERE p.product_id = NEW.product_id AND p.is_active = 1 AND p.id <> NEW.id
     AND COALESCE(p.date_start,  '0000-01-01') <= COALESCE(NEW.date_end,  '9999-12-31')
     AND COALESCE(NEW.date_start,'0000-01-01') <= COALESCE(p.date_end,    '9999-12-31')
     AND ( ((p.promo_type IN ('percent','fixed') OR (p.promo_type = 'mixed' AND p.discount_value IS NOT NULL))
            AND (NEW.promo_type IN ('percent','fixed') OR (NEW.promo_type = 'mixed' AND NEW.discount_value IS NOT NULL)))
        OR ((p.promo_type IN ('bundle','gift') OR (p.promo_type = 'mixed' AND (p.bundle_buy IS NOT NULL OR p.gift_desc IS NOT NULL)))
            AND (NEW.promo_type IN ('bundle','gift') OR (NEW.promo_type = 'mixed' AND (NEW.bundle_buy IS NOT NULL OR NEW.gift_desc IS NOT NULL)))) ))
BEGIN
  SELECT RAISE(ABORT, 'one current price promo and one current qty promo per product');
END;

DROP TRIGGER IF EXISTS promotions_one_per_slot_upd;
CREATE TRIGGER promotions_one_per_slot_upd
BEFORE UPDATE ON promotions
WHEN NEW.is_active = 1 AND EXISTS (
  SELECT 1 FROM promotions p
   WHERE p.product_id = NEW.product_id AND p.is_active = 1 AND p.id <> NEW.id
     AND COALESCE(p.date_start,  '0000-01-01') <= COALESCE(NEW.date_end,  '9999-12-31')
     AND COALESCE(NEW.date_start,'0000-01-01') <= COALESCE(p.date_end,    '9999-12-31')
     AND ( ((p.promo_type IN ('percent','fixed') OR (p.promo_type = 'mixed' AND p.discount_value IS NOT NULL))
            AND (NEW.promo_type IN ('percent','fixed') OR (NEW.promo_type = 'mixed' AND NEW.discount_value IS NOT NULL)))
        OR ((p.promo_type IN ('bundle','gift') OR (p.promo_type = 'mixed' AND (p.bundle_buy IS NOT NULL OR p.gift_desc IS NOT NULL)))
            AND (NEW.promo_type IN ('bundle','gift') OR (NEW.promo_type = 'mixed' AND (NEW.bundle_buy IS NOT NULL OR NEW.gift_desc IS NOT NULL)))) ))
BEGIN
  SELECT RAISE(ABORT, 'one current price promo and one current qty promo per product');
END;

COMMIT;
