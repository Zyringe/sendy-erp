-- True recovery = restore the pre-migration .backup snapshot (the old id
-- permutation is no longer derivable from the data alone).
-- This file only de-registers the migration so init_db() won't try to re-apply.
DELETE FROM applied_migrations WHERE filename = '116_renumber_employee_id.sql';
