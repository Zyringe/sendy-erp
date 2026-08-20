-- 167_generic_standins_rivet_4_2.sql
-- Curated stand-ins: the 4-2 rivet colour variants are booked in Express under
-- ONE generic catch-all product, "ตะปูยิงรีเวท Sendai 4-2 (ซอง)" (pid 882) —
-- the same shape migration 134 already recorded for the bidet/shower heads and
-- the 4-6 rivets (pid 848). Without a row here, every 4-2 marketplace order
-- fails Pass 1/1.5 of marketplace_match and has to be hand-linked.
--
-- Evidence (measured on prod 2026-08-20, counting only single-product invoices
-- against single-product orders so a co-occurring line on a bundled invoice
-- cannot inflate the tally):
--   906 ลูกรีเวท DOME 4-2 สีธรรมชาติ -> 882 in 13 of 14 pairs (93%)
--   977 / 979 / 1894 (CSK 4-2 น้ำตาล / ขาว / ดำ): too few clean pairs to
--       decide from data alone; included on Put's explicit confirmation
--       (2026-08-20) that the team keys EVERY 4-2 colour as the one generic.
--       Curation by a human is the intended input for this table — see
--       Operations/05_analysis-reports/engineering/
--       generic-standin-schema-design_2026-07-10.md §1.
--   978 ลูกรีเวท CSK Sendai 4-2 สีธรรมชาติ is deliberately NOT included: it has
--       no platform_skus mapping, so there is no order it could ever serve.
--
-- Measured effect when applied to prod (full row-diff of all 4,338
-- marketplace_order_invoice rows across a run_automatch of both platforms):
-- added 2, removed 0, changed 0 — two settled Lazada orders that had been
-- stranded without an invoice since 2024-03 and 2026-04 now link confidently.
--
-- ⚠ ONE GENERIC PER VARIANT. The table's UNIQUE(variant, generic) permits
-- several rows per variant, but marketplace_match._generic_standins() reads
-- them into a DICT — a second row for the same variant silently overrides the
-- first. Add a row only for a variant that has none, or UPDATE the existing
-- row; never both.
--
-- INVARIANT (inherited from 134, unchanged): consulted ONLY by
-- inventory_app/marketplace_match.py. Never join it into stock, mapping or
-- unit_conversions paths.
--
-- Idempotent: WHERE NOT EXISTS, because prod and the dev DB already received
-- these four rows by hand on 2026-08-20 (verified there before this file was
-- written) and the migration must be a no-op there rather than tripping
-- UNIQUE(variant_product_id, generic_product_id).
--
-- Apply:    sqlite3 .../inventory.db < data/migrations/167_generic_standins_rivet_4_2.sql
-- Rollback: data/migrations/167_generic_standins_rivet_4_2.rollback.sql

BEGIN;

INSERT INTO product_generic_standins (variant_product_id, generic_product_id, note)
SELECT v.variant, 882, 'รีเวท 4-2: ทีมคีย์ Express เป็น "ตะปูยิงรีเวท 4-2 (ซอง)" ตัวเดียวทุกสี (mig 167)'
  FROM (SELECT 906 AS variant UNION ALL SELECT 977 UNION ALL
        SELECT 979 UNION ALL SELECT 1894) v
 WHERE NOT EXISTS (SELECT 1 FROM product_generic_standins g
                    WHERE g.variant_product_id = v.variant);

COMMIT;
