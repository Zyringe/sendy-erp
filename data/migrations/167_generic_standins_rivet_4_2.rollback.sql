-- Rollback for 167_generic_standins_rivet_4_2.sql
-- Removes ONLY the four curated 4-2 rows this migration is responsible for.
-- Scoped by (variant, generic) so it can never touch migration 134's seed
-- (the bidet/shower heads on 908/907, or the 4-6 rivets on 848).
BEGIN;
DELETE FROM product_generic_standins
 WHERE generic_product_id = 882
   AND variant_product_id IN (906, 977, 979, 1894);
COMMIT;
