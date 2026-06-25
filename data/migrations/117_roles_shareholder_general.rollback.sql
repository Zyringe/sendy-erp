-- Reverting requires reasserting the narrower CHECK; only safe if no user has a
-- new role. Restore from .backup if rows exist. This only de-registers.
DELETE FROM applied_migrations WHERE filename = '117_roles_shareholder_general.sql';
