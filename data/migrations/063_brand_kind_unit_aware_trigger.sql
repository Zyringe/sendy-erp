-- 063_brand_kind_unit_aware_trigger.sql
-- Make the brand_kind cache trigger UNIT-AWARE, and one-time repair the
-- express_sales.brand_kind values that the pre-063 trigger corrupted.
--
-- Background:
--   Migration 061 made product_code_mapping unit-aware: the SAME bsn_code
--   can now map to DIFFERENT products depending on bsn_unit (exact
--   (bsn_code, bsn_unit) override beats the bsn_unit='' catch-all). The
--   canonical resolver (models.py resolve_pending_mappings / get_mapping):
--       WHERE bsn_code = ? AND bsn_unit IN (COALESCE(unit,''), '')
--         AND product_id IS NOT NULL
--       ORDER BY (bsn_unit = '')   -- exact unit (0) before catch-all (1)
--       LIMIT 1
--
--   But the refresh_brand_kind_on_product_brand_change trigger recreated by
--   061 (verbatim from 021) still matched express_sales BY product_code
--   ONLY, ignoring express_sales.unit. On a split code (unit A → product A,
--   unit B → product B) changing product A's brand rewrote brand_kind for
--   EVERY express_sales row with that code — including rows that resolve to
--   product B. That silently corrupts the commission brand_kind cache.
--   (Codex adversarial review, high finding, 2026-05-20.)
--
-- Why a NEW migration (not an edit to 061):
--   061 is already recorded in applied_migrations on prod/Railway, so the
--   runner will never re-run it there. Editing 061 would only affect
--   never-applied DBs and leave prod's trigger broken. A forward migration
--   fixes prod AND fresh DBs and keeps applied migrations immutable.
--
-- This migration:
--   (1) replaces the trigger with a unit-aware predicate that mirrors the
--       resolver exactly, and
--   (2) one-time recomputes express_sales.brand_kind for every row whose
--       code resolves, repairing any corruption the old trigger caused.
--
-- No table is rebuilt here, so there is no DROP TABLE / RENAME trigger
-- re-validation hazard (the 061 prod-down failure mode does not apply).
--
-- NOTE: do NOT self-insert into applied_migrations here. The runner records
--   every migration it executes automatically.

BEGIN;

-- (1) Unit-aware trigger ----------------------------------------------------
DROP TRIGGER IF EXISTS refresh_brand_kind_on_product_brand_change;

CREATE TRIGGER refresh_brand_kind_on_product_brand_change
AFTER UPDATE OF brand_id ON products
WHEN OLD.brand_id IS NOT NEW.brand_id
BEGIN
    UPDATE express_sales
       SET brand_kind = (
           SELECT CASE WHEN b.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
             FROM brands b WHERE b.id = NEW.brand_id
       )
     WHERE NEW.id = (
         SELECT m.product_id
           FROM product_code_mapping m
          WHERE m.bsn_code = express_sales.product_code
            AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
            AND m.product_id IS NOT NULL
          ORDER BY (m.bsn_unit = '')   -- exact unit (0) before catch-all (1)
          LIMIT 1
       );
END;

-- (2) One-time backfill: recompute brand_kind for every resolvable row -------
-- Resolution MUST mirror the trigger / import resolver EXACTLY: pick the
-- winning product_id from product_code_mapping ALONE (bsn_code + unit-aware,
-- product_id NOT NULL), THEN look up that product's brand separately. Do NOT
-- JOIN brands inside the resolver — a winning product with brand_id IS NULL
-- (a supported state) would be filtered out before ORDER BY/LIMIT and the
-- backfill would wrongly fall through to a different mapping. With the brand
-- lookup OUTSIDE, a resolved-but-unbranded product yields brand_kind = NULL,
-- exactly what the trigger does (Codex review P2, 2026-05-20).
-- EXISTS guard: rows whose code resolves to no product keep their current
-- brand_kind (never nulled); resolved rows are recomputed (NULL if the
-- resolved product has no brand — consistent with the trigger).
UPDATE express_sales
   SET brand_kind = (
       SELECT CASE WHEN br.is_own_brand = 1 THEN 'own' ELSE 'third_party' END
         FROM brands br
        WHERE br.id = (
              SELECT p.brand_id FROM products p
               WHERE p.id = (
                     SELECT m.product_id
                       FROM product_code_mapping m
                      WHERE m.bsn_code = express_sales.product_code
                        AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
                        AND m.product_id IS NOT NULL
                      ORDER BY (m.bsn_unit = '')
                      LIMIT 1
               )
        )
   )
 WHERE EXISTS (
       SELECT 1
         FROM product_code_mapping m
        WHERE m.bsn_code = express_sales.product_code
          AND m.bsn_unit IN (COALESCE(express_sales.unit, ''), '')
          AND m.product_id IS NOT NULL
   );

COMMIT;
