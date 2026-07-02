-- data/migrations/122_product_created_via.sql
-- Provenance for how a product row was created. Nullable — no CHECK
-- (SQLite can't add one via ALTER cleanly); value validity is enforced in
-- app code (Phase 3 stamps 'manual' on the hand-form path and
-- 'smart_mapping' on Smart Suggest approve; pre-existing rows backfilled
-- here as 'legacy').
-- Apply: restart the app (database.py::run_pending_migrations auto-applies).
-- Rollback: data/migrations/122_product_created_via.rollback.sql
-- NOTE: do NOT self-insert into applied_migrations.
BEGIN;
ALTER TABLE products ADD COLUMN created_via TEXT;
UPDATE products SET created_via = 'legacy' WHERE created_via IS NULL;
COMMIT;
