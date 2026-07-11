-- Rollback for 134_product_generic_standins.sql
-- Clean drop: the table (seed rows included), its indexes, and its triggers
-- all go with it. Nothing else references product_generic_standins (see the
-- migration's invariant note), so there is no dependent state to clean up.
DROP TABLE IF EXISTS product_generic_standins;
