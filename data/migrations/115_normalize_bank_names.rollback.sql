-- Value normalization is not cleanly reversible (legacy variants collapsed).
-- Rollback only de-registers the migration; data stays canonical (acceptable).
DELETE FROM applied_migrations WHERE filename = '115_normalize_bank_names.sql';
